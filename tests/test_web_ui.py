"""Smoke tests for the web UI: pages render, settings persist, jobs are guarded.

No browser and no LLM are involved — jobs are only inspected, never started
against real hh.ru.
"""

import pytest
from fastapi.testclient import TestClient

from src.config import Config
from src.db.models import Resume, TaskKind, VacancyScore
from src.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "test.db")
    config.web.token = ""
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_pages_render(client):
    for path in ("/", "/vacancies", "/applications", "/resume", "/settings", "/runs"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "job-bless" in response.text


def test_task_panel_partial(client):
    response = client.get("/partials/status")
    assert response.status_code == 200
    assert 'id="task-panel"' in response.text


def test_settings_roundtrip(client):
    response = client.post(
        "/actions/settings",
        data={
            "matching.threshold": "85",
            "apply.mode": "auto",
            "schedule.enabled": "1",
            "schedule.interval_minutes": "60",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    settings = client.app.state.settings
    assert settings.get("matching.threshold") == 85
    assert settings.get("apply.mode") == "auto"
    # Checkboxes absent from the payload must become False, not stay True.
    assert settings.get("schedule.do_apply") is False
    assert settings.get("schedule.enabled") is True

    # Values survive a reload from the database (fresh process would do the same).
    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.get("matching.threshold") == 85
    assert settings.get("apply.mode") == "auto"
    assert "85" in client.get("/settings").text


def test_settings_validation_rejects_bad_number(client):
    response = client.post("/actions/settings", data={"matching.threshold": "500"})
    assert response.status_code == 400
    assert "максимум" in response.text
    assert client.app.state.settings.get("matching.threshold") != 500


def test_settings_feed_typed_configs(client):
    client.post(
        "/actions/settings",
        data={
            "browser.headless": "1",
            "llm.standard": "anthropic",
            "llm.model": "gemini-2.5-flash-lite",
            "scroller.max_pages": "7",
        },
        follow_redirects=False,
    )
    settings = client.app.state.settings
    assert settings.browser_config().headless is True
    assert settings.llm_config().standard == "anthropic"
    assert settings.scroller_config().max_pages == 7


def test_secret_is_not_cleared_by_empty_field(client):
    client.post("/actions/settings", data={"llm.api_key": "secret-key"}, follow_redirects=False)
    assert client.app.state.settings.get("llm.api_key") == "secret-key"

    client.post("/actions/settings", data={"llm.api_key": ""}, follow_redirects=False)
    assert client.app.state.settings.get("llm.api_key") == "secret-key"


async def _seed_vacancy(repository, external_id="v1", title="Python developer") -> int:
    from src.db.models import PageCommitParams, SearchRun, VacancyCard

    run_id = "seed-run"
    await repository.create_search_run(SearchRun(id=run_id, task_id=run_id, search_url="https://hh.ru/search"))
    card = VacancyCard(
        external_id=external_id, url=f"https://hh.ru/vacancy/{external_id}", title=title,
        company_name="ООО Ромашка", salary_text="200 000 ₽", city="Москва", snippet="Django, PostgreSQL",
    )
    await repository.commit_page_transaction(
        PageCommitParams(
            search_run_id=run_id, page_key="page_1", page_number=1,
            current_url="https://hh.ru/search", canonical_url="https://hh.ru/search", cards=[card],
        )
    )
    row = await repository._fetch_one("SELECT id FROM vacancies WHERE external_id = ?;", (external_id,))
    return row["id"]


def test_vacancies_page_shows_scores(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        vacancy_id = portal.call(_seed_vacancy, repository)
        resume_id = portal.call(
            repository.upsert_resume,
            Resume(source_url="https://hh.ru/resume/abc", title="Python-разработчик", skills=["Python"]),
        )
        portal.call(repository.set_active_resume, resume_id)
        portal.call(
            repository.upsert_score,
            VacancyScore(vacancy_id=vacancy_id, resume_id=resume_id, score=91,
                         verdict="Полное совпадение по стеку", matched_skills=["Python"]),
        )

    page = client.get("/vacancies")
    assert page.status_code == 200
    assert "Python developer" in page.text
    assert "91" in page.text
    assert "Полное совпадение по стеку" in page.text

    dashboard = client.get("/")
    assert "готовы к отклику" in dashboard.text


def test_apply_without_resume_reports_error_in_panel(client, monkeypatch):
    # A job that fails must surface in the panel, not crash the request.
    response = client.post("/actions/apply")
    assert response.status_code == 200
    assert 'id="task-panel"' in response.text


def test_stop_and_confirm_without_task(client):
    assert "нет выполняющейся задачи" in client.post("/actions/stop").text
    assert "задача не ждёт подтверждения" in client.post("/actions/confirm").text


def test_resume_import_rejects_bad_url(client):
    from anyio.from_thread import start_blocking_portal
    from src.resume.parser import extract_resume_id, is_resume_url

    assert is_resume_url("https://hh.ru/resume/abc123") is True
    assert is_resume_url("https://hh.ru/vacancy/123") is False
    assert extract_resume_id("https://hh.ru/resume/abc123?query=1") == "abc123"

    service_error = None
    with start_blocking_portal() as portal:
        from src.resume.service import ResumeService

        service = ResumeService(client.app.state.repository, client.app.state.settings)
        try:
            portal.call(service.import_from_url, "https://example.com/not-a-resume")
        except ValueError as e:
            service_error = str(e)
    assert "hh.ru/resume" in (service_error or "")


def test_token_guard_blocks_without_token(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "guard.db")
    config.web.token = "s3cret"

    with TestClient(create_app(config)) as guarded:
        assert guarded.get("/").status_code == 401
        assert guarded.get("/static/app.css").status_code == 200
        allowed = guarded.get("/?token=s3cret", follow_redirects=True)
        assert allowed.status_code == 200


def test_search_query_builds_url_and_keeps_other_filters(client):
    """The query now comes from a resume; the URL template still carries filters."""
    settings = client.app.state.settings
    settings._search_url_template = "https://spb.hh.ru/search/vacancy?text=Python&area=2&experience=between1And3"

    url = settings.search_url_for("Go разработчик")
    assert url.startswith("https://spb.hh.ru/search/vacancy?")
    assert "text=Go+%D1%80%D0%B0%D0%B7%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D1%87%D0%B8%D0%BA" in url
    assert "area=2" in url and "experience=between1And3" in url
    assert settings.scroller_config(query="Go разработчик").search_url == url


def test_settings_page_has_no_search_fields(client):
    """Both the raw URL and the query live outside the settings page now."""
    page = client.get("/settings").text
    assert 'name="search.url"' not in page
    assert 'name="search.query"' not in page


def test_legacy_search_url_setting_migrates_to_query(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(
            repository.save_settings,
            {"search.url": "https://hh.ru/search/vacancy?text=Data+Engineer&area=1"},
        )
        portal.call(settings.load)

    # The query is kept to seed a resume that has none of its own yet.
    assert settings.legacy_query == "Data Engineer"
    assert "area=1" in settings.search_url


# --- llm lamp -----------------------------------------------------------

def test_llm_lamp_reports_disabled(client):
    client.post("/actions/settings", data={"llm.enabled": ""}, follow_redirects=False)
    response = client.get("/actions/llm-health")
    assert response.status_code == 200
    assert "llm-status disabled" in response.text
    assert "нейросеть выключена" in response.text


def test_llm_lamp_reports_unreachable_endpoint(client):
    client.post(
        "/actions/settings",
        data={"llm.enabled": "1", "llm.base_url": "http://127.0.0.1:1", "llm.timeout_sec": "5"},
        follow_redirects=False,
    )
    response = client.get("/actions/llm-health?force=1")
    assert "llm-status error" in response.text
    assert "нет подключения" in response.text


def test_refreshed_lamp_does_not_retrigger_itself(client):
    """A returned lamp carrying hx-trigger="load" would loop forever."""
    fragment = client.get("/actions/llm-health").text
    assert "load" not in fragment
    assert "every 10s" in fragment

    # The copy embedded in a page does need the initial load.
    assert "load, every 10s" in client.get("/partials/status").text


def test_compact_lamp_shows_only_model(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(
        ok=True, state="ok", message="есть подключение", model="gemini-2.5-flash-lite", latency_ms=900
    )
    monitor._checked_monotonic = float("inf")

    compact = client.get("/actions/llm-health?compact=1").text
    assert "gemini-2.5-flash-lite" in compact
    assert 'class="llm-text"' not in compact  # no visible message, only the title
    assert 'title="есть подключение"' in compact
    assert "проверить" not in compact
    assert "compact=1" in compact  # keeps polling in compact form

    full = client.get("/actions/llm-health").text
    assert 'class="llm-text"' in full
    assert "есть подключение" in full and "проверить" in full


def test_panel_lamp_is_compact_and_page_lamp_is_not(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(ok=True, state="ok", message="есть подключение", model="gemini-2.5-flash-lite")
    monitor._checked_monotonic = float("inf")

    panel = client.get("/partials/status").text
    assert "llm-status ok compact" in panel
    assert 'class="llm-text"' not in panel  # compact: lamp + model only

    dashboard = client.get("/").text
    assert 'class="llm-text"' in dashboard  # full lamp with the message
    assert "проверить" in dashboard


def test_llm_lamp_is_on_dashboard_and_settings(client):
    assert "llm-status" in client.get("/").text
    assert "llm-status" in client.get("/settings").text


def test_llm_health_is_cached(client):
    monitor = client.app.state.llm_health
    client.get("/actions/llm-health?force=1")
    first = monitor.cached
    client.get("/actions/llm-health")
    assert monitor.cached is first  # served from cache, no second probe


def test_model_field_is_dropdown_when_models_known(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(
        ok=True, state="ok", message="есть подключение",
        models=["gemini-2.5-flash-lite", "gemini-3-flash-preview"],
    )
    monitor._checked_monotonic = float("inf")  # keep the fake result cached

    response = client.get("/actions/llm-models")
    assert "<select" in response.text
    assert 'name="llm.model"' in response.text
    assert "gemini-3-flash-preview" in response.text
    assert "моделей доступно: 2" in response.text


def test_model_field_keeps_custom_value_not_in_list(client):
    from src.llm.health import LLMHealth

    client.post("/actions/settings", data={"llm.model": "my-own-model"}, follow_redirects=False)
    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(ok=True, state="ok", models=["gemini-2.5-flash-lite"])
    monitor._checked_monotonic = float("inf")

    text = client.get("/actions/llm-models").text
    assert "my-own-model — своё значение" in text


def test_model_field_falls_back_to_text_input(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(ok=False, state="error", message="нет подключения: ключ отклонён", models=[])
    monitor._checked_monotonic = float("inf")

    text = client.get("/actions/llm-models").text
    assert "<select" not in text
    assert 'type="text"' in text and 'name="llm.model"' in text
    assert "ключ отклонён" in text


def test_scroll_settings_are_behind_a_spoiler(client):
    page = client.get("/settings").text
    assert "<details" in page and "Тонкая настройка" in page

    # Everyday fields stay visible, the scroll knobs move inside the spoiler.
    before_details = page.split("<details")[0]
    assert 'name="scroller.load_mode"' in before_details
    assert 'name="scroller.max_pages"' not in before_details
    assert 'name="scroller.max_pages"' in page


def test_advanced_fields_still_save(client):
    client.post(
        "/actions/settings",
        data={"scroller.max_pages": "7", "scroller.stable_cycles": "5"},
        follow_redirects=False,
    )
    settings = client.app.state.settings
    assert settings.get("scroller.max_pages") == 7
    assert settings.scroller_config().stable_cycles == 5


def test_spoiler_opens_when_a_value_differs_from_default(client):
    client.post("/actions/settings", data={"scroller.max_pages": "9"}, follow_redirects=False)
    page = client.get("/settings").text
    assert "<details class=\"advanced\" open" in page


def test_runner_radios_are_visually_hidden(client):
    css = client.get("/static/app.css").text
    assert ".runner-option input" in css
    assert "opacity: 0" in css.split(".runner-option input")[1][:200]


def test_settings_page_requests_model_dropdown(client):
    """Every model-typed setting asks for its own dropdown."""
    page = client.get("/settings").text
    assert 'hx-get="/actions/llm-models?field=llm.model"' in page
    assert 'hx-get="/actions/llm-models?field=profile.model"' in page
    assert 'hx-get="/actions/llm-models?field=cover_letter.model"' in page


def test_static_assets_are_versioned_and_revalidated(client):
    page = client.get("/").text
    assert "/static/app.css?v=" in page
    assert "/static/app.js?v=" in page

    response = client.get("/static/app.css")
    assert "no-cache" in response.headers.get("cache-control", "")


# --- runner -------------------------------------------------------------

def test_runner_renders_options_and_start_button(client):
    panel = client.get("/partials/status").text
    for kind in ("collect", "score", "apply", "pipeline", "login"):
        assert f'value="{kind}"' in panel
    assert "Старт" in panel
    assert 'hx-post="/actions/start"' in panel


def test_start_requires_known_kind(client):
    response = client.post("/actions/start", data={"kind": "nonsense"})
    assert "неизвестная задача" in response.text


def test_start_runs_selected_job(client):
    # `score` fails fast without a resume, which is enough to prove routing.
    response = client.post("/actions/start", data={"kind": "score"})
    assert response.status_code == 200
    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        runs = portal.call(client.app.state.repository.list_task_runs, 5)
    assert runs and runs[0]["kind"] == "score"


# --- stopping -----------------------------------------------------------

def test_stop_hook_fires_immediately(client):
    import anyio
    from anyio.from_thread import start_blocking_portal

    from src.db.models import TaskKind

    manager = client.app.state.tasks
    fired = {"value": False}

    async def slow_job(ctx):
        ctx.on_stop(lambda: fired.__setitem__("value", True))
        for _ in range(200):
            ctx.raise_if_stopped()
            await anyio.sleep(0.05)
        return {}

    async def stop_from_loop():
        manager.request_stop()

    with start_blocking_portal() as portal:
        portal.call(manager.start, TaskKind.COLLECT, slow_job)
        portal.call(anyio.sleep, 0.1)
        assert manager.is_busy
        portal.call(stop_from_loop)
        assert fired["value"] is True  # hook ran on the button press, not later
        portal.call(anyio.sleep, 0.3)
        assert not manager.is_busy


def test_second_stop_press_cancels_stuck_job(client):
    import anyio
    from anyio.from_thread import start_blocking_portal

    from src.db.models import TaskKind, TaskStatus

    manager = client.app.state.tasks

    async def stuck_job(ctx):
        # Ignores the stop flag entirely, like a hung browser call.
        await anyio.sleep(30)
        return {}

    async def stop_from_loop():
        manager.request_stop()

    with start_blocking_portal() as portal:
        portal.call(manager.start, TaskKind.COLLECT, stuck_job)
        portal.call(anyio.sleep, 0.1)
        portal.call(stop_from_loop)     # polite
        portal.call(stop_from_loop)     # forced
        portal.call(anyio.sleep, 0.2)
        assert not manager.is_busy
        assert manager.current.status == TaskStatus.CANCELLED


# --- shutdown -----------------------------------------------------------

def test_event_stream_closes_when_server_shuts_down(client):
    """Ctrl+C used to hang forever waiting for the live-log connection."""
    client.app.state.shutting_down = True

    with client.stream("GET", "/actions/events") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "server_closing" in body  # the stream ended by itself


def test_serve_marks_app_as_shutting_down_on_signal(tmp_path, monkeypatch):
    """The signal handler must flip the flag before uvicorn waits for sockets."""
    import asyncio

    import uvicorn

    from src.web import app as web_app

    config = Config.load("configs/config.local.yaml")
    config.db.sqlite_path = str(tmp_path / "serve.db")
    config.web.port = 8099

    captured = {}

    class FakeServer(uvicorn.Server):
        def __init__(self, uvicorn_config):
            super().__init__(uvicorn_config)
            captured["server"] = self
            captured["app"] = uvicorn_config.app
            captured["timeout"] = uvicorn_config.timeout_graceful_shutdown

        async def serve(self, sockets=None):
            return None

        def handle_exit(self, sig, frame):
            captured["exited"] = True

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    asyncio.run(web_app.serve(config))

    server = captured["server"]
    assert captured["timeout"] == web_app.SHUTDOWN_DEADLINE_SECONDS
    assert captured["app"].state.shutting_down is False
    server.handle_exit(2, None)  # the subclass created inside serve()
    assert captured["app"].state.shutting_down is True


def test_task_kinds_cover_all_jobs():
    from src.web import jobs

    assert {TaskKind.COLLECT, TaskKind.SCORE, TaskKind.APPLY, TaskKind.RESUME_IMPORT, TaskKind.LOGIN}
    assert callable(jobs.collect_job) and callable(jobs.score_job) and callable(jobs.apply_job)
    assert callable(jobs.resume_import_job) and callable(jobs.login_job) and callable(jobs.pipeline_job)
