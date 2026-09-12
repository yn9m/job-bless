"""Action forms isolate their settings and preserve shared/global state."""

import re

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient

from src.config import Config
from src.db.models import Resume
from src.web.action_settings import ACTION_KEYS, SHARED_KEYS
from src.web.app import create_app
from src.web import jobs


@pytest.fixture()
def client(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "action-settings.db")
    config.web.token = ""
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_shared_form_excludes_action_fields_and_preserves_their_switches(client):
    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(settings.save, {
            "activity.open_vacancies": "1", "schedule.activity_enabled": "1",
            "resume_touch.edit_fallback": "1", "cover_letter.enabled": "1",
            "schedule.enabled": "1", "matching.enabled": "1",
            "llm.enabled": "1",
        })
    before = settings.all_values()
    page = client.get('/settings').text
    rendered = set(re.findall(r'name="([a-z_]+\.[^"]+)"', page))
    assert rendered == SHARED_KEYS
    assert 'name="apply.skip_questions"' not in page
    response = client.post('/actions/settings', data={
        'matching.threshold': '82', 'activity.open_vacancies': '', 'profile.max_chars': '9999',
    }, follow_redirects=False)
    assert response.status_code == 303
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    for key, value in before.items():
        if key not in SHARED_KEYS:
            assert settings.get(key) == value, key


@pytest.mark.parametrize('with_resume', [False, True])
def test_inline_query_saves_and_start_uses_latest_input(client, monkeypatch, with_resume):
    repo = client.app.state.repository
    resume_id = ''
    if with_resume:
        resume_id = str(client.portal.call(repo.upsert_resume, Resume(
            source_url='https://hh.ru/resume/inline', search_query='Python', context_text='Keep my experience',
        )))
        client.portal.call(repo.set_active_resume, int(resume_id))
    response = client.post('/actions/search-query', data={'resume_id':resume_id, 'search_query':'  Go  '})
    assert 'Сохранено' in response.text
    assert not client.app.state.tasks.is_busy
    assert 'value="Go"' in client.get('/partials/status').text
    # Saving browser settings no longer submits or clears the query.
    client.post('/actions/search-settings', data={'resume_id':resume_id, 'scroller.max_scroll_steps_per_page':'9'})
    assert 'value="Go"' in client.get('/partials/status').text
    seen = []
    async def collect(ctx):
        resume = await ctx.repository.get_active_resume()
        seen.append(resume.search_query if resume else ctx.settings.search_query)
        if resume:
            assert resume.context_text == 'Keep my experience'
        return {}
    monkeypatch.setattr(jobs, 'collect_job', collect)
    client.post('/actions/collect', data={'resume_id':resume_id, 'search_query':'  Rust  '})
    async def finish():
        await client.app.state.tasks.lane().task
    client.portal.call(finish)
    assert seen == ['Rust']


def test_inline_query_rejects_stale_resume_and_empty_input(client, monkeypatch):
    from unittest.mock import AsyncMock
    work = AsyncMock(return_value={})
    monkeypatch.setattr(jobs, 'collect_job', work)
    before = client.app.state.settings.search_query
    response = client.post('/actions/collect', data={'resume_id':'999', 'search_query':'Go'})
    assert 'Активное резюме изменилось' in response.text
    response = client.post('/actions/collect', data={'resume_id':'', 'search_query':' '})
    assert 'Укажите, какую работу ищете' in response.text
    work.assert_not_awaited()
    assert client.app.state.settings.search_query == before


@pytest.mark.parametrize('kind,values,expected', [
    ('llm', {'llm.enabled': '1', 'llm.model': 'test-model', 'llm.api_key': 'test-secret'},
     {'llm.enabled': True, 'llm.model': 'test-model', 'llm.api_key': 'test-secret'}),
    ('score', {'matching.batch_size': '7', 'matching.concurrency': '2', 'matching.prompt': 'Оценить опыт'},
     {'matching.batch_size': 7, 'matching.concurrency': 2}),
    ('profile', {'profile.model': 'profile-test', 'profile.max_chars': '7500', 'profile.timeout_sec': '200'},
     {'profile.model': 'profile-test', 'profile.max_chars': 7500}),
    ('resume_touch', {'resume_touch.edit_fallback': '1', 'schedule.resume_touch_enabled': '1',
                      'schedule.resume_touch_interval_hours': '8'},
     {'resume_touch.edit_fallback': True, 'schedule.resume_touch_enabled': True,
      'schedule.resume_touch_interval_hours': 8}),
])
def test_action_modal_persists_only_its_own_settings(client, kind, values, expected):
    settings = client.app.state.settings
    before = settings.all_values()
    page = client.get('/actions').text
    # Profile generation now lives on the resume card, without a duplicate action.
    assert (f'data-open-{kind}' in page) == (kind != 'profile')
    assert f'id="{kind}-dialog"' in page
    modal = client.get(f'/actions/{kind}-settings')
    assert modal.status_code == 200
    for key in ACTION_KEYS[kind]:
        assert f'name="{key}"' in modal.text
    response = client.post(f'/actions/{kind}-settings', data={
        **values, 'llm.enabled': '1', 'matching.threshold': '99', 'schedule.do_apply': '1',
    })
    assert response.headers['HX-Trigger-After-Settle'] == f'{kind}SettingsSaved'
    assert response.headers['HX-Retarget'] == '#task-panel'
    assert not client.app.state.tasks.is_busy and not client.app.state.tasks.activity_busy
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    for key, value in expected.items():
        assert settings.get(key) == value
    for key, value in before.items():
        if key not in ACTION_KEYS[kind]:
            assert settings.get(key) == value, key
    if kind in {'activity', 'resume_touch'}:
        entry = next(entry for entry in client.app.state.scheduler.entries if entry.name == kind)
        assert entry.next_run_at is not None
        client.post(f'/actions/{kind}-settings', data={})
        assert entry.next_run_at is None


