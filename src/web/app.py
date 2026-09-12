"""FastAPI application: pages, actions and the background runtime.

Everything lives in one process — the web server, the job runner that drives
the browser, and the scheduler. Wiring happens in the lifespan handler.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.browser.session import SESSION
from src.browser.docker_runtime import prepare_browser, local_panel_url
from src.aistudio.runtime import AIStudioRuntime
from src.aistudio.remote import RemoteAIStudioRuntime
from src.web.auth import OwnerAuth, router as auth_router
from src.web.workspace_auth import WorkspaceAuth
from src.web.accounts import RemoteScreens, router as accounts_router
from src.config import Config
from src.db.connection import init_postgres, init_sqlite
from src.db.repository import DatabaseRepository
from src.llm.health import LLMHealthMonitor
from src.ratelimit import RateLimiter
from src.resume.service import ResumeService
from src.settings import SettingsService
from src.web.scheduler import Scheduler
from src.web.tasks import TaskManager

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Hard deadline for uvicorn's graceful shutdown, in case something else stalls.
SHUTDOWN_DEADLINE_SECONDS = 5

# How often the connectivity lamp re-checks itself.
LLM_POLL_SECONDS = 10


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or Config.load()
    auth = WorkspaceAuth(config.web) if config.web.workspace_token else OwnerAuth(config.web)
    if (config.accounts.hh_vnc_host or config.accounts.google_vnc_host) and not auth.enabled:
        raise ValueError("Remote account screens require WEB_PASSWORD_HASH")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        SESSION.keep_alive = True
        config.browser.storage_state_path = str(Path(config.db.sqlite_path).resolve().parent / "hh-session.json")
        if config.db.driver == "sqlite":
            connection = await init_sqlite(config.db.sqlite_path)
            repository = DatabaseRepository(connection, driver="sqlite")
        else:
            connection = await init_postgres(config.db)
            repository = DatabaseRepository(connection, driver="postgres")

        await repository.fail_stale_task_runs()
        if config.accounts.hh_vnc_host and not Path(config.browser.storage_state_path).is_file():
            # An old Chrome login record cannot authenticate a new Camoufox context.
            await repository.save_settings({"hh.login_required": "true"})

        settings = SettingsService(config, repository)
        await settings.load()

        # Resumes imported by an older parser keep their full page text, so the
        # missing sections are recovered here instead of asking for a re-import.
        try:
            recovered = await ResumeService(repository, settings).backfill_sections()
            if recovered:
                logger.info("re-parsed %d resume(s) from stored text", recovered)
        except Exception as e:  # noqa: BLE001 - never block startup on this
            logger.warning("resume backfill skipped: %s", e)

        # The search query used to be a single global setting; give it to the
        # active resume so nothing is lost when it moves onto the resume card.
        try:
            active = await repository.get_active_resume()
            if active and not active.search_query.strip() and settings.legacy_query:
                await repository.update_resume_fields(
                    active.id, settings.legacy_query, active.context_text
                )
                logger.info(
                    "search query %r moved to resume %s", settings.legacy_query, active.id
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("could not move the search query onto the resume: %s", e)

        # One limiter for the whole process: collector, applier and resume
        # import share a single budget against hh.ru.
        limiter = RateLimiter(settings.ratelimit_config())
        manager = TaskManager(config, repository, settings, limiter=limiter)
        # Slightly below the poll interval, so each poll refreshes the verdict
        # while several lamps on one page still share a single probe.
        health_monitor = LLMHealthMonitor(settings, ttl_seconds=LLM_POLL_SECONDS - 1)
        scheduler = Scheduler(manager, settings)
        scheduler.reschedule()
        scheduler.start()

        app.state.config = config
        app.state.connection = connection
        app.state.repository = repository
        app.state.settings = settings
        app.state.tasks = manager
        app.state.scheduler = scheduler
        app.state.llm_health = health_monitor
        app.state.limiter = limiter
        runtime_class = RemoteAIStudioRuntime if config.accounts.google_url else AIStudioRuntime
        app.state.aistudio = runtime_class(settings, health_monitor)
        app.state.screens = RemoteScreens(config.accounts)
        manager.manual_hh = lambda: app.state.screens.active("hh")
        manager.connection_busy = lambda: app.state.aistudio.busy
        app.state.aistudio.autostart()

        logger.info("web app ready at http://%s:%d", config.web.host, config.web.port)
        try:
            yield
        finally:
            await scheduler.stop()
            await app.state.screens.close()
            await manager.shutdown()
            await app.state.aistudio.stop()
            try:
                await SESSION.close()
            except Exception:
                logger.exception("Не удалось сохранить сессию HH при остановке")
            try:
                await connection.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("error closing db connection: %s", e)

    app = FastAPI(title="job-bless", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    # Flipped by the signal handler (see `serve`) so open SSE streams end
    # themselves — otherwise uvicorn waits for them forever on Ctrl+C.
    app.state.shutting_down = False
    app.mount("/static", RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")
    if config.accounts.hh_vnc_host or config.accounts.google_vnc_host:
        app.mount("/remote-static", StaticFiles(directory=config.accounts.novnc_path), name="remote-static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["score_class"] = _score_class
    templates.env.filters["short_dt"] = _short_dt
    # Cache buster: without it browsers keep serving yesterday's stylesheet.
    templates.env.globals["static_version"] = _static_version()
    templates.env.globals["poll_seconds"] = LLM_POLL_SECONDS
    templates.env.globals["clerk_publishable_key"] = config.web.clerk_publishable_key if config.web.workspace_token else ""
    if config.web.workspace_token and config.web.clerk_publishable_key:
        from src.web.clerk_auth import frontend_host
        templates.env.globals["clerk_host"] = frontend_host(config.web.clerk_publishable_key)
    app.state.templates = templates

    app.middleware("http")(auth.guard)

    from src.web.routes import actions, pages  # imported here to avoid a cycle

    app.include_router(pages.router)
    app.include_router(actions.router)
    app.include_router(auth_router)
    app.include_router(accounts_router)

    @app.get("/healthz")
    async def healthz():
        return {"service": "job-bless", "status": "ok", "instance": getattr(config.app, "instance_id", "")}

    if config.web.workspace_token:
        @app.get("/internal/idle-status")
        async def idle_status():
            state = app.state
            scheduled = any(state.settings.get(key, False) for key in (
                "schedule.enabled", "schedule.resume_touch_enabled"))
            return {"busy": any(lane.is_busy for lane in state.tasks.lanes.values())
                    or state.aistudio.busy or any(state.screens.active(p) for p in ("hh", "google")),
                    "scheduled": scheduled}

        @app.post("/internal/revoke-session")
        async def revoke_workspace_session(request: Request):
            from src.db.models import TaskKind
            sid = (await request.json()).get("session_id", "")
            state = app.state
            hh_lease = state.screens.leases.get("hh")
            google_lease = state.screens.leases.get("google")
            hh_owned = hh_lease is not None and hh_lease.owner == sid
            google_owned = google_lease is not None and google_lease.owner == sid
            state.auth.sessions.pop(sid, None)
            await state.screens.revoke_owner(sid)
            current = state.tasks.current
            if hh_owned and current and state.tasks.is_busy and current.kind == TaskKind.LOGIN:
                state.tasks.request_stop()
            if google_owned and state.aistudio.state == "login":
                await state.aistudio.stop()
            return {"revoked": True}

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        return PlainTextResponse("Страница не найдена", status_code=404)

    return app


async def serve(config: Optional[Config] = None) -> None:
    """Run the web UI with a shutdown that actually shuts down.

    Ctrl+C used to hang on "Waiting for connections to close": the live-log SSE
    streams never end on their own. Now the signal marks the app as closing (the
    streams notice within a heartbeat) and uvicorn gets a hard deadline as well.
    """
    import uvicorn

    config = config or Config.load()
    runtime = await asyncio.to_thread(prepare_browser, config)
    if runtime:
        logger.info("Откройте панель по личной ссылке: %s", local_panel_url(config))
    try:
        app = create_app(config)
    except BaseException:
        if runtime:
            await asyncio.to_thread(runtime.stop)
        raise

    class GracefulServer(uvicorn.Server):
        def handle_exit(self, sig, frame):  # noqa: ANN001
            app.state.shutting_down = True
            super().handle_exit(sig, frame)

    server = GracefulServer(
        uvicorn.Config(
            app,
            host=config.web.host,
            port=config.web.port,
            log_level=config.log.level.lower(),
            timeout_graceful_shutdown=SHUTDOWN_DEADLINE_SECONDS,
        )
    )
    try:
        await server.serve()
    finally:
        if runtime:
            await asyncio.to_thread(runtime.stop)


class RevalidatedStaticFiles(StaticFiles):
    """Static files that must be revalidated instead of silently cached.

    Assets change with every UI tweak; without this a browser happily keeps a
    stale app.css and the page looks unstyled.
    """

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _static_version() -> str:
    """Newest mtime across the static folder, used as the ?v= query."""
    try:
        newest = max(path.stat().st_mtime for path in STATIC_DIR.glob("*") if path.is_file())
    except (OSError, ValueError):
        return "0"
    return str(int(newest))


def _score_class(score: Optional[int]) -> str:
    if score is None:
        return "score-none"
    if score >= 80:
        return "score-high"
    if score >= 60:
        return "score-mid"
    return "score-low"


def _short_dt(value) -> str:  # noqa: ANN001
    if not value:
        return "—"
    text = str(value)
    text = text.replace("T", " ")
    return text[:16]
