"""Owner access and account controls exercised through the real ASGI app."""

import asyncio
import re
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.config import Config
from src.db.models import TaskKind
from src.passwords import hash_password
from src.web.app import create_app
from src.web import jobs
from src.web.auth import COOKIE, LOGIN_COOKIE
from src.web.tasks import LANE_ACTIVITY, TaskBusyError

PASSWORD = "a-test-password-1234"
PASSWORD_HASH = hash_password(PASSWORD)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_BLESS_AISTUDIO_BUNDLE", str(tmp_path / "no-bundle"))
    config = Config()
    config.db.sqlite_path = str(tmp_path / "app.db")
    config.web.password_hash = PASSWORD_HASH
    config.web.require_auth = True
    config.accounts.hh_vnc_host = "127.0.0.1"
    config.accounts.novnc_path = str(tmp_path / "novnc")
    (tmp_path / "novnc").mkdir()
    with TestClient(create_app(config)) as test_client:
        test_client.portal.call(test_client.app.state.repository.save_settings, {"hh.login_required": "false"})
        yield test_client


def sign_in(client, password=PASSWORD):
    client.get("/login")
    return client.post("/login", data={"password": password, "csrf_token": client.cookies[LOGIN_COOKIE]},
                       headers={"Origin": "http://testserver"}, follow_redirects=False)


def action_headers(client):
    session = client.app.state.auth.sessions[client.cookies[COOKIE]]
    return {"Origin": "http://testserver", "X-CSRF-Token": session.csrf}


def test_server_requires_a_valid_password_configuration():
    config = Config()
    config.web.require_auth = True
    with pytest.raises(ValueError, match="WEB_PASSWORD_HASH"):
        create_app(config)
    config.web.password_hash = "plain-text-password"
    with pytest.raises(ValueError, match="invalid format"):
        create_app(config)


def test_no_remote_desktop_without_owner_login():
    config = Config()
    config.accounts.hh_vnc_host = "private-browser"
    with pytest.raises(ValueError, match="Remote account screens"):
        create_app(config)


