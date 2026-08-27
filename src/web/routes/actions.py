"""Actions: starting jobs, editing settings, managing resumes, SSE stream."""

import asyncio
import functools
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from src.db.models import TaskKind
from src.web import jobs
from src.web.tasks import LANE_ACTIVITY, LANE_MAIN, LANE_PROFILE, TaskBusyError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions")

# Also the worst-case delay before a stream notices the server is shutting down.
HEARTBEAT_SECONDS = 3


def _panel(request: Request, error: str = "") -> HTMLResponse:
    app = request.app
    return app.state.templates.TemplateResponse(
        request,
        "partials/task_panel.html",
        {
            "tasks": app.state.tasks,
            "current_task": app.state.tasks.current,
            "activity_task": app.state.tasks.activity,
            "scheduler": app.state.scheduler,
            "llm_health": app.state.llm_health.cached,
            "error": error,
        },
    )


# What the runner offers on the panel: kind -> (task kind, job, lane).
RUNNABLE_JOBS = {
    "collect": (TaskKind.COLLECT, "collect_job", LANE_MAIN),
    "score": (TaskKind.SCORE, "score_job", LANE_MAIN),
    "apply": (TaskKind.APPLY, "apply_job", LANE_MAIN),
    "pipeline": (TaskKind.COLLECT, "pipeline_job", LANE_MAIN),
    "resume_touch": (TaskKind.RESUME_TOUCH, "resume_touch_job", LANE_MAIN),
    "login": (TaskKind.LOGIN, "login_job", LANE_MAIN),
    # Runs alongside everything else, in its own tab.
    "activity": (TaskKind.ACTIVITY, "activity_job", LANE_ACTIVITY),
    # No browser involved: runs in its own lane, parallel to everything.
    "profile": (TaskKind.PROFILE, "profile_job", LANE_PROFILE),
}