@pytest.mark.parametrize('kind,values', [
    ('llm', {'llm.timeout_sec': '0', 'llm.enabled': '1'}),
    ('search', {'scroller.max_scroll_steps_per_page': '0'}),
    ('score', {'matching.batch_size': '0', 'matching.prompt': '<b>Сохранить мой текст</b>'}),
    ('profile', {'profile.max_chars': '0', 'profile.model': 'my-model'}),
    ('resume_touch', {'schedule.resume_touch_interval_hours': '0', 'resume_touch.edit_fallback': '1'}),
    ('pipeline', {'schedule.interval_minutes': '0', 'schedule.do_apply': '1', 'schedule.enabled': '1'}),
])
def test_invalid_action_settings_keep_input_and_do_not_write(client, kind, values):
    settings = client.app.state.settings
    before = settings.all_values()
    response = client.post(f'/actions/{kind}-settings', data=values)
    assert 'HX-Trigger-After-Settle' not in response.headers
    assert 'минимум' in response.text and 'value="0"' in response.text
    if kind == 'score':
        assert '&lt;b&gt;Сохранить мой текст&lt;/b&gt;' in response.text
    if kind == 'pipeline':
        assert re.search(r'name="schedule.do_apply"[^>]*checked', response.text)
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.all_values() == before


def test_invalid_search_settings_do_not_change_query_or_resume_context(client):
    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        resume_id = portal.call(repository.upsert_resume, Resume(
            source_url='https://hh.ru/resume/search-modal', title='Developer',
            search_query='Python', context_text='My experience',
        ))
        portal.call(repository.set_active_resume, resume_id)
    before = client.app.state.settings.all_values()
    response = client.post('/actions/search-settings', data={
        'resume_id': str(resume_id), 'search_query': 'Go', 'scroller.max_scroll_steps_per_page': '0',
    })
    assert 'value="0"' in response.text
    assert 'HX-Trigger-After-Settle' not in response.headers
    with start_blocking_portal() as portal:
        resume = portal.call(repository.get_resume, resume_id)
    assert resume.search_query == 'Python' and resume.context_text == 'My experience'
    assert client.app.state.settings.all_values() == before
    stale = client.post('/actions/search-settings', data={
        'resume_id': str(resume_id + 1), 'search_query': 'Go', 'scroller.max_scroll_steps_per_page': '9',
    })
    assert 'Активное резюме изменилось' in stale.text
    assert client.app.state.settings.all_values() == before


def test_setup_section_and_resume_selection(client):
    repository = client.app.state.repository
    with start_blocking_portal() as portal:
        first = portal.call(repository.upsert_resume, Resume(
            source_url='https://hh.ru/resume/first', title='Python developer', search_query='Python',
        ))
        second = portal.call(repository.upsert_resume, Resume(
            source_url='https://hh.ru/resume/second', title='Go developer', search_query='Golang',
        ))
        portal.call(repository.set_active_resume, first)
    page = client.get('/actions').text
    setup, rest = page.split('<section class="main-actions"', 1)
    assert 'Необходимые настройки' in setup
    assert setup.count('data-action-id="login"') == 1
    assert 'data-action-id="login"' not in rest
    assert 'id="active-resume"' in setup and 'data-open-llm' in setup
    assert f'value="{first}" selected' in setup
    response = client.post('/actions/resume/select', data={'resume_id': second})
    assert response.status_code == 200
    assert f'value="{second}" selected' in response.text
    assert 'value="Golang"' in response.text
    with start_blocking_portal() as portal:
        assert portal.call(repository.get_active_resume).id == second
    missing = client.post('/actions/resume/select', data={'resume_id': second + 1})
    assert 'Резюме не найдено' in missing.text
    with start_blocking_portal() as portal:
        assert portal.call(repository.get_active_resume).id == second

    from src.db.models import TaskKind, TaskStatus
    from src.web.tasks import TaskState
    client.app.state.tasks.lane().current = TaskState(
        id='selection-busy', kind=TaskKind.COLLECT, status=TaskStatus.RUNNING,
    )
    from types import SimpleNamespace
    from unittest.mock import patch
    with patch.object(client.app.state.tasks.lane(), 'task', SimpleNamespace(done=lambda: False)):
        blocked = client.post('/actions/resume/select', data={'resume_id': first})
        standalone = client.post('/actions/resume/select', data={'resume_id': 0})
        assert 'Дождитесь завершения текущего действия' in standalone.text
    assert 'Дождитесь завершения текущего действия' in blocked.text
    assert re.search(r'<select[^>]*id="active-resume"[^>]*disabled', blocked.text)
    with start_blocking_portal() as portal:
        assert portal.call(repository.get_active_resume).id == second
    client.app.state.tasks.lane().current = None


