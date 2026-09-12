"""Standalone collection and a complete, filtered download without live hh.ru."""

import asyncio
import csv
import io
import re
from html import unescape
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from src.config import Config
from src.db.models import (
    ApplicationStatus, PageCommitParams, Resume, SearchRun, TaskKind,
    VacancyApplication, VacancyCard, VacancyScore, VacancyDetails,
)
from src.web import jobs
from src.web.app import create_app
from src.web.tasks import TaskContext, TaskState


@pytest.fixture(autouse=True)
def detail_reader(monkeypatch):
    class Reader:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        async def read(self, card):
            return VacancyDetails(full_description='Full vacancy', fetched_at='2026-09-11T10:00:00+00:00')
    monkeypatch.setattr(jobs, 'VacancyDetailsReader', Reader)


@pytest.fixture
def client(tmp_path):
    config = Config.load("configs/config.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "test.db")
    config.web.token = ""
    with TestClient(create_app(config)) as client:
        yield client


def test_standalone_query_persists_and_empty_query_stays_empty(client):
    settings = client.app.state.settings
    response = client.post("/actions/search-settings", data={
        "search_query": "  Аналитик & SQL  ", "scroller.max_scroll_steps_per_page": "3",
    })
    assert "HX-Trigger-After-Settle" in response.headers
    client.portal.call(settings.load)
    assert settings.search_query == "Аналитик & SQL"
    assert parse_qs(urlparse(settings.scroller_config().search_url).query)["text"] == [settings.search_query]
    assert settings.get("scroller.max_scroll_steps_per_page") == 3
    assert "Аналитик &amp; SQL" in client.get("/").text
    client.post("/actions/search-settings", data={"search_query": " "})
    client.portal.call(settings.load)
    assert settings.search_query == ""
    panel = client.get("/actions").text
    assert "required" in re.search(r'<input[^>]*id="panel-search-query"[^>]*>', panel).group()


def test_invalid_settings_keep_query_and_stale_form_cannot_change_resume(client):
    settings = client.app.state.settings
    original = settings.search_query
    response = client.post("/actions/search-settings", data={
        "search_query": "Новый запрос", "scroller.max_scroll_steps_per_page": "-1",
    })
    assert "HX-Trigger-After-Settle" not in response.headers
    assert 'name="search_query"' not in response.text
    assert settings.search_query == original
    repo = client.app.state.repository
    resume_id = client.portal.call(repo.upsert_resume, Resume(
        source_url="https://hh.ru/resume/one", search_query="Java",
    ))
    client.portal.call(repo.set_active_resume, resume_id)
    response = client.post("/actions/search-settings", data={"search_query": "Python"})
    assert "Активное резюме изменилось" in response.text
    assert client.portal.call(repo.get_active_resume).search_query == "Java"
    assert settings.search_query == original


def test_select_standalone_keeps_resume_and_independent_queries(client):
    repo, settings = client.app.state.repository, client.app.state.settings
    rid = client.portal.call(repo.upsert_resume, Resume(
        source_url='https://hh.ru/resume/saved', search_query='Java', context_text='Saved experience',
    ))
    client.portal.call(repo.set_active_resume, rid)
    client.portal.call(settings.save_search_query, 'Аналитик')
    response = client.post('/actions/resume/select', data={'resume_id': 0})
    assert 'value="0" selected>Без резюме' in response.text
    assert 'value="Аналитик"' in response.text
    assert client.portal.call(repo.get_active_resume) is None
    assert 'Выбран поиск без резюме' in client.get('/resume').text
    # A search form opened before the switch cannot overwrite the other query.
    stale = client.post('/actions/search-settings', data={'resume_id': rid, 'search_query': 'Stale'})
    assert 'Активное резюме изменилось' in stale.text
    client.post('/actions/search-settings', data={'search_query': 'SQL'})
    client.portal.call(settings.load)
    assert settings.search_query == 'SQL'
    saved = client.portal.call(repo.get_resume, rid)
    assert (saved.search_query, saved.context_text) == ('Java', 'Saved experience')
    # Both selection controls can restore the saved resume.
    response = client.post(f'/actions/resume/{rid}/activate')
    assert response.status_code == 200
    assert client.portal.call(repo.get_active_resume).id == rid
    response = client.post('/actions/resume/select', data={'resume_id': 0})
    assert 'value="SQL"' in response.text


@pytest.mark.parametrize("with_resume", [False, True])
def test_collection_saves_cards_with_optional_resume(client, monkeypatch, with_resume):
    repo = client.app.state.repository
    settings = client.app.state.settings
    client.portal.call(settings.save_search_query, "Аналитик")
    resume_id = None
    if with_resume:
        resume_id = client.portal.call(repo.upsert_resume, Resume(
            source_url="https://hh.ru/resume/one", search_query="Java",
        ))
        client.portal.call(repo.set_active_resume, resume_id)
    browser = AsyncMock(return_value=settings.browser_config())
    monkeypatch.setattr(jobs, "ensure_browser", browser)
    seen = {}

    async def collect(self, **kwargs):
        seen.update(kwargs)
        yield PageCommitParams(
            search_run_id=kwargs["task_id"], page_key="1", page_number=1,
            current_url=kwargs["search_url"], canonical_url=kwargs["search_url"],
            cards=[VacancyCard(external_id="42", title="Аналитик", url="https://hh.ru/vacancy/42")],
        )

    monkeypatch.setattr(jobs.HHVacancyCardCollector, "collect", collect)
    manager = client.app.state.tasks
    ctx = TaskContext(manager, TaskState(id="test-collect", kind=TaskKind.COLLECT), manager.lane())
    client.portal.call(jobs.collect_job, ctx)
    assert parse_qs(urlparse(seen["search_url"]).query)["text"] == ["Java" if with_resume else "Аналитик"]
    assert client.portal.call(repo.count_vacancies) == 1
    assert ctx.state.done == 1 and ctx.state.total == 0

    async def read_run():
        async with repo.connection.execute("SELECT resume_id, status FROM search_runs") as cursor:
            return await cursor.fetchone()

    saved = client.portal.call(read_run)
    assert saved[0] == resume_id
    assert saved[1] == "completed"


def test_empty_standalone_query_fails_before_launching_browser(client, monkeypatch):
    manager = client.app.state.tasks
    client.portal.call(manager.settings.save_search_query, "")
    browser = AsyncMock()
    monkeypatch.setattr(jobs, "ensure_browser", browser)
    ctx = TaskContext(manager, TaskState(id="empty", kind=TaskKind.COLLECT), manager.lane())
    with pytest.raises(ValueError, match="поисковый запрос"):
        client.portal.call(jobs.collect_job, ctx)
    browser.assert_not_called()


def test_collect_only_pipeline_available_without_resume(client):
    client.post("/actions/pipeline-settings", data={"schedule.do_collect": "1"})
    panel = client.get("/actions").text
    button = re.search(r'<button[^>]*hx-post="/actions/pipeline"[^>]*>', panel).group()
    assert "disabled" not in button


def _seed(client, resume_id=None):
    async def seed():
        repo = client.app.state.repository
        await repo.create_search_run(SearchRun(id="export", task_id="export", search_url="url", resume_id=resume_id))
        await repo.commit_page_transaction(PageCommitParams(
            search_run_id="export", page_key="1", page_number=1, current_url="url", canonical_url="url",
            cards=[VacancyCard(
                external_id=str(i), title=f"Аналитик {i}", company_name='Компания; "Тест"',
                url=f"https://hh.ru/vacancy/{i}", salary_from=i, salary_text=f"{i} руб.",
                raw_text="Первая строка\nВторая; строка", snippet="=HYPERLINK(\"example\")",
                skills=["SQL", "Python"],
            ) for i in range(31)],
        ))
        return await repo.list_vacancies(limit=None)
    return client.portal.call(seed)


def _download(client, **params):
    response = client.get("/vacancies/export", params=params)
    assert response.status_code == 200
    assert "attachment; filename=" in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))


