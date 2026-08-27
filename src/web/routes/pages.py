"""HTML pages rendered with Jinja."""

import logging
import math
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_SIZE = 25


def _render(request: Request, template: str, **context) -> HTMLResponse:
    app = request.app
    context.setdefault("tasks", app.state.tasks)
    context.setdefault("current_task", app.state.tasks.current)
    context.setdefault("activity_task", app.state.tasks.activity)
    context.setdefault("scheduler", app.state.scheduler)
    context.setdefault("path", request.url.path)
    # Cached verdict only — the lamp refreshes itself over htmx, so rendering
    # a page never waits on the network.
    context.setdefault("llm_health", app.state.llm_health.cached)
    return app.state.templates.TemplateResponse(request, template, context)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    repository = request.app.state.repository
    settings = request.app.state.settings

    resume = await repository.get_active_resume()
    stats = await repository.get_dashboard_stats(resume.id if resume else None)
    runs = await repository.list_task_runs(limit=5)
    threshold = int(settings.get("matching.threshold", 70))

    ready_to_apply = 0
    if resume:
        ready_to_apply = await repository.count_vacancies(
            resume_id=resume.id, min_score=threshold, only_unapplied=True, only_scored=True
        )

    return _render(
        request,
        "dashboard.html",
        stats=stats,
        resume=resume,
        runs=runs,
        threshold=threshold,
        ready_to_apply=ready_to_apply,
        apply_mode=settings.get("apply.mode", "manual"),
        # The query belongs to the resume now, not to the global settings.
        search_query=resume.search_query if resume else "",
        search_url=settings.search_url_for(resume.search_query) if resume else settings.search_url,
    )


@router.get("/vacancies", response_class=HTMLResponse)
async def vacancies(
    request: Request,
    page: int = Query(1, ge=1),
    min_score: Optional[int] = Query(None, ge=0, le=100),
    search: str = Query(""),
    only_unapplied: bool = Query(False),
    only_scored: bool = Query(False),
    found_for_resume: int = Query(0),
    order: str = Query("score"),
) -> HTMLResponse:
    repository = request.app.state.repository
    settings = request.app.state.settings
    resume = await repository.get_active_resume()
    resume_id = resume.id if resume else None

    filters = dict(
        resume_id=resume_id,
        min_score=min_score,
        only_unapplied=only_unapplied,
        only_scored=only_scored,
        search=search.strip(),
        found_for_resume=found_for_resume or None,
    )
    total = await repository.count_vacancies(**filters)
    rows = await repository.list_vacancies(
        **filters, order=order, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )

    return _render(
        request,
        "vacancies.html",
        rows=rows,
        total=total,
        page=page,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        min_score=min_score,
        search=search,
        only_unapplied=only_unapplied,
        only_scored=only_scored,
        order=order,
        resume=resume,
        resumes=await repository.list_resumes(),
        found_for_resume=found_for_resume,
        threshold=int(settings.get("matching.threshold", 70)),
    )


@router.get("/applications", response_class=HTMLResponse)
async def applications(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query(""),
) -> HTMLResponse:
    repository = request.app.state.repository
    total = await repository.count_applications(status)
    rows = await repository.list_applications(
        status=status, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    stats = await repository.get_dashboard_stats(None)

    return _render(
        request,
        "applications.html",
        rows=rows,
        total=total,
        page=page,
        pages=max(1, math.ceil(total / PAGE_SIZE)),
        status=status,
        by_status=stats["by_status"],
    )


@router.get("/resume", response_class=HTMLResponse)
async def resume_page(request: Request) -> HTMLResponse:
    repository = request.app.state.repository
    settings = request.app.state.settings
    resumes = await repository.list_resumes()
    return _render(
        request,
        "resume.html",
        resumes=resumes,
        # Cached model list — the profile model is picked right by the button.
        models=request.app.state.llm_health.models,
        profile_model=settings.profile_config().model,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = Query(False)) -> HTMLResponse:
    settings = request.app.state.settings
    return _render(request, "settings.html", groups=settings.grouped_fields(), saved=saved, errors=[])


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    runs = await request.app.state.repository.list_task_runs(limit=50)
    return _render(request, "runs.html", runs=runs)


@router.get("/partials/status", response_class=HTMLResponse)
async def status_partial(request: Request) -> HTMLResponse:
    return _render(request, "partials/task_panel.html")