def test_llm_modal_preserves_secret_and_invalidates_health(client):
    from src.llm.health import LLMHealth

    monitor = client.app.state.llm_health
    client.post('/actions/llm-settings', data={'llm.api_key': 'saved-secret', 'llm.enabled': '1'})
    monitor._health = LLMHealth(ok=True, state='ok')
    assert monitor.cached is not None
    response = client.post('/actions/llm-settings', data={'llm.api_key': '', 'llm.enabled': '1'})
    assert response.headers['HX-Trigger-After-Settle'] == 'llmSettingsSaved'
    assert client.app.state.settings.get('llm.api_key') == 'saved-secret'
    assert monitor.cached is None


def test_aistudio_model_settings_preserve_connection_and_unrelated_settings(client):
    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(settings.save, {'llm.connection': 'aistudio', 'llm.enabled': '1'})
    before = settings.all_values()
    response = client.post('/actions/aistudio/settings', data={'llm.model': 'gemini-2.5-flash-lite'})
    assert response.status_code == 200
    assert response.headers['HX-Trigger-After-Settle'] == 'llmSettingsSaved'
    assert settings.get('llm.model') == 'gemini-2.5-flash-lite'
    assert settings.get('llm.connection') == 'aistudio'
    assert settings.get('llm.enabled') is True
    assert settings.get('matching.threshold') == before['matching.threshold']


def test_aistudio_model_settings_are_blocked_during_running_work(client):
    from types import SimpleNamespace
    from unittest.mock import patch
    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(settings.save, {'llm.connection': 'aistudio', 'llm.model': 'before'})
    with patch.object(client.app.state.tasks.lane(), 'task', SimpleNamespace(done=lambda: False)):
        response = client.post('/actions/aistudio/settings', data={'llm.model': 'after'})
    assert 'Дождитесь завершения текущего действия' in response.text
    assert settings.get('llm.model') == 'before'


def test_inline_model_selection_saves_only_model_and_renders_card(client, monkeypatch):
    settings = client.app.state.settings
    runtime = client.app.state.aistudio
    with start_blocking_portal() as portal:
        portal.call(settings.save, {
            'llm.connection': 'aistudio', 'llm.enabled': '1', 'llm.model': 'before',
            'llm.temperature': '1', 'llm.modifiers.search': '1',
        })
    runtime.state = 'ready'
    runtime.models = ['before', 'after']
    monkeypatch.setattr(runtime, '_read_status', lambda: {'connected': True})
    before = settings.all_values()
    for url in ['/actions', '/actions/aistudio/card']:
        page = client.get(url).text
        assert 'id="active-llm-model"' in page
        assert 'hx-post="/actions/llm/select"' in page
        assert 'value="before" selected' in page
        assert 'value="after"' in page
    response = client.post('/actions/llm/select', data={
        'model': 'after', 'llm.temperature': '0.2', 'llm.enabled': '0',
    })
    assert response.status_code == 200
    assert 'value="after" selected' in response.text
    assert 'HX-Trigger-After-Settle' not in response.headers
    with start_blocking_portal() as portal:
        portal.call(settings.load)
    assert settings.all_values() == {**before, 'llm.model': 'after'}


@pytest.mark.parametrize('model', ['', 'unknown'])
def test_inline_model_rejects_missing_or_unavailable_model(client, model):
    before = client.app.state.settings.all_values()
    response = client.post('/actions/llm/select', data={'model': model})
    assert 'Модель недоступна' in response.text
    assert client.app.state.settings.all_values() == before


@pytest.mark.parametrize('lane', ['main', 'profile'])
def test_inline_model_cannot_change_during_work(client, lane, monkeypatch):
    from types import SimpleNamespace
    from src.llm.health import LLMHealth

    settings = client.app.state.settings
    with start_blocking_portal() as portal:
        portal.call(settings.save, {'llm.connection': 'custom', 'llm.enabled': '1'})
    client.app.state.llm_health._health = LLMHealth(ok=True, state='ok', models=['after'])
    before = settings.all_values()
    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.tasks.lane(lane), 'task', SimpleNamespace(done=lambda: False))
        response = client.post('/actions/llm/select', data={'model': 'after'})
    assert 'Дождитесь завершения текущего действия' in response.text
    assert re.search(r'<select[^>]*id="active-llm-model"[^>]*disabled', response.text)
    assert settings.all_values() == before
