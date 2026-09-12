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
def client(tmp_path, monkeypatch):
    # General UI tests cover a source checkout without optional bundled tools.
    monkeypatch.setenv("JOB_BLESS_AISTUDIO_BUNDLE", str(tmp_path / "missing-bundle"))
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "test.db")
    config.web.token = ""
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_pages_render(client):
    for path in ("/", "/actions", "/vacancies", "/applications", "/resume", "/settings", "/runs"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "job-bless" in response.text


def test_actions_are_the_home_page(client):
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/actions"
    response = client.get("/")
    assert response.url.path == "/actions"
    assert 'id="task-panel"' in response.text
    assert 'id="console-block"' in response.text
    assert 'href="/actions" class="active"' in response.text
    assert 'class="brand" href="/actions"' in response.text
    assert "Обзор" not in response.text


def test_applications_pagination_keeps_filter_and_clamps_page(client):
    from src.db.models import ApplicationStatus, VacancyApplication

    async def seed():
        for i in range(26):
            await client.app.state.repository.record_application(VacancyApplication(
                external_id=f"pagination-{i}", vacancy_url=f"https://hh.ru/vacancy/{i}",
                status=ApplicationStatus.FAILED,
            ))
        await client.app.state.repository.record_application(VacancyApplication(
            external_id="sent-example", vacancy_url="https://hh.ru/vacancy/sent",
            status=ApplicationStatus.APPLIED,
        ))

    client.portal.call(seed)
    first = client.get("/applications?status=failed").text
    assert first.count('<article class="history-item"') == 25
    assert '?page=2&amp;status=failed' in first
    last = client.get("/applications?status=failed&page=999").text
    assert last.count('<article class="history-item"') == 1
    assert 'aria-current="page">2</span>' in last
    assert "sent-example" not in last


def test_runs_preserve_nested_results_and_escape_text(client):
    from src.db.models import TaskRun, TaskStatus

    repository = client.app.state.repository
    client.portal.call(repository.create_task_run, TaskRun(
        id="nested-run", kind=TaskKind.COLLECT, params={"action": "pipeline"},
    ))
    client.portal.call(repository.finish_task_run, "nested-run", TaskStatus.COMPLETED,
                       {"collect": {"unique": 42}, "logged_in": False,
                        "future_field": "<script>alert(1)</script>"})
    response = client.get("/runs")
    assert response.status_code == 200
    assert "Полный цикл" in response.text
    assert "Вакансий собрано" in response.text
    assert "42" in response.text
    assert "Нет" in response.text
    assert "future_field" in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_task_panel_partial(client):
    response = client.get("/partials/status")
    assert response.status_code == 200
    assert 'id="task-panel"' in response.text


@pytest.mark.parametrize("name", ["", "Анна Петрова", "<script>alert(1)</script>"])
def test_confirmed_account_persists_in_panel_without_resume(client, name):
    from html import escape
    from src.db.models import TaskRun, TaskStatus

    repository = client.app.state.repository
    client.portal.call(repository.create_task_run, TaskRun(id="account-login", kind=TaskKind.LOGIN))
    result = {"logged_in": True, "account_name": name} if name else {"logged_in": True}
    client.portal.call(repository.finish_task_run, "account-login", TaskStatus.COMPLETED, result)
    for path in ("/actions", "/partials/status"):
        panel = client.get(path).text
        assert "Вы вошли в hh.ru" in panel
        assert ">Сменить аккаунт</button>" in panel
        assert '"switch_account": "1"' in panel
        assert escape(name) in panel
        assert "<script>alert(1)</script>" not in panel


def test_unconfirmed_new_login_hides_previous_account(client):
    from src.db.models import TaskRun, TaskStatus

    repository = client.app.state.repository
    client.portal.call(repository.create_task_run, TaskRun(id="old-account", kind=TaskKind.LOGIN))
    client.portal.call(repository.finish_task_run, "old-account", TaskStatus.COMPLETED,
                       {"logged_in": True, "account_name": "Предыдущий Аккаунт"})
    client.portal.call(repository.create_task_run, TaskRun(id="cancelled-login", kind=TaskKind.LOGIN))
    client.portal.call(repository.finish_task_run, "cancelled-login", TaskStatus.CANCELLED)
    panel = client.get("/partials/status").text
    assert "Предыдущий Аккаунт" not in panel
    assert "Вы вошли в hh.ru" not in panel
    assert ">Войти в hh.ru</button>" in panel


def test_switch_account_endpoint_passes_explicit_intent(client, monkeypatch):
    from src.web import jobs

    async def login(ctx):
        return {"logged_in": True, "account_name": "Другой Аккаунт"}

    monkeypatch.setattr(jobs, "login_job", login)
    client.post("/actions/login", data={"switch_account": "1"})
    manager = client.app.state.tasks
    assert manager.current.params["switch_account"] is True

    async def wait_finished():
        await manager.lane().task

    client.portal.call(wait_finished)
    assert "Другой Аккаунт" in client.get("/partials/status").text


@pytest.mark.parametrize('status', ['running', 'cancelled', 'failed'])
def test_isolated_account_switch_keeps_previous_account_visible(client, status):
    from src.db.models import TaskRun, TaskStatus
    repository = client.app.state.repository
    previous = {'logged_in': True, 'name': 'Прежний Аккаунт', 'confirmed_at': '2026-09-08T10:00:00'}
    client.portal.call(repository.create_task_run, TaskRun(
        id='isolated-switch', kind=TaskKind.LOGIN,
        params={'switch_account': True, 'previous_account': previous},
    ))
    if status != 'running':
        client.portal.call(repository.finish_task_run, 'isolated-switch', TaskStatus(status))
    panel = client.get('/partials/status').text
    assert 'Прежний Аккаунт' in panel and 'Вы вошли в hh.ru' in panel
    assert 'hx-confirm="Сменить аккаунт hh.ru?' in panel


def test_stopping_import_after_verified_login_keeps_new_account(client):
    from src.db.models import TaskRun, TaskStatus
    repository = client.app.state.repository
    client.portal.call(repository.create_task_run, TaskRun(
        id='verified-switch', kind=TaskKind.LOGIN, params={'switch_account': True},
    ))
    client.portal.call(repository.finish_task_run, 'verified-switch', TaskStatus.CANCELLED,
                       {'logged_in': True, 'account_name': 'Новый Аккаунт', 'account_verified': True})
    panel = client.get('/partials/status').text
    assert 'Новый Аккаунт' in panel and 'Вы вошли в hh.ru' in panel

def test_settings_roundtrip(client):
    response = client.post(
        "/actions/settings",
        data={
            "matching.threshold": "85",
            "ratelimit.enabled": "1",
            "ratelimit.requests_per_minute": "60",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"

    settings = client.app.state.settings
    assert settings.get("matching.threshold") == 85
    assert settings.get("ratelimit.requests_per_minute") == 60
    # Checkboxes absent from the payload must become False, not stay True.
    assert settings.get("schedule.do_apply") is False
    assert settings.get("ratelimit.enabled") is True
    assert settings.get("llm.enabled") is False

    # Values survive a reload from the database (fresh process would do the same).
    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.get("matching.threshold") == 85
    assert settings.get("ratelimit.requests_per_minute") == 60
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
            "browser.close_stale_tabs": "1",
            "llm.standard": "anthropic",
            "llm.model": "gemini-2.5-flash-lite",
        },
        follow_redirects=False,
    )
    settings = client.app.state.settings
    assert settings.browser_config().close_stale_tabs is True
    assert settings.llm_config().standard == "anthropic"
    client.post("/actions/search-settings", data={"scroller.max_scroll_steps_per_page": "7"})
    assert settings.scroller_config().max_scroll_steps_per_page == 7


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


def test_resume_page_offers_account_import_in_heading(client, monkeypatch):
    from unittest.mock import AsyncMock
    from src.web.tasks import TaskBusyError

    resume_id = client.portal.call(client.app.state.repository.upsert_resume,
                                  Resume(source_url="https://hh.ru/resume/one", title="Engineer"))
    page = client.get("/resume").text
    assert 'name="resume_url"' not in page
    assert 'Обновить все резюме' in page
    heading = page.split('<header class="page-heading"')[1].split('</header>')[0]
    assert 'Обновить все резюме' in heading
    assert f'action="/actions/resume/{resume_id}/refresh"' not in page
    start = AsyncMock()
    monkeypatch.setattr(client.app.state.tasks, "start", start)
    response = client.post("/actions/resume/import", follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/resume'
    assert start.call_args.args[0] == TaskKind.RESUME_IMPORT
    response = client.post(f"/actions/resume/{resume_id}/refresh", follow_redirects=False)
    assert response.status_code == 303
    assert start.call_args.kwargs['params']['resume_id'] == resume_id
    assert client.post('/actions/resume/999/refresh').status_code == 404
    start.side_effect = TaskBusyError('Задача выполняется')
    assert 'Задача выполняется' in client.post('/actions/resume/import').text


def test_resume_card_shows_confirmed_skills_without_city_salary_or_raw_text(client):
    client.portal.call(client.app.state.repository.upsert_resume, Resume(
        source_url='https://hh.ru/resume/verified', title='Engineer',
        city='Legacy city', salary_text='Legacy salary', skills=['Docker', 'Python'],
        verified_skills=['Docker'], raw_text='Private raw fallback',
    ))
    page = client.get('/resume').text
    assert '✓ Docker' in page and '✓ Python' not in page
    assert 'Подтверждено на hh.ru' in page
    assert 'Весь распознанный текст' not in page and 'Private raw fallback' not in page
    assert 'Legacy city' not in page and 'Legacy salary' not in page


def test_resume_auto_import_for_existing_login_runs_once(client, monkeypatch):
    from unittest.mock import AsyncMock
    from src.db.models import TaskRun, TaskStatus

    repo = client.app.state.repository
    client.portal.call(repo.create_task_run, TaskRun(id='old-login', kind=TaskKind.LOGIN))
    client.portal.call(repo.finish_task_run, 'old-login', TaskStatus.COMPLETED, {'logged_in': True}, '')
    start = AsyncMock()
    monkeypatch.setattr(client.app.state.tasks, 'start', start)
    client.get('/resume')
    client.get('/resume')
    start.assert_awaited_once()
    assert start.call_args.kwargs['trigger'] == 'auto'


def test_resume_page_does_not_repeat_import_completed_during_login(client, monkeypatch):
    from unittest.mock import AsyncMock
    from src.db.models import TaskRun, TaskStatus

    repo = client.app.state.repository
    client.portal.call(repo.create_task_run, TaskRun(id='new-login', kind=TaskKind.LOGIN))
    client.portal.call(repo.finish_task_run, 'new-login', TaskStatus.COMPLETED,
                       {'logged_in': True, 'resume_import': {'found': 0, 'imported': 0}}, '')
    start = AsyncMock()
    monkeypatch.setattr(client.app.state.tasks, 'start', start)
    client.get('/resume')
    start.assert_not_awaited()


def test_token_guard_blocks_without_token(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "guard.db")
    config.web.token = "s3cret"

    with TestClient(create_app(config)) as guarded:
        assert guarded.get("/", follow_redirects=False).headers["location"] == "/login"
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
    assert "load, every 10s" in client.get("/actions").text


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


def test_panel_has_action_settings_buttons_and_llm_health_status(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    monitor._health = LLMHealth(ok=True, state="ok", message="есть подключение", model="gemini-2.5-flash-lite")
    monitor._checked_monotonic = float("inf")

    panel = client.get("/partials/status").text
    assert 'aria-label="Настроить оценку вакансий"' in panel
    assert 'aria-label="Настроить описание опыта"' not in panel
    assert "search-model" not in panel
    assert "llm-status ok" in panel

    settings = client.get("/actions/llm-settings").text
    assert 'class="llm-text"' in settings  # full lamp with the message
    assert "проверить" in settings


def test_llm_settings_moved_to_actions(client):
    assert "llm-status" in client.get("/actions").text
    assert 'name="llm.enabled"' not in client.get("/settings").text


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
    page = client.get("/actions/search-settings").text
    assert "<details" in page and "Тонкая настройка" in page

    # Everyday fields sit inside the section, scroll knobs in its nested spoiler.
    before_advanced = page.split('<details class="advanced">', 1)[0]
    assert 'name="scroller.load_mode"' in before_advanced
    assert 'name="scroller.max_scroll_steps_per_page"' not in before_advanced
    assert 'name="scroller.max_scroll_steps_per_page"' in page


def test_advanced_fields_still_save(client):
    client.post(
        "/actions/search-settings",
        data={"scroller.max_scroll_steps_per_page": "7", "scroller.stable_cycles": "5"},
        follow_redirects=False,
    )
    settings = client.app.state.settings
    assert settings.get("scroller.max_scroll_steps_per_page") == 7
    assert settings.scroller_config().stable_cycles == 5


def test_settings_stay_collapsed_when_a_value_differs_from_default(client):
    import re

    client.post("/actions/settings", data={"ratelimit.requests_per_minute": "90"}, follow_redirects=False)
    page = client.get("/settings").text
    details = re.findall(r'<details\b[^>]*>', page)
    assert details and all(' open' not in tag for tag in details)
    assert 'name="ratelimit.requests_per_minute" value="90"' in page


def _stage_button(panel, kind):
    import re

    match = re.search(r'<button\b[^>]*hx-post="/actions/' + kind + r'"[^>]*>', panel)
    if kind == "collect":
        match = re.search(r'<button\b[^>]*form="inline-search-form"[^>]*>', panel)
    assert match, f"Missing stage button: {kind}"
    return match.group()


def test_new_user_can_collect_but_needs_resume_for_scoring_and_applying(client):
    import re

    panel = client.get("/partials/status").text
    assert 'href="/resume">Перейти к резюме</a>' in panel
    assert panel.count('id="necessary-settings"') == 1
    assert 'resume-banner' not in panel
    assert panel.count('data-action-id=') == 6
    assert "Начните с вашего резюме" not in panel
    assert 'aria-label="Настроить оценку вакансий"' in panel
    assert "Искать вакансии" in panel
    assert "Оценить соответствие вакансий резюме" in panel
    assert "disabled" not in _stage_button(panel, "collect")
    for kind in ("score", "apply"):
        assert "disabled" in _stage_button(panel, kind)
        assert f'aria-describedby="{kind}-context"' in _stage_button(panel, kind)
    assert "Искать вакансии" in panel
    assert 'hx-get="/actions/search-settings"' in panel
    assert 'id="panel-search-query"' in panel
    modal = client.get("/actions/search-settings").text
    query = re.search(r'<input\b[^>]*id="panel-search-query"[^>]*>', panel)
    assert query and "disabled" not in query.group()
    assert 'label for="panel-search-query">Какую работу ищете</label>' in panel
    assert 'name="search_query"' not in modal
    assert 'type="radio"' not in panel


def test_settings_page_requests_model_dropdown(client):
    """Every model-typed setting asks for its own dropdown."""
    page = client.get("/actions/llm-settings").text
    assert 'hx-get="/actions/llm-models?field=llm.model"' in page
    assert 'hx-get="/actions/llm-models?field=profile.model"' in client.get('/actions/profile-settings').text
    assert 'hx-get="/actions/llm-models?field=cover_letter.model"' in client.get('/actions/apply-settings').text


def test_static_assets_are_versioned_and_revalidated(client):
    page = client.get("/").text
    assert "/static/app.css?v=" in page
    assert "/static/app.js?v=" in page

    response = client.get("/static/app.css")
    assert "no-cache" in response.headers.get("cache-control", "")


# --- runner -------------------------------------------------------------

def test_resume_unlocks_actions_and_explains_missing_setup(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        resume_id = portal.call(repository.upsert_resume, Resume(source_url="https://hh.ru/resume/qa", title="Разработчик"))
        portal.call(repository.set_active_resume, resume_id)

    panel = client.get("/partials/status").text
    assert "Укажите должность в карточке «Искать вакансии»." in panel
    assert "Настроить оценку вакансий" in panel
    assert "disabled" not in _stage_button(panel, "collect")
    assert "disabled" in _stage_button(panel, "score")
    assert "disabled" not in _stage_button(panel, "apply")

    with start_blocking_portal() as portal:
        portal.call(repository.update_resume_fields, resume_id, "Python", "")
        portal.call(client.app.state.settings.save, {"llm.enabled": True, "matching.threshold": 85, "apply.batch_limit": 3})

    for path in ("/partials/status", "/actions"):
        panel = client.get(path).text
        for kind in ("collect", "score", "apply"):
            assert "disabled" not in _stage_button(panel, kind)
        assert 'value="Python"' in panel
        assert "До 3 откликов" in panel
        assert "оценка от 85" in panel


def test_panel_query_edit_preserves_resume_context(client):
    from anyio.from_thread import start_blocking_portal

    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        resume_id = portal.call(repository.upsert_resume, Resume(
            source_url="https://hh.ru/resume/query-test", title="Разработчик",
            context_text="Опыт разработки на Python", search_query="Python",
        ))
        portal.call(repository.set_active_resume, resume_id)

    modal = client.get("/actions/search-settings").text
    assert 'value="Python"' in client.get('/partials/status').text
    assert 'hx-post="/actions/search-settings"' in modal
    assert f'name="resume_id" value="{resume_id}"' in modal

    response = client.post("/actions/search-settings", data={"resume_id": str(resume_id), "search_query": "  Backend Python  ", "scroller.max_scroll_steps_per_page": "7"})
    assert response.status_code == 200
    assert response.headers["HX-Trigger-After-Settle"] == "searchSettingsSaved"
    assert client.app.state.settings.scroller_config().max_scroll_steps_per_page == 7
    assert 'value="Backend Python"' in response.text
    assert "disabled" not in _stage_button(response.text, "collect")
    with start_blocking_portal() as portal:
        saved = portal.call(repository.get_resume, resume_id)
    assert saved.search_query == "Backend Python"
    assert saved.context_text == "Опыт разработки на Python"

    response = client.post(f"/actions/resume/{resume_id}/search-query", data={"search_query": " "})
    assert "disabled" not in _stage_button(response.text, "collect")
    response = client.post(f"/actions/resume/{resume_id + 1}/search-query", data={"search_query": "Java"})
    assert "Активное резюме изменилось" in response.text


def test_pipeline_dialog_matches_effective_settings(client):
    from anyio.from_thread import start_blocking_portal

    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(settings.save, {"schedule.do_collect": True, "schedule.do_apply": True, "apply.mode": "manual", "matching.enabled": False})
    modal = client.get("/actions/pipeline-settings").text
    assert modal.count(" checked") == 1

    with start_blocking_portal() as portal:
        portal.call(settings.save, {"schedule.do_collect": True, "schedule.do_apply": True, "apply.mode": "auto", "matching.enabled": False})
    modal = client.get("/actions/pipeline-settings").text
    assert modal.count(" checked") == 2


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
    monkeypatch.setattr(web_app, "prepare_browser", lambda config: None)
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


def test_pipeline_modal_saves_only_its_settings(client):
    from anyio.from_thread import start_blocking_portal

    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post('/actions/pipeline-settings', data={
        'schedule.do_collect': '1', 'schedule.do_score': '1', 'schedule.do_apply': '1',
        'schedule.enabled': '1', 'schedule.interval_minutes': '60',
        'llm.enabled': '1',  # unrelated input is ignored
    })
    assert response.status_code == 200
    assert response.headers['HX-Retarget'] == '#task-panel'
    assert 'pipelineSettingsSaved' in response.headers['HX-Trigger-After-Settle']
    changed_keys = {'schedule.do_collect', 'schedule.do_score', 'schedule.do_apply', 'matching.enabled', 'apply.mode', 'schedule.enabled', 'schedule.interval_minutes'}
    for key, value in before.items():
        if key not in changed_keys:
            assert settings.get(key) == value, key
    assert settings.get('matching.enabled') is True
    assert settings.get('apply.mode') == 'auto'
    assert not client.app.state.tasks.is_busy
    assert client.get('/actions/pipeline-settings').text.count(' checked') == 4
    assert client.app.state.scheduler.entries[0].next_run_at is not None

    # The full settings form no longer owns the stage flags.
    client.post('/actions/settings', data={'matching.threshold': '85'})
    for key in ('schedule.do_collect', 'schedule.do_score', 'schedule.do_apply'):
        assert settings.get(key) is True

    # Turning off every stage persists; no old checkbox value sneaks back in.
    client.post('/actions/pipeline-settings', data={})
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    for key in ('schedule.do_collect', 'schedule.do_score', 'schedule.do_apply'):
        assert settings.get(key) is False
    assert 'disabled' in _stage_button(client.get('/partials/status').text, 'pipeline')


def test_stage_controls_moved_out_of_settings_and_tools_are_visible(client):
    panel = client.get('/partials/status').text
    main, tools = panel.split('id="search-tools"', 1)
    assert 'Выполнить несколько действий подряд' in main
    assert 'Настроить действия' in main
    assert '<details' not in panel
    assert 'data-action-id="login"' in main
    assert 'data-action-id="login"' not in tools
    page = client.get('/settings').text
    assert 'id="pipeline-dialog"' not in page
    assert 'id="pipeline-dialog"' in client.get("/actions").text
    for key in ('schedule.do_collect', 'schedule.do_score', 'schedule.do_apply'):
        assert f'name="{key}"' not in page
        assert f'name="{key}"' in client.get('/actions/pipeline-settings').text


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_dismiss_finished_task_preserves_history(client, status):
    from anyio.from_thread import start_blocking_portal
    from src.db.models import TaskRun, TaskStatus
    from src.web.tasks import TaskState

    manager = client.app.state.tasks
    state = TaskState(id="dismiss-qa", kind=TaskKind.LOGIN, status=TaskStatus(status), error_message="Test failure")
    manager.lane().current = state
    manager.history.append(state)
    with start_blocking_portal() as portal:
        portal.call(manager.repository.create_task_run, TaskRun(id=state.id, kind=state.kind))
        portal.call(manager.repository.finish_task_run, state.id, state.status, {}, state.error_message)
    assert '>Скрыть</button>' in client.get('/partials/status').text
    # An old page cannot dismiss a different task's message.
    client.post('/actions/dismiss', data={'task_id': 'old-task'})
    assert manager.current is state
    response = client.post('/actions/dismiss', data={'task_id': state.id})
    assert response.status_code == 200
    assert manager.current is None
    assert 'dismiss-task' not in response.text
    assert manager.history == [state]
    with start_blocking_portal() as portal:
        runs = portal.call(manager.repository.list_task_runs)
    assert runs[0]['id'] == state.id and runs[0]['status'] == status
    assert runs[0]['error_message'] == 'Test failure'
    assert 'Test failure' in client.get('/runs').text


def test_cannot_dismiss_running_task(client):
    from src.web.tasks import TaskState

    manager = client.app.state.tasks
    state = TaskState(id="running-qa", kind=TaskKind.LOGIN)
    manager.lane().current = state
    assert '>Скрыть</button>' not in client.get('/partials/status').text
    client.post('/actions/dismiss', data={'task_id': state.id})
    assert manager.current is state


def test_action_panel_and_console_only_appear_on_actions_page(client):
    for path in ("/vacancies", "/applications", "/resume", "/settings", "/runs"):
        page = client.get(path).text
        assert 'href="/actions"' in page
        for element_id in ("task-panel", "console-block", "pipeline-dialog", "search-dialog"):
            assert f'id="{element_id}"' not in page
    page = client.get('/actions').text
    for element_id in ("task-panel", "console-block", "pipeline-dialog", "search-dialog"):
        assert f'id="{element_id}"' in page
    assert 'href="/actions" class="active"' in page


def test_selected_vacancies_start_and_redirect_to_actions(client, monkeypatch):
    from unittest.mock import AsyncMock
    from src.web import jobs

    job = AsyncMock(return_value={})
    monkeypatch.setattr(jobs, 'apply_job', job)
    response = client.post('/actions/apply', data={'return_to': 'actions', 'vacancy_ids': ['12', '15']}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/actions'
    assert client.app.state.tasks.current.params['vacancy_ids'] == [12, 15]
    page = client.get('/vacancies').text
    assert 'hx-target="#task-panel"' not in page


@pytest.mark.parametrize("action,lane,kind", [
    ("collect", "main", TaskKind.COLLECT), ("score", "main", TaskKind.SCORE),
    ("apply", "main", TaskKind.APPLY), ("pipeline", "main", TaskKind.COLLECT),
    ("login", "main", TaskKind.LOGIN), ("resume_touch", "main", TaskKind.RESUME_TOUCH),
])
def test_running_card_switches_play_to_stop_in_its_lane(client, action, lane, kind):
    import asyncio
    import re
    from anyio.from_thread import start_blocking_portal

    manager = client.app.state.tasks

    async def idle_job(ctx):
        while not ctx.should_stop():
            await asyncio.sleep(.01)
        return {}

    async def start():
        await manager.start(kind, idle_job, lane=lane, params={"action": action})

    async def wait_finished():
        await asyncio.wait_for(manager.lane(lane).task, 2)

    def card(html, name):
        return re.search(r'<article[^>]*data-action-id="' + name + r'"[^>]*>(.*?)</article>', html, re.S).group()

    with start_blocking_portal() as portal:
        portal.call(start)
        panel = client.get('/partials/status').text
        active = card(panel, action)
        assert 'is-running' in active
        assert 'is-stop' in active
        assert 'hx-post="/actions/stop"' in active
        assert f'"lane": "{lane}"' in active
        assert 'aria-label="Остановить:' in active
        if action == 'pipeline':
            assert 'is-stop' not in card(panel, 'collect')
        if lane != 'main':
            assert ('disabled' in _stage_button(card(panel, 'login'), 'login')) == (lane == 'activity')
        client.post('/actions/stop', data={'lane': lane})
        portal.call(wait_finished)
    stopped = card(client.get('/partials/status').text, action)
    assert 'is-running' not in stopped
    assert 'is-stop' not in stopped
    assert 'class="action-control"' in stopped


def test_apply_settings_modal_persists_only_reply_settings(client):
    from anyio.from_thread import start_blocking_portal
    from src.web.routes.actions import APPLY_SETTING_KEYS

    page = client.get('/actions').text
    assert 'aria-label="Настроить отклики"' in page
    assert 'id="apply-dialog"' in page
    modal = client.get('/actions/apply-settings').text
    for key in APPLY_SETTING_KEYS:
        assert f'name="{key}"' in modal
    assert 'name="apply.skip_questions"' not in modal
    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post('/actions/apply-settings', data={
        'matching.threshold': '84', 'apply.batch_limit': '12', 'apply.delay_sec': '3.5',
        'apply.recheck_with_llm': '1', 'apply.mode': 'auto', 'cover_letter.enabled': '1',
        'cover_letter.when': 'always', 'cover_letter.model': 'letter-test',
        'cover_letter.max_chars': '900', 'cover_letter.prompt': 'Кратко',
        'cover_letter.fallback_text': 'Здравствуйте!', 'llm.enabled': '1',
        'schedule.do_apply': '1',
    })
    assert response.headers['HX-Trigger-After-Settle'] == 'applySettingsSaved'
    assert response.headers['HX-Retarget'] == '#task-panel'
    assert not client.app.state.tasks.is_busy
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.get('matching.threshold') == 84
    assert settings.get('apply.batch_limit') == 12
    assert settings.get('apply.delay_sec') == 3.5
    assert settings.get('apply.recheck_with_llm') is True
    assert settings.cover_letter_config().when == 'always'
    assert settings.cover_letter_config().fallback_text == 'Здравствуйте!'
    for key, value in before.items():
        if key not in APPLY_SETTING_KEYS:
            assert settings.get(key) == value
    assert 'value="84"' in client.get('/settings').text
    assert 'value="84"' in client.get('/actions/apply-settings').text
    client.post('/actions/apply-settings', data={'matching.threshold': '84'})
    assert settings.get('cover_letter.enabled') is False
    assert settings.get('apply.recheck_with_llm') is False


def test_invalid_reply_settings_preserve_input_and_change_nothing(client):
    from anyio.from_thread import start_blocking_portal

    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post('/actions/apply-settings', data={
        'matching.threshold': '84', 'apply.batch_limit': '999',
        'cover_letter.prompt': 'Мой текст',
    })
    assert 'максимум' in response.text
    assert 'value="999"' in response.text
    assert 'Мой текст' in response.text
    assert 'HX-Trigger-After-Settle' not in response.headers
    assert settings.all_values() == before
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.all_values() == before



def test_empty_vacancy_selection_never_starts_automatic_batch(client):
    response = client.post('/actions/apply', data={'return_to': 'actions'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/vacancies'
    assert client.app.state.tasks.current is None


def test_vacancy_empty_states_distinguish_filters(client):
    empty = client.get('/vacancies').text
    assert 'Пока нет сохранённых вакансий' in empty
    assert 'id="apply-selected"' not in empty
    filtered = client.get('/vacancies?search=nonexistent').text
    assert 'Нет вакансий по этим фильтрам' in filtered
    assert 'Сбросить фильтры' in filtered


def test_vacancy_pagination_preserves_all_filters(client, monkeypatch):
    import html
    import re
    from urllib.parse import parse_qs, urlparse
    from unittest.mock import AsyncMock

    monkeypatch.setattr(client.app.state.repository, 'count_vacancies', AsyncMock(return_value=30))
    monkeypatch.setattr(client.app.state.repository, 'list_vacancies', AsyncMock(return_value=[]))
    params = {'search': 'Python & Go #1', 'found_for_resume': '7', 'min_score': '60',
              'only_scored': '1', 'only_unapplied': '1', 'order': 'recent'}
    response = client.get('/vacancies', params=params)
    href = re.search(r'href="([^"]+)" aria-label="Страница 2"', response.text).group(1)
    query = parse_qs(urlparse(html.unescape(href)).query)
    assert query == {**{key: [value] for key, value in params.items()}, 'page': ['2']}