async def _start(request: Request, kind: TaskKind, job, params=None, lane: str = LANE_MAIN) -> HTMLResponse:
    try:
        await request.app.state.tasks.start(kind, job, params=params or {}, lane=lane)
    except TaskBusyError as e:
        return _panel(request, error=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("could not start task %s", kind.value)
        return _panel(request, error=str(e))
    return _panel(request)


# --- jobs ---------------------------------------------------------------

@router.post("/start", response_class=HTMLResponse)
async def start_selected(request: Request, kind: str = Form("collect")) -> HTMLResponse:
    """Single entry point for the runner: pick a job, then press «Старт»."""
    entry = RUNNABLE_JOBS.get(kind)
    if not entry:
        return _panel(request, error=f"неизвестная задача «{kind}»")

    task_kind, job_name, lane = entry
    return await _start(request, task_kind, getattr(jobs, job_name), lane=lane)


@router.post("/collect", response_class=HTMLResponse)
async def start_collect(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.COLLECT, jobs.collect_job)


@router.post("/score", response_class=HTMLResponse)
async def start_score(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.SCORE, jobs.score_job)


@router.post("/pipeline", response_class=HTMLResponse)
async def start_pipeline(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.COLLECT, jobs.pipeline_job)


@router.post("/apply", response_class=HTMLResponse)
async def start_apply(
    request: Request,
    vacancy_ids: Optional[List[int]] = Form(None),
) -> HTMLResponse:
    job = functools.partial(jobs.apply_job, vacancy_ids=vacancy_ids) if vacancy_ids else jobs.apply_job
    return await _start(
        request, TaskKind.APPLY, job, params={"vacancy_ids": vacancy_ids or []}
    )


@router.post("/login", response_class=HTMLResponse)
async def start_login(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.LOGIN, jobs.login_job)


@router.post("/activity", response_class=HTMLResponse)
async def start_activity(request: Request) -> HTMLResponse:
    return await _start(request, TaskKind.ACTIVITY, jobs.activity_job, lane=LANE_ACTIVITY)


@router.post("/stop", response_class=HTMLResponse)
async def stop_task(request: Request, lane: str = Form(LANE_MAIN)) -> HTMLResponse:
    stopped = request.app.state.tasks.request_stop(lane)
    return _panel(request, error="" if stopped else "нет выполняющейся задачи")


@router.post("/confirm", response_class=HTMLResponse)
async def confirm_task(request: Request, lane: str = Form(LANE_MAIN)) -> HTMLResponse:
    confirmed = request.app.state.tasks.confirm(lane)
    return _panel(request, error="" if confirmed else "задача не ждёт подтверждения")


# --- resume -------------------------------------------------------------

@router.post("/resume/import")
async def import_resume(request: Request, resume_url: str = Form(...)) -> RedirectResponse:
    job = functools.partial(jobs.resume_import_job, resume_url=resume_url.strip())
    try:
        await request.app.state.tasks.start(
            TaskKind.RESUME_IMPORT, job, params={"resume_url": resume_url.strip()}
        )
    except TaskBusyError as e:
        return RedirectResponse(f"/resume?error={e}", status_code=303)
    return RedirectResponse("/resume", status_code=303)


@router.post("/resume/{resume_id}/update")
async def update_resume(
    request: Request,
    resume_id: int,
    search_query: str = Form(""),
    context_text: str = Form(""),
) -> RedirectResponse:
    """Save the two hand-edited fields and refresh the profile if they changed."""
    repository = request.app.state.repository
    before = await repository.get_resume(resume_id)
    await repository.update_resume_fields(resume_id, search_query.strip(), context_text.strip())

    context_changed = bool(before) and before.context_text.strip() != context_text.strip()
    if context_changed and bool(request.app.state.settings.get("llm.enabled", False)):
        # The profile is built from this text, so it is rebuilt in its own lane
        # — it needs no browser and must not wait for a collection run.
        job = functools.partial(jobs.profile_job, resume_id=resume_id)
        try:
            await request.app.state.tasks.start(
                TaskKind.PROFILE, job, params={"resume_id": resume_id}, lane=LANE_PROFILE
            )
        except TaskBusyError as e:
            logger.info("profile rebuild postponed: %s", e)

    return RedirectResponse("/resume", status_code=303)


@router.post("/resume/{resume_id}/profile")
async def rebuild_profile(
    request: Request, resume_id: int, model: str = Form("")
) -> RedirectResponse:
    model = model.strip()
    if model:
        # Picked next to the button; remembered so it is preselected next time.
        await request.app.state.settings.save({"profile.model": model})

    job = functools.partial(jobs.profile_job, resume_id=resume_id, model=model)
    try:
        await request.app.state.tasks.start(
            TaskKind.PROFILE, job, params={"resume_id": resume_id}, lane=LANE_PROFILE
        )
    except TaskBusyError as e:
        return RedirectResponse(f"/resume?error={e}", status_code=303)
    return RedirectResponse("/resume", status_code=303)


@router.post("/resume/{resume_id}/activate")
async def activate_resume(request: Request, resume_id: int) -> RedirectResponse:
    await request.app.state.repository.set_active_resume(resume_id)
    return RedirectResponse("/resume", status_code=303)


@router.post("/resume/{resume_id}/delete")
async def delete_resume(request: Request, resume_id: int) -> RedirectResponse:
    await request.app.state.repository.delete_resume(resume_id)
    return RedirectResponse("/resume", status_code=303)


# --- settings -----------------------------------------------------------

@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request) -> HTMLResponse:
    form = await request.form()
    raw = {key: str(value) for key, value in form.multi_items()}

    settings = request.app.state.settings
    errors = await settings.save(raw)
    if errors:
        return request.app.state.templates.TemplateResponse(
            request,
            "settings.html",
            {
                "groups": settings.grouped_fields(),
                "errors": errors,
                "saved": False,
                "tasks": request.app.state.tasks,
                "current_task": request.app.state.tasks.current,
                "scheduler": request.app.state.scheduler,
                "path": "/settings",
            },
            status_code=400,
        )

    request.app.state.scheduler.reschedule()
    request.app.state.llm_health.invalidate()  # endpoint/key/model may have changed
    request.app.state.limiter.reconfigure(settings.ratelimit_config())
    return RedirectResponse("/settings?saved=1", status_code=303)


# --- llm connectivity ---------------------------------------------------

@router.get("/llm-models", response_class=HTMLResponse)
async def llm_models(
    request: Request, force: bool = False, field: str = "llm.model"
) -> HTMLResponse:
    """Model dropdown for a model-typed setting, filled from the endpoint."""
    monitor = request.app.state.llm_health
    health = await monitor.get(force=force)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/model_field.html",
        {
            "models": monitor.models,
            "current": str(request.app.state.settings.get(field, "")),
            "field_key": field,
            # Only the main model must be set; the others may follow it.
            "allow_empty": field != "llm.model",
            "error": "" if health.ok else health.message,
        },
    )


@router.get("/llm-health", response_class=HTMLResponse)
async def llm_health(request: Request, force: bool = False, compact: bool = False) -> HTMLResponse:
    health = await request.app.state.llm_health.get(force=force)
    # `initial` stays False here: a refreshed lamp that still carried
    # hx-trigger="load" would re-request itself instantly, forever.
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/llm_status.html",
        {"llm_health": health, "compact": compact, "initial": False},
    )


# --- live updates -------------------------------------------------------

@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    manager = request.app.state.tasks
    queue = manager.subscribe()

    async def stream():
        try:
            state = manager.current
            if state:
                yield _sse({"type": "snapshot", "task": state.as_dict()})
                for line in list(state.logs):
                    yield _sse({"type": "log", "line": line, "task": state.as_dict()})
            while True:
                # An endless stream would make uvicorn hang on Ctrl+C waiting
                # for this connection, so the server closes it itself.
                if request.app.state.shutting_down:
                    yield _sse({"type": "server_closing"})
                    break
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
