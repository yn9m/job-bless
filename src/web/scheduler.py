"""Interval scheduler.

Three independent cycles, because they serve different purposes and run at
different rhythms:

* the pipeline (collect -> score -> apply) in the main lane;
* activity browsing in its own lane, so it overlaps with the pipeline;
* the resume refresh, which hh.ru only allows every few hours.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from src.db.models import TaskKind
from src.web.jobs import activity_job, pipeline_job, resume_touch_job
from src.web.tasks import LANE_ACTIVITY, LANE_MAIN, TaskBusyError, TaskManager

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
# When a lane is busy, look again shortly instead of skipping a whole interval.
POSTPONE_MINUTES = 5


@dataclass
class ScheduleEntry:
    name: str
    title: str
    lane: str
    kind: TaskKind
    job: Callable
    enabled_key: str
    interval_key: str
    interval_scale: float = 1.0  # minutes per configured unit
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None


class Scheduler:
    def __init__(self, manager: TaskManager, settings):
        self.manager = manager
        self.settings = settings
        self.entries: List[ScheduleEntry] = [
            ScheduleEntry(
                name="pipeline",
                title="Сбор и оценка",
                lane=LANE_MAIN,
                kind=TaskKind.COLLECT,
                job=pipeline_job,
                enabled_key="schedule.enabled",
                interval_key="schedule.interval_minutes",
            ),
            ScheduleEntry(
                name="activity",
                title="Активность",
                lane=LANE_ACTIVITY,
                kind=TaskKind.ACTIVITY,
                job=activity_job,
                enabled_key="schedule.activity_enabled",
                interval_key="schedule.activity_interval_minutes",
            ),
            ScheduleEntry(
                name="resume_touch",
                title="Обновление резюме",
                lane=LANE_MAIN,
                kind=TaskKind.RESUME_TOUCH,
                job=resume_touch_job,
                enabled_key="schedule.resume_touch_enabled",
                interval_key="schedule.resume_touch_interval_hours",
                interval_scale=60.0,
            ),
        ]
        self._task: Optional[asyncio.Task] = None

    # --- configuration ----------------------------------------------------

    def is_enabled(self, entry: ScheduleEntry) -> bool:
        return bool(self.settings.get(entry.enabled_key, False))

    def interval_minutes(self, entry: ScheduleEntry) -> float:
        raw = float(self.settings.get(entry.interval_key, 60))
        return max(5.0, raw * entry.interval_scale)

    # Kept for the templates that show a single "schedule is on" note.
    @property
    def enabled(self) -> bool:
        return any(self.is_enabled(entry) for entry in self.entries)

    @property
    def interval_minutes_main(self) -> int:
        return int(self.interval_minutes(self.entries[0]))

    @property
    def next_run_at(self) -> Optional[datetime]:
        upcoming = [e.next_run_at for e in self.entries if self.is_enabled(e) and e.next_run_at]
        return min(upcoming) if upcoming else None

    def status(self) -> List[Dict[str, object]]:
        """What the settings/dashboard pages show about each cycle."""
        return [
            {
                "name": entry.name,
                "title": entry.title,
                "enabled": self.is_enabled(entry),
                "interval_minutes": int(self.interval_minutes(entry)),
                "next_run_at": entry.next_run_at,
                "last_run_at": entry.last_run_at,
            }
            for entry in self.entries
        ]

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "scheduler started: %s",
            ", ".join(
                f"{e.name}={'on' if self.is_enabled(e) else 'off'}/{self.interval_minutes(e):.0f}m"
                for e in self.entries
            ),
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def reschedule(self) -> None:
        """Called after the settings change so the UI shows the new times."""
        now = datetime.now(timezone.utc)
        for entry in self.entries:
            if not self.is_enabled(entry):
                entry.next_run_at = None
                continue
            base = entry.last_run_at or now
            entry.next_run_at = base + timedelta(minutes=self.interval_minutes(entry))

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                for entry in self.entries:
                    await self._tick(entry)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                logger.exception("scheduler tick failed")

    async def _tick(self, entry: ScheduleEntry) -> None:
        if not self.is_enabled(entry):
            entry.next_run_at = None
            return

        now = datetime.now(timezone.utc)
        if entry.next_run_at is None:
            entry.next_run_at = now + timedelta(minutes=self.interval_minutes(entry))
            return
        if now < entry.next_run_at:
            return

        if self.manager.lane(entry.lane).is_busy:
            logger.info("scheduled %s postponed: lane %s is busy", entry.name, entry.lane)
            entry.next_run_at = now + timedelta(minutes=POSTPONE_MINUTES)
            return

        logger.info("scheduled run starting: %s", entry.name)
        try:
            await self.manager.start(entry.kind, entry.job, trigger="schedule", lane=entry.lane)
        except TaskBusyError:
            entry.next_run_at = now + timedelta(minutes=POSTPONE_MINUTES)
            return

        entry.last_run_at = now
        entry.next_run_at = now + timedelta(minutes=self.interval_minutes(entry))
