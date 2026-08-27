"""Background job runner for the web UI.

Jobs run in *lanes*. The main lane drives the collector, the scorer and the
applier — those share one browser workflow and must not overlap. The activity
lane runs alongside them: keeping the account warm is a different job with its
own tab, its own settings and its own schedule.
"""

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

from src.db.models import TaskKind, TaskRun, TaskStatus
from src.ratelimit import PacedLogger, RateLimiter

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 500

LANE_MAIN = "main"
LANE_ACTIVITY = "activity"
# Building the candidate profile touches no browser, so it never has to wait
# for the collector or the applier.
LANE_PROFILE = "profile"
LANES = (LANE_MAIN, LANE_ACTIVITY, LANE_PROFILE)


class TaskBusyError(RuntimeError):
    """Raised when a job is requested while the lane is still running one."""


@dataclass
class TaskState:
    id: str
    kind: TaskKind
    lane: str = LANE_MAIN
    status: TaskStatus = TaskStatus.RUNNING
    trigger: str = "manual"
    message: str = ""
    done: int = 0
    total: int = 0
    params: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    # Set when the job waits for the user (e.g. "I have logged in").
    awaiting_confirmation: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "lane": self.lane,
            "status": self.status.value,
            "trigger": self.trigger,
            "message": self.message,
            "done": self.done,
            "total": self.total,
            "percent": int(self.done * 100 / self.total) if self.total else 0,
            "error_message": self.error_message,
            "result": self.result,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "awaiting_confirmation": self.awaiting_confirmation,
            "is_running": self.status == TaskStatus.RUNNING,
        }


class Lane:
    """One slot for a running job, with its own stop and confirm signals."""

    def __init__(self, name: str):
        self.name = name
        self.current: Optional[TaskState] = None
        self.stop_requested = False
        self.confirm_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.stop_hooks: List[Callable[[], Any]] = []
        self.kill_timer: Optional[asyncio.Task] = None

    @property
    def is_busy(self) -> bool:
        return self.task is not None and not self.task.done()


class TaskContext:
    """Handed to a job: logging, progress, stop and confirmation signals."""

    def __init__(self, manager: "TaskManager", state: TaskState, lane: Lane):
        self._manager = manager
        self._lane = lane
        self.state = state
        self.repository = manager.repository
        self.settings = manager.settings
        self.config = manager.config
        # Shared across lanes: the per-minute budget is global, not per task.
        self.limiter = manager.limiter
        self._pace_logger = PacedLogger(self.log)

    @property
    def lane(self) -> str:
        return self._lane.name

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} {message}"
        self.state.logs.append(line)
        self.state.message = message
        logger.info("[task %s/%s] %s", self._lane.name, self.state.kind.value, message)
        self._manager.publish(
            {"type": "log", "lane": self._lane.name, "line": line, "task": self.state.as_dict()}
        )

    def progress(self, done: int, total: int = 0) -> None:
        self.state.done = done
        if total:
            self.state.total = total
        self._manager.publish(
            {"type": "progress", "lane": self._lane.name, "task": self.state.as_dict()}
        )

    def should_stop(self) -> bool:
        return self._lane.stop_requested

    def raise_if_stopped(self) -> None:
        if self.should_stop():
            raise asyncio.CancelledError("stopped by user")

    def on_stop(self, callback: Callable[[], Any]) -> None:
        """Register a callback fired the moment the user presses «Стоп».

        Without it a job only notices the flag at its next checkpoint, which for
        the collector is a whole search page away.
        """
        self._manager.add_stop_hook(callback, lane=self._lane.name)

    async def pace(self) -> float:
        """Wait out the configured interval before opening the next page."""
        waited = await self.limiter.acquire(should_stop=self.should_stop)
        self._pace_logger.report(waited, self.limiter)
        return waited

    async def wait_for_confirmation(self, timeout_sec: float = 900.0) -> bool:
        """Block until the user presses the confirm button in the UI."""
        self.state.awaiting_confirmation = True
        self._manager.publish(
            {"type": "progress", "lane": self._lane.name, "task": self.state.as_dict()}
        )
        try:
            await asyncio.wait_for(self._lane.confirm_event.wait(), timeout=timeout_sec)
            # «Стоп» also releases the event — that is a cancellation, not a confirmation.
            self.raise_if_stopped()
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.state.awaiting_confirmation = False
            self._lane.confirm_event.clear()


JobFn = Callable[[TaskContext], Awaitable[Dict[str, Any]]]