def test_csv_downloads_all_pages_and_preserves_cyrillic_multiline_text(client):
    _seed(client)
    rows = _download(client, order="salary", page=2)
    assert len(rows) == 31
    assert [row["ID hh.ru"] for row in rows] == [str(i) for i in reversed(range(31))]
    assert rows[0]["Компания"] == 'Компания; "Тест"'
    assert rows[0]["Текст карточки"] == "Первая строка\nВторая; строка"
    assert rows[0]["Описание карточки"].startswith("'=HYPERLINK")
    assert rows[0]["Навыки"] == "SQL, Python"
    assert rows[0]["Оценка"] == ""
    assert rows[0]["Ссылка"] == "https://hh.ru/vacancy/30"
    page = client.get("/vacancies?search=налитик&order=salary&page=2").text
    href = unescape(re.search(r'href="(/vacancies/export[^\"]*)"', page).group(1))
    assert parse_qs(urlparse(href).query) == {"search": ["налитик"], "order": ["salary"]}
    assert "Скачать CSV · 31" in page


def test_csv_applies_same_score_source_and_application_filters_as_list(client):
    repo = client.app.state.repository
    resume_id = client.portal.call(repo.upsert_resume, Resume(source_url="https://hh.ru/resume/one"))
    client.portal.call(repo.set_active_resume, resume_id)
    rows = _seed(client, resume_id)
    for row in rows[:3]:
        client.portal.call(repo.upsert_score, VacancyScore(
            vacancy_id=row["id"], resume_id=resume_id, score=80, verdict="Подходит",
        ))
    client.portal.call(repo.record_application, VacancyApplication(
        external_id=rows[0]["external_id"], vacancy_url=rows[0]["canonical_url"],
        status=ApplicationStatus.APPLIED,
    ))
    exported = _download(client, min_score=75, only_scored=1, only_unapplied=1,
                         found_for_resume=resume_id, search="налитик")
    assert len(exported) == 2
    assert all(row["Оценка"] == "80" and row["Обоснование оценки"] == "Подходит" for row in exported)
    assert _download(client, found_for_resume=resume_id + 1) == []
    assert _download(client, search="отсутствует") == []


