"""FastAPI application: pages, actions and the background runtime.

Everything lives in one process — the web server, the job runner that drives
the browser, and the scheduler. Wiring happens in the lifespan handler.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.browser.session import SESSION
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

COOKIE_NAME = "job_bless_token"

# Hard deadline for uvicorn's graceful shutdown, in case something else stalls.
SHUTDOWN_DEADLINE_SECONDS = 5

# How often the connectivity lamp re-checks itself.
LLM_POLL_SECONDS = 10


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or Config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.db.driver == "sqlite":
            connection = await init_sqlite(config.db.sqlite_path)
            repository = DatabaseRepository(connection, driver="sqlite")
        else:
            connection = await init_postgres(config.db)
            repository = DatabaseRepository(connection, driver="postgres")

        await repository.fail_stale_task_runs()

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

        logger.info("web app ready at http://%s:%d", config.web.host, config.web.port)
        try:
            yield
        finally:
            await scheduler.stop()
            await manager.shutdown()
            await SESSION.close()  # drop the shared browser connection
            try:
                await connection.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("error closing db connection: %s", e)

    app = FastAPI(title="job-bless", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Flipped by the signal handler (see `serve`) so open SSE streams end
    # themselves — otherwise uvicorn waits for them forever on Ctrl+C.
    app.state.shutting_down = False
    app.mount("/static", RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["score_class"] = _score_class
    templates.env.filters["short_dt"] = _short_dt
    # Cache buster: without it browsers keep serving yesterday's stylesheet.
    templates.env.globals["static_version"] = _static_version()
    templates.env.globals["poll_seconds"] = LLM_POLL_SECONDS
    app.state.templates = templates

    if config.web.token:
        _install_token_guard(app, config.web.token)

    from src.web.routes import actions, pages  # imported here to avoid a cycle

    app.include_router(pages.router)
    app.include_router(actions.router)

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
    app = create_app(config)

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
    await server.serve()


def _install_token_guard(app: FastAPI, token: str) -> None:
    """Minimal shared-secret guard for when the UI is not on localhost."""

    @app.middleware("http")
    async def token_middleware(request: Request, call_next):  # noqa: ANN001
        if request.url.path.startswith("/static"):
            return await call_next(request)

        provided = request.query_params.get("token") or request.cookies.get(COOKIE_NAME)
        if provided != token:
            return JSONResponse({"detail": "Требуется корректный token"}, status_code=401)

        if request.query_params.get("token") and not request.cookies.get(COOKIE_NAME):
            response = RedirectResponse(request.url.path, status_code=303)
            response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
            return response
        return await call_next(request)


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