def test_login_page_and_api_are_protected(client):
    assert client.get("/actions", follow_redirects=False).headers["location"] == "/login"
    response = client.get("/actions", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"
    assert client.get("/healthz").json()["service"] == "job-bless"
    assert COOKIE not in client.cookies


def test_login_csrf_and_invalid_password_do_not_create_a_session(client):
    client.get("/login")
    response = client.post("/login", data={"password": PASSWORD}, headers={"Origin": "http://testserver"})
    assert response.status_code == 403
    response = sign_in(client, "wrong-password")
    assert response.status_code == 401
    assert COOKIE not in client.cookies


def test_login_limits_attempts_without_trusting_forwarding_headers(client):
    for index in range(5):
        client.get("/login")
        response = client.post("/login", data={"password": "wrong", "csrf_token": client.cookies[LOGIN_COOKIE]},
                               headers={"Origin": "http://testserver", "X-Forwarded-For": f"192.0.2.{index}"})
        assert response.status_code == 401
    assert sign_in(client).status_code == 429


def test_cookie_and_native_form_roundtrip(client):
    response = sign_in(client)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert "SameSite=strict" in response.headers.get_list("set-cookie")[0]
    page = client.get("/settings")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    response = client.post("/actions/settings", data={"csrf_token": csrf, "matching.threshold": "73"},
                           headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert client.app.state.settings.get("matching.threshold") == 73


def test_csrf_guard_does_not_mutate_settings(client):
    sign_in(client)
    before = client.app.state.settings.get("matching.threshold")
    for headers in ({"Origin": "http://testserver"},
                    {**action_headers(client), "Origin": "https://attacker.example"}):
        response = client.post("/actions/settings", data={"matching.threshold": "99"}, headers=headers)
        assert response.status_code == 403
    assert client.app.state.settings.get("matching.threshold") == before


def test_logout_and_expiry_revoke_access(client):
    sign_in(client)
    token = client.cookies[COOKIE]
    assert client.post("/logout", headers=action_headers(client), follow_redirects=False).status_code == 303
    client.cookies.set(COOKIE, token)
    assert client.get("/settings", follow_redirects=False).headers["location"] == "/login"
    client.cookies.clear()
    sign_in(client)
    client.app.state.auth.sessions[client.cookies[COOKIE]].expires = time.time() - 1
    assert client.get("/settings", follow_redirects=False).headers["location"] == "/login"


def test_hh_logout_stops_browser_jobs_and_keeps_panel_and_google(client, monkeypatch):
    from pathlib import Path
    from src.browser.session import SharedBrowserSession
    from src.db.models import Resume
    from src.web.routes import actions
    sign_in(client)
    state = client.app.state
    manager = state.tasks
    manager.STOP_GRACE_SECONDS = .01
    stopped = []

    async def work(ctx):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(ctx.state.kind)

    client.portal.call(manager.start, TaskKind.COLLECT, work)
    client.portal.call(lambda: manager.start(TaskKind.ACTIVITY, work, lane=LANE_ACTIVITY))
    owner = client.cookies[COOKIE]
    state.screens.grant("hh", owner)
    google_lease = state.screens.grant("google", owner)
    google_stop = AsyncMock()
    monkeypatch.setattr(state.aistudio, "stop", google_stop)
    monkeypatch.setattr(actions, "SESSION", SharedBrowserSession())
    storage = Path(state.settings.browser_config().storage_state_path)
    storage.write_text('{"cookies":["synthetic-login"]}')
    client.portal.call(state.repository.upsert_resume, Resume(source_url="https://hh.ru/resume/synthetic", title="Saved resume"))
    history_before = client.portal.call(state.repository.list_resumes)

    assert client.post("/actions/hh/logout", headers={"Origin": "http://testserver"}).status_code == 403
    assert storage.exists()
    response = client.post("/actions/hh/logout", headers=action_headers(client))
    assert response.status_code == 200
    assert set(stopped) == {TaskKind.COLLECT, TaskKind.ACTIVITY}
    assert not storage.exists()
    assert not state.screens.active("hh")
    assert state.screens.leases["google"] is google_lease
    google_stop.assert_not_awaited()
    assert client.get("/actions").status_code == 200  # panel login survives
    assert "Вы вошли в hh.ru" not in response.text
    assert client.portal.call(state.repository.get_all_settings)["hh.login_required"] == "true"
    assert client.portal.call(state.repository.list_resumes) == history_before
    with pytest.raises(TaskBusyError, match="подтвердите вход"):
        client.portal.call(lambda: manager.start(TaskKind.APPLY, AsyncMock(), trigger="schedule"))

    async def guest_collect():
        await manager.start(TaskKind.COLLECT, AsyncMock(return_value={}), trigger="schedule")
        await manager.lane().task
        assert manager.current.status.value == 'completed'
    client.portal.call(guest_collect)


def test_secure_cookie_for_https(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_BLESS_AISTUDIO_BUNDLE", str(tmp_path / "no-bundle"))
    config = Config()
    config.db.sqlite_path = str(tmp_path / "https.db")
    config.web.password_hash = PASSWORD_HASH
    config.web.public_url = "https://jobs.example.com"
    with TestClient(create_app(config), base_url="https://jobs.example.com") as client:
        client.get("/login")
        response = client.post("/login", data={"password": PASSWORD, "csrf_token": client.cookies[LOGIN_COOKIE]},
                               headers={"Origin": "https://jobs.example.com"}, follow_redirects=False)
        assert response.status_code == 303
        assert "Secure" in response.headers.get_list("set-cookie")[0]
        assert client.get("/actions").status_code == 200


def test_opening_another_login_page_keeps_the_first_form_valid(client):
    import re
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    client.get("/favicon.ico")  # follows the unauthenticated redirect
    response = client.post("/login", data={"password": PASSWORD, "csrf_token": token},
                           headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/actions").status_code == 200


def test_websocket_rejects_missing_session_origin_and_expired_lease(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/accounts/hh/ws", headers={"Origin": "http://testserver"}):
            pass
    sign_in(client)
    owner = client.cookies[COOKIE]
    lease = client.app.state.screens.grant("hh", owner)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/accounts/hh/ws", headers={"Origin": "https://attacker.example"}):
            pass
    lease.expires = 0
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/accounts/hh/ws", headers={"Origin": "http://testserver"}):
            pass


def test_websocket_binary_transport_and_logout_revocation(client):
    sign_in(client)
    client.app.state.screens.grant("hh", client.cookies[COOKIE])

    async def start_server():
        async def echo(reader, writer):
            try:
                writer.write(b"RFB 003.008\n")
                await writer.drain()
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        client.app.state.config.accounts.hh_vnc_port = server.sockets[0].getsockname()[1]
        return server

    server = client.portal.call(start_server)
    try:
        with client.websocket_connect("/accounts/hh/ws", headers={"Origin": "http://testserver"}) as socket:
            assert socket.receive_bytes() == b"RFB 003.008\n"
            socket.send_bytes(b"keyboard-event")
            assert socket.receive_bytes() == b"keyboard-event"
            client.post("/logout", headers=action_headers(client))
            with pytest.raises(WebSocketDisconnect):
                socket.receive_bytes()
    finally:
        client.portal.call(server.close)
        client.portal.call(server.wait_closed)


def test_screen_holds_browser_lanes_against_scheduled_work(client):
    sign_in(client)
    manager = client.app.state.tasks
    client.app.state.screens.grant("hh", client.cookies[COOKIE])
    with pytest.raises(TaskBusyError, match="Закройте окно"):
        client.portal.call(manager.start, TaskKind.COLLECT, AsyncMock())
    client.portal.call(client.app.state.screens.revoke, "hh")
    manager.connection_busy = lambda: True
    with pytest.raises(TaskBusyError, match="Google"):
        client.portal.call(manager.start, TaskKind.SCORE, AsyncMock())


def test_open_screen_stops_both_hh_lanes_before_login(client, monkeypatch):
    sign_in(client)
    manager = client.app.state.tasks
    manager.STOP_GRACE_SECONDS = 0.01
    stopped = []

    async def work(ctx):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(ctx.lane)

    async def login(ctx):
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "login_job", login)

    async def start():
        await manager.start(TaskKind.COLLECT, work)
        await manager.start(TaskKind.ACTIVITY, work, lane=LANE_ACTIVITY)
        await asyncio.sleep(0)
    client.portal.call(start)
    response = client.post("/accounts/hh/open", headers=action_headers(client))
    assert response.status_code == 200
    assert set(stopped) == {"main", "activity"}
    assert manager.current.kind == TaskKind.LOGIN
    assert client.post("/accounts/hh/close", headers=action_headers(client)).status_code == 200


def test_another_device_cannot_take_over_a_screen(client):
    sign_in(client)
    old_owner = client.cookies[COOKIE]
    client.app.state.screens.grant("hh", old_owner)
    client.cookies.clear()
    sign_in(client)
    assert client.post("/accounts/hh/open", headers=action_headers(client)).status_code == 409
    assert client.app.state.screens.leases["hh"].owner == old_owner


def test_deployment_browser_settings_override_imported_desktop_values(client):
    state = client.app.state
    state.config.browser.endpoint = "ws://hh-browser:3000/hh"
    state.settings._values.update({"browser.provider": "local_process", "browser.cdp.endpoint": "http://localhost:9222", "browser.headless": True})
    browser = state.settings.browser_config()
    assert browser.endpoint == "ws://hh-browser:3000/hh"


def test_native_private_link_uses_sessions_csrf_and_logout(client):
    auth = client.app.state.auth
    auth.config.password_hash = ""
    auth.config.token = "native-private-link-" + "x" * 32
    login = client.get("/actions?token=" + auth.config.token, follow_redirects=False)
    assert login.status_code == 303
    assert "token=" not in login.headers["location"]
    assert COOKIE in client.cookies
    assert auth.config.token not in client.cookies.values()
    assert client.post("/actions/settings", data={"matching.threshold": "73"}).status_code == 403
    assert client.post("/actions/settings", data={"matching.threshold": "73"},
                       headers=action_headers(client), follow_redirects=False).status_code == 303
    owner = client.cookies[COOKIE]
    client.post("/logout", headers=action_headers(client))
    assert owner not in auth.sessions


def test_chrome_controls_are_removed_from_settings(client):
    sign_in(client)
    page = client.get("/settings").text
    for key in ("browser.provider", "browser.transport", "browser.cdp.endpoint", "browser.headless"):
        assert f'name="{key}"' not in page
    assert 'name="browser.close_stale_tabs"' in page


def test_hh_challenge_stops_current_run_but_public_search_can_be_retried(client):
    from src.browser.intervention import HHInterventionRequired
    from src.web.panel import hh_account_status
    manager = client.app.state.tasks

    async def challenge(ctx):
        raise HHInterventionRequired("Откройте браузер HH")

    async def run():
        await manager.start(TaskKind.COLLECT, challenge)
        await manager.lane().task
        assert not (await hh_account_status(client.app.state.repository))["logged_in"]
        with pytest.raises(TaskBusyError, match="подтвердите вход"):
            await manager.start(TaskKind.APPLY, AsyncMock(), trigger="schedule")
        await manager.start(TaskKind.COLLECT, AsyncMock(return_value={}))
        await manager.lane().task
        assert (await client.app.state.repository.get_all_settings())['hh.login_required'] == 'true'
    client.portal.call(run)


@pytest.mark.parametrize('path,data,job_name', [
    ('/actions/collect', {}, 'collect_job'),
    ('/actions/start', {'kind': 'collect'}, 'collect_job'),
    ('/actions/pipeline', {}, 'pipeline_job'),
    ('/actions/start', {'kind': 'pipeline'}, 'pipeline_job'),
])
def test_guest_collection_endpoints_work_with_login_required_flag(client, monkeypatch, path, data, job_name):
    sign_in(client)  # App authentication is separate from the HH account.
    client.portal.call(client.app.state.repository.save_settings, {'hh.login_required': 'true'})
    work = AsyncMock(return_value={'new': 1})
    monkeypatch.setattr(jobs, job_name, work)
    response = client.post(path, data=data, headers=action_headers(client))
    assert response.status_code == 200
    assert 'подтвердите вход' not in response.text
    async def finish():
        await client.app.state.tasks.lane().task
    client.portal.call(finish)
    work.assert_awaited_once()
    assert client.app.state.tasks.current.status.value == 'completed'
    assert client.portal.call(client.app.state.repository.get_all_settings)['hh.login_required'] == 'true'


@pytest.mark.parametrize('apply_enabled,mode,blocked', [
    (False, 'auto', False), (True, 'manual', False), (True, 'auto', True),
])
def test_guest_pipeline_requires_hh_only_when_it_will_submit_responses(client, apply_enabled, mode, blocked):
    manager = client.app.state.tasks
    async def run():
        await client.app.state.repository.save_settings({'hh.login_required': 'true'})
        await client.app.state.settings.save({'schedule.do_apply': '1' if apply_enabled else '0', 'apply.mode': mode})
        work = AsyncMock(return_value={})
        if blocked:
            with pytest.raises(TaskBusyError, match='подтвердите вход'):
                await manager.start(TaskKind.COLLECT, work, params={'action': 'pipeline'}, trigger='schedule')
            work.assert_not_awaited()
        else:
            await manager.start(TaskKind.COLLECT, work, params={'action': 'pipeline'}, trigger='schedule')
            await manager.lane().task
            work.assert_awaited_once()
    client.portal.call(run)


@pytest.mark.asyncio
async def test_logout_revokes_live_log_stream_before_next_event():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from src.web.routes.actions import events
    owner = [object()]
    queue = asyncio.Queue()
    manager = SimpleNamespace(current=None, subscribe=lambda: queue, unsubscribe=Mock())
    auth = SimpleNamespace(enabled=True, session=lambda request: owner[0])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        tasks=manager, auth=auth, shutting_down=False)), is_disconnected=AsyncMock(return_value=False))
    response = await events(request)
    queue.put_nowait({"type": "log", "line": "first"})
    assert "first" in await anext(response.body_iterator)
    owner[0] = None
    queue.put_nowait({"type": "log", "line": "private-after-logout"})
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)
    manager.unsubscribe.assert_called_once_with(queue)