class TaskManager:
    """Runs one job per lane and broadcasts progress to the browser."""

    STOP_GRACE_SECONDS = 4.0

    def __init__(self, config, repository, settings, limiter: Optional[RateLimiter] = None):
        self.config = config
        self.repository = repository
        self.settings = settings
        self.limiter = limiter or RateLimiter(settings.ratelimit_config())
        self.lanes: Dict[str, Lane] = {name: Lane(name) for name in LANES}
        self.history: List[TaskState] = []
        self._subscribers: Set[asyncio.Queue] = set()

    # --- lane access ------------------------------------------------------

    def lane(self, name: str = LANE_MAIN) -> Lane:
        if name not in self.lanes:
            raise TaskBusyError(f"неизвестная очередь «{name}»")
        return self.lanes[name]

    @property
    def current(self) -> Optional[TaskState]:
        return self.lanes[LANE_MAIN].current

    @property
    def is_busy(self) -> bool:
        return self.lanes[LANE_MAIN].is_busy

    @property
    def activity(self) -> Optional[TaskState]:
        return self.lanes[LANE_ACTIVITY].current

    @property
    def activity_busy(self) -> bool:
        return self.lanes[LANE_ACTIVITY].is_busy

    @property
    def stop_requested(self) -> bool:
        return self.lanes[LANE_MAIN].stop_requested

    # --- subscriptions (SSE) ---------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: Dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow browser must not stall the job.
                self._subscribers.discard(queue)

    # --- job control ------------------------------------------------------

    async def start(
        self,
        kind: TaskKind,
        job: JobFn,
        *,
        params: Optional[Dict[str, Any]] = None,
        trigger: str = "manual",
        lane: str = LANE_MAIN,
    ) -> TaskState:
        target = self.lane(lane)
        if target.is_busy:
            running = target.current.kind.value if target.current else "задача"
            raise TaskBusyError(f"уже выполняется «{running}»")

        state = TaskState(
            id=uuid.uuid4().hex[:12], kind=kind, lane=lane, trigger=trigger, params=params or {}
        )
        target.current = state
        target.stop_requested = False
        target.confirm_event.clear()
        target.stop_hooks.clear()
        self._cancel_kill_timer(target)

        await self.repository.create_task_run(
            TaskRun(id=state.id, kind=kind, status=TaskStatus.RUNNING, trigger=trigger, params=state.params)
        )
        self.publish({"type": "started", "lane": lane, "task": state.as_dict()})
        target.task = asyncio.create_task(self._run(job, state, target))
        return state

    async def _run(self, job: JobFn, state: TaskState, lane: Lane) -> None:
        context = TaskContext(self, state, lane)
        try:
            state.result = await job(context) or {}
            state.status = TaskStatus.CANCELLED if lane.stop_requested else TaskStatus.COMPLETED
            context.log("задача остановлена" if lane.stop_requested else "задача завершена")
        except asyncio.CancelledError:
            state.status = TaskStatus.CANCELLED
            state.error_message = "остановлено пользователем"
            context.log("задача остановлена")
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            state.status = TaskStatus.FAILED
            state.error_message = str(e)
            logger.exception("task %s failed", state.kind.value)
            context.log(f"ошибка: {e}")
        finally:
            state.finished_at = datetime.now(timezone.utc)
            state.awaiting_confirmation = False
            self._cancel_kill_timer(lane)
            lane.stop_hooks.clear()
            try:
                await self.repository.finish_task_run(
                    state.id, state.status, state.result, state.error_message
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("could not persist task run %s: %s", state.id, e)
            self.history.insert(0, state)
            del self.history[20:]
            self.publish({"type": "finished", "lane": lane.name, "task": state.as_dict()})

    # --- stopping ---------------------------------------------------------

    def add_stop_hook(self, callback: Callable[[], Any], *, lane: str = LANE_MAIN) -> None:
        target = self.lane(lane)
        target.stop_hooks.append(callback)
        if target.stop_requested:  # the stop already happened — honour it at once
            self._fire_stop_hooks(target)

    def _fire_stop_hooks(self, lane: Lane) -> None:
        for hook in lane.stop_hooks:
            try:
                hook()
            except Exception as e:  # noqa: BLE001
                logger.warning("stop hook failed: %s", e)

    def request_stop(self, lane: str = LANE_MAIN) -> bool:
        """First press asks politely, a second one (or the timer) kills the task."""
        target = self.lane(lane)
        if not target.is_busy:
            return False

        if target.stop_requested:
            self._hard_cancel(target, "остановлено принудительно")
            return True

        target.stop_requested = True
        if target.current:
            target.current.message = "останавливаюсь..."
        self._fire_stop_hooks(target)
        target.confirm_event.set()  # unblock a job waiting for confirmation
        self.publish(
            {
                "type": "stopping",
                "lane": lane,
                "task": target.current.as_dict() if target.current else {},
            }
        )

        # Browser and network calls can hang far longer than the next checkpoint,
        # so a graceful stop gets a short deadline and then the task is cancelled.
        try:
            target.kill_timer = asyncio.create_task(self._kill_after_grace(target))
        except RuntimeError:  # called from outside the loop — hooks already fired
            logger.warning("stop watchdog not scheduled: no running event loop")
        return True

    async def _kill_after_grace(self, lane: Lane) -> None:
        try:
            await asyncio.sleep(self.STOP_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        if lane.is_busy and lane.stop_requested:
            self._hard_cancel(lane, "задача не остановилась сама — прерываю")

    def _hard_cancel(self, lane: Lane, reason: str) -> None:
        if not lane.task or lane.task.done():
            return
        logger.info("hard-cancelling task in lane %s: %s", lane.name, reason)
        if lane.current:
            lane.current.message = reason
        lane.task.cancel()

    def _cancel_kill_timer(self, lane: Lane) -> None:
        if lane.kill_timer and not lane.kill_timer.done():
            lane.kill_timer.cancel()
        lane.kill_timer = None

    def confirm(self, lane: str = LANE_MAIN) -> bool:
        """User pressed the confirmation button the running job waits for."""
        target = self.lane(lane)
        if not target.is_busy or not target.current or not target.current.awaiting_confirmation:
            return False
        target.confirm_event.set()
        return True

    async def shutdown(self) -> None:
        for lane in self.lanes.values():
            if lane.is_busy and lane.task:
                lane.stop_requested = True
                lane.task.cancel()
                try:
                    await lane.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