def test_empty_export_has_headers_and_bad_filters_are_rejected(client):
    assert _download(client) == []
    assert client.get("/vacancies/export?min_score=101").status_code == 422


def test_browser_filter_form_allows_empty_minimum_score(client):
    _seed(client)
    page = client.get("/vacancies", params={"search": "налитик", "order": "score", "min_score": ""})
    assert page.status_code == 200
    assert "Скачать CSV · 31" in page.text
    assert len(_download(client, search="налитик", min_score="")) == 31
    for value in ("bad", "-1", "101", "1.5"):
        assert client.get("/vacancies", params={"min_score": value}).status_code == 422


def test_stop_during_rate_limit_closes_search_run(client, monkeypatch):
    manager = client.app.state.tasks
    monkeypatch.setattr(jobs, "ensure_browser", AsyncMock(return_value=manager.settings.browser_config()))
    async def cancelled_collect(self, **kwargs):
        yield PageCommitParams(
            search_run_id=kwargs["task_id"], page_key="1", page_number=1,
            current_url=kwargs["search_url"], canonical_url=kwargs["search_url"],
            cards=[VacancyCard(external_id="stop-test")],
        )
        raise asyncio.CancelledError("stopped while waiting for the rate limit")
    monkeypatch.setattr(jobs.HHVacancyCardCollector, "collect", cancelled_collect)
    ctx = TaskContext(manager, TaskState(id="cancel-collect", kind=TaskKind.COLLECT), manager.lane())
    async def run_and_read():
        with pytest.raises(asyncio.CancelledError):
            await jobs.collect_job(ctx)
        async with manager.repository.connection.execute(
            "SELECT status, completion_reason FROM search_runs WHERE id = ?", ("web_cancel-collect",)
        ) as cursor:
            return tuple(await cursor.fetchone())
    assert client.portal.call(run_and_read) == ("cancelled", "stopped_by_user")
    assert client.portal.call(manager.repository.count_vacancies) == 1


def test_unlimited_progress_shows_pages_without_percentage(client):
    manager = client.app.state.tasks
    manager.lane().current = TaskState(id="progress", kind=TaskKind.COLLECT, done=12)
    page = client.get("/partials/status").text
    assert 'Обработано страниц: 12 · без лимита' in page
    assert 'id="task-bar"' not in page
