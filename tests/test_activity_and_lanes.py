"""Parallel lanes, tab cleanup, the activity module and the resume refresh."""

import asyncio

import anyio
import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient

from src.config import BrowserConfig, Config
from src.db.models import TaskKind, TaskStatus
from src.web.app import create_app
from src.web.tasks import LANE_ACTIVITY, LANE_MAIN, TaskBusyError


@pytest.fixture()
def client(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.driver = "sqlite"
    config.db.sqlite_path = str(tmp_path / "lanes.db")
    with TestClient(create_app(config)) as test_client:
        yield test_client


# --- lanes ---------------------------------------------------------------

def test_activity_runs_in_parallel_with_the_main_job(client):
    manager = client.app.state.tasks
    running = {"main": False, "activity": False}

    async def busy(name):
        async def job(ctx):
            running[name] = True
            for _ in range(200):
                ctx.raise_if_stopped()
                await anyio.sleep(0.05)
            return {}
        return job

    with start_blocking_portal() as portal:
        main_job = portal.call(busy, "main")
        activity = portal.call(busy, "activity")

        portal.call(manager.start, TaskKind.COLLECT, main_job)
        portal.call(_start_in_lane, manager, TaskKind.ACTIVITY, activity, LANE_ACTIVITY)
        portal.call(anyio.sleep, 0.2)

        # Both lanes are occupied at the same time.
        assert manager.is_busy and manager.activity_busy
        assert running == {"main": True, "activity": True}

        portal.call(_stop_lane, manager, LANE_MAIN)
        portal.call(_stop_lane, manager, LANE_ACTIVITY)
        portal.call(anyio.sleep, 0.3)
        assert not manager.is_busy and not manager.activity_busy


async def _stop_lane(manager, lane):
    manager.request_stop(lane)


async def _start_in_lane(manager, kind, job, lane):
    """portal.call passes arguments positionally, start() wants keywords."""
    return await manager.start(kind, job, lane=lane)


def test_second_job_in_the_same_lane_is_rejected(client):
    manager = client.app.state.tasks

    async def job(ctx):
        await anyio.sleep(2)
        return {}

    with start_blocking_portal() as portal:
        portal.call(manager.start, TaskKind.COLLECT, job)
        portal.call(anyio.sleep, 0.1)

        with pytest.raises(TaskBusyError):
            portal.call(manager.start, TaskKind.SCORE, job)

        # ...while the other lane is free.
        portal.call(_start_in_lane, manager, TaskKind.ACTIVITY, job, LANE_ACTIVITY)
        assert manager.activity_busy

        portal.call(_stop_lane, manager, LANE_MAIN)
        portal.call(_stop_lane, manager, LANE_ACTIVITY)
        portal.call(anyio.sleep, 0.3)


def test_stopping_one_lane_leaves_the_other_running(client):
    manager = client.app.state.tasks

    async def job(ctx):
        for _ in range(100):
            ctx.raise_if_stopped()
            await anyio.sleep(0.05)
        return {}

    with start_blocking_portal() as portal:
        portal.call(manager.start, TaskKind.COLLECT, job)
        portal.call(_start_in_lane, manager, TaskKind.ACTIVITY, job, LANE_ACTIVITY)
        portal.call(anyio.sleep, 0.2)

        portal.call(_stop_lane, manager, LANE_ACTIVITY)
        portal.call(anyio.sleep, 0.3)

        assert manager.is_busy          # main lane untouched
        assert not manager.activity_busy
        assert manager.activity.status == TaskStatus.CANCELLED

        portal.call(_stop_lane, manager, LANE_MAIN)
        portal.call(anyio.sleep, 0.3)


def test_panel_shows_both_lanes(client):
    panel = client.get("/partials/status").text
    assert "Имитация активности" in panel
    assert 'hx-post="/actions/activity"' in panel
    assert 'value="resume_touch"' in panel  # resume refresh is a runner option


def test_activity_endpoint_starts_the_activity_lane(client):
    response = client.post("/actions/activity")
    assert response.status_code == 200

    with start_blocking_portal() as portal:
        runs = portal.call(client.app.state.repository.list_task_runs, 5)
    assert runs and runs[0]["kind"] == "activity"


def test_stop_targets_the_requested_lane(client):
    assert "нет выполняющейся задачи" in client.post("/actions/stop", data={"lane": "activity"}).text


# --- stale tabs ----------------------------------------------------------

class FakePage:
    def __init__(self, url="https://hh.ru/old"):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


async def test_stale_tabs_are_closed_but_ours_is_kept():
    from src.browser.connector import BrowserConnector

    connector = BrowserConnector(BrowserConfig(close_stale_tabs=True))
    ours = FakePage("about:blank")
    stale = [FakePage("https://hh.ru/vacancy/1"), FakePage("https://hh.ru/search")]
    connector._created_page = ours

    await connector._close_stale_tabs(FakeContext([*stale, ours]))

    assert all(page.closed for page in stale)
    assert not ours.closed


async def test_stale_tab_cleanup_can_be_disabled():
    from src.browser.connector import BrowserConnector

    connector = BrowserConnector(BrowserConfig(close_stale_tabs=False))
    stale = FakePage()
    connector._created_page = FakePage("about:blank")

    await connector._close_stale_tabs(FakeContext([stale]))
    assert not stale.closed


async def test_tab_cleanup_survives_a_page_that_refuses_to_close():
    from src.browser.connector import BrowserConnector

    class Stubborn(FakePage):
        async def close(self):
            raise RuntimeError("target closed")

    connector = BrowserConnector(BrowserConfig(close_stale_tabs=True))
    connector._created_page = FakePage("about:blank")
    good = FakePage()

    await connector._close_stale_tabs(FakeContext([Stubborn(), good]))
    assert good.closed  # one bad tab does not abort the cleanup


# --- collector stream finalization --------------------------------------

async def test_close_stream_releases_the_generator():
    from src.web.jobs import close_stream

    cleaned = {"value": False}

    async def stream():
        try:
            yield 1
            yield 2
        finally:
            cleaned["value"] = True

    gen = stream()
    assert await gen.__anext__() == 1
    await close_stream(gen)
    assert cleaned["value"] is True  # the browser tab would be closed here


async def test_close_stream_completes_even_when_cancelled():
    from src.web.jobs import close_stream

    cleaned = {"value": False}

    async def stream():
        try:
            yield 1
        finally:
            cleaned["value"] = True

    gen = stream()
    await gen.__anext__()

    async def consumer():
        await close_stream(gen)

    task = asyncio.ensure_future(consumer())
    await asyncio.sleep(0)
    task.cancel()
    await task  # the cancellation of the *close* itself is absorbed

    await asyncio.sleep(0.05)  # the shielded close finishes on its own
    assert cleaned["value"] is True


# --- settings ------------------------------------------------------------

def test_activity_settings_have_their_own_group(client):
    settings = client.app.state.settings
    groups = dict(settings.grouped_fields())
    assert "Имитация активности" in groups
    assert "Обновление резюме" in groups

    page = client.get("/settings").text
    assert "Имитация активности" in page
    assert 'name="activity.duration_min"' in page


def test_activity_config_falls_back_to_the_resume_query(client):
    settings = client.app.state.settings
    client.post(
        "/actions/settings",
        data={"activity.url": "", "activity.duration_min": "3"},
        follow_redirects=False,
    )
    config = settings.activity_config(query="Go")
    assert "text=Go" in config.url
    assert config.duration_min == 3


def test_activity_pause_range_cannot_be_inverted(client):
    settings = client.app.state.settings
    client.post(
        "/actions/settings",
        data={"activity.pause_min_sec": "9", "activity.pause_max_sec": "2"},
        follow_redirects=False,
    )
    config = settings.activity_config()
    assert config.pause_max_sec >= config.pause_min_sec


def test_max_pages_accepts_four_digits(client):
    client.post("/actions/settings", data={"scroller.max_pages": "9999"}, follow_redirects=False)
    assert client.app.state.settings.scroller_config().max_pages == 9999

    rejected = client.post("/actions/settings", data={"scroller.max_pages": "10000"})
    assert rejected.status_code == 400


# --- scheduler -----------------------------------------------------------

def test_scheduler_has_three_independent_cycles(client):
    names = [cycle["name"] for cycle in client.app.state.scheduler.status()]
    assert names == ["pipeline", "activity", "resume_touch"]


def test_activity_schedule_is_independent_of_the_pipeline(client):
    client.post(
        "/actions/settings",
        data={
            "schedule.enabled": "",               # pipeline off
            "schedule.activity_enabled": "1",     # activity on
            "schedule.activity_interval_minutes": "45",
        },
        follow_redirects=False,
    )
    cycles = {cycle["name"]: cycle for cycle in client.app.state.scheduler.status()}
    assert cycles["pipeline"]["enabled"] is False
    assert cycles["activity"]["enabled"] is True
    assert cycles["activity"]["interval_minutes"] == 45
    assert cycles["activity"]["next_run_at"] is not None


def test_resume_touch_interval_is_in_hours(client):
    client.post(
        "/actions/settings",
        data={"schedule.resume_touch_enabled": "1", "schedule.resume_touch_interval_hours": "4"},
        follow_redirects=False,
    )
    cycles = {cycle["name"]: cycle for cycle in client.app.state.scheduler.status()}
    assert cycles["resume_touch"]["interval_minutes"] == 240


# --- lanes must not interfere ------------------------------------------

async def test_lanes_do_not_close_each_others_tabs():
    """Starting activity used to close the tab the collector was working in."""
    from src.browser.connector import LIVE_PAGES, BrowserConnector

    collector_tab = FakePage("https://hh.ru/search?page=2")
    LIVE_PAGES.add(collector_tab)  # the collector is working here right now
    abandoned_tab = FakePage("https://hh.ru/vacancy/999")

    activity = BrowserConnector(BrowserConfig(close_stale_tabs=True))
    activity._created_page = FakePage("about:blank")

    try:
        await activity._close_stale_tabs(FakeContext([collector_tab, abandoned_tab,
                                                      activity._created_page]))
        assert not collector_tab.closed   # another lane is using it
        assert abandoned_tab.closed       # a real leftover is still cleaned up
    finally:
        LIVE_PAGES.discard(collector_tab)


async def test_background_requests_do_not_wait_for_page_spacing():
    """A page-opening pause must not block the other lane's XHR."""
    import time

    from src.ratelimit import RateLimitConfig, RateLimiter

    limiter = RateLimiter(
        RateLimitConfig(min_interval_sec=1.0, jitter_sec=0.0, requests_per_minute=100)
    )
    start = time.monotonic()
    xhr_times = []

    async def page_openings():
        for _ in range(3):
            await limiter.acquire()

    async def background_requests():
        for _ in range(5):
            await limiter.acquire(spacing=False)
            xhr_times.append(time.monotonic() - start)

    await asyncio.gather(page_openings(), background_requests())

    # All background requests fit in the burst budget and go through at once.
    assert max(xhr_times) < 0.5, xhr_times


async def test_page_openings_stay_spaced_while_sharing_the_limiter():
    """Uses a controlled clock: wall-clock timing is flaky under load."""
    from src.ratelimit import RateLimitConfig, RateLimiter

    class FakeClock:
        def __init__(self):
            self.now = 1000.0

        def time(self):
            return self.now

        async def sleep(self, seconds):
            self.now += seconds
            await asyncio.sleep(0)  # let the other lane run

    clock = FakeClock()
    limiter = RateLimiter(
        RateLimitConfig(min_interval_sec=0.4, jitter_sec=0.0, requests_per_minute=600),
        clock=clock.time,
        sleep=clock.sleep,
    )
    stamps = []

    async def lane(count):
        for _ in range(count):
            await limiter.acquire()
            stamps.append(round(clock.now - 1000.0, 2))

    await asyncio.gather(lane(3), lane(3))

    # Six openings, 0.4s apart, from one shared queue: the first is free and the
    # remaining five are spaced, so the run takes 5 * 0.4 = 2.0s.
    # Per-lane limiters would finish in 0.8s; double-spacing would take 4s.
    elapsed = round(clock.now - 1000.0, 2)
    assert len(stamps) == 6
    assert 1.9 <= elapsed <= 2.2, f"elapsed={elapsed}, stamps={stamps}"


# --- collecting must not crawl like the activity module ------------------

def test_collector_scroll_engine_uses_the_settings():
    """The engine used to ignore them and always crawl at 150px per 0.7s."""
    from src.collector.collector import HHVacancyCardCollector
    from src.config import ScrollerConfig

    config = ScrollerConfig(
        scroll_step_min=800, scroll_step_max=1400,
        scroll_pause_min_sec=0.05, scroll_pause_max_sec=0.15,
        max_scroll_steps_per_page=42, stable_cycles=2,
    )
    engine = HHVacancyCardCollector().build_scroll_engine(config)

    assert engine.wheel_step_min_px == 800
    assert engine.wheel_step_max_px == 1400
    assert engine.step_delay_sec == 0.05
    assert engine.post_step_wait_sec == 0.15
    assert engine.max_scroll_steps_per_page == 42
    assert engine.stable_height_cycles_threshold == 2


def test_collecting_is_much_faster_than_imitation():
    from src.collector.collector import HHVacancyCardCollector
    from src.config import Config

    config = Config.load("configs/config.local.yaml")
    engine = HHVacancyCardCollector().build_scroll_engine(config.scroller)

    average_step = (engine.wheel_step_min_px + engine.wheel_step_max_px) / 2
    seconds_per_step = engine.step_delay_sec + engine.post_step_wait_sec
    pixels_per_second = average_step / seconds_per_step

    assert pixels_per_second > 800, f"collecting crawls at {pixels_per_second:.0f} px/s"


def test_inverted_step_range_is_tolerated():
    from src.collector.scroll_engine import ScrollEngine

    engine = ScrollEngine(wheel_step_min_px=900, wheel_step_max_px=300)
    assert engine.wheel_step_max_px >= engine.wheel_step_min_px


def test_scroll_pace_settings_reach_the_config(client):
    client.post(
        "/actions/settings",
        data={
            "scroller.scroll_step_min": "900",
            "scroller.scroll_step_max": "1500",
            "scroller.scroll_pause_min_sec": "0.05",
            "scroller.scroll_pause_max_sec": "0.2",
        },
        follow_redirects=False,
    )
    scroller = client.app.state.settings.scroller_config()
    assert scroller.scroll_step_min == 900
    assert scroller.scroll_step_max == 1500
    assert scroller.scroll_pause_min_sec == 0.05
    assert scroller.scroll_pause_max_sec == 0.2


# --- shared browser session ---------------------------------------------

async def test_browser_session_is_shared_and_reference_counted(monkeypatch):
    from src.browser import session as session_module

    connects = {"count": 0, "disconnects": 0}

    class FakeBrowser:
        def __init__(self):
            self.contexts = ["context"]

        def is_connected(self):
            return True

        async def close(self):
            pass

    shared = session_module.SharedBrowserSession()

    async def fake_connect(config):
        connects["count"] += 1
        shared._browser = FakeBrowser()
        shared._context = "context"

    async def fake_disconnect():
        connects["disconnects"] += 1
        shared._browser = None
        shared._context = None

    monkeypatch.setattr(shared, "_connect", fake_connect)
    monkeypatch.setattr(shared, "_disconnect", fake_disconnect)

    await shared.acquire(BrowserConfig())
    await shared.acquire(BrowserConfig())   # the second lane joins in
    assert connects["count"] == 1           # one connection for both

    await shared.release()
    assert shared.is_connected              # still used by the first lane
    await shared.release()
    assert connects["disconnects"] >= 1     # released by the last user


# --- collecting without scrolling ---------------------------------------

class GrowingListPage:
    """Page whose card list fills in over time, the way hh.ru does."""

    def __init__(self, counts):
        self.counts = list(counts)
        self.calls = 0
        self.scrolled = False

    async def wait_for_selector(self, selector, **kwargs):
        return object()

    async def query_selector_all(self, selector):
        count = self.counts[min(self.calls, len(self.counts) - 1)]
        self.calls += 1
        return [object()] * count


async def test_wait_returns_only_after_the_list_stops_growing():
    from src.collector.collector import HHVacancyCardCollector

    # 20 cards first, then the rest arrive, then it stays at 50.
    page = GrowingListPage([20, 20, 35, 50, 50, 50, 50])
    found = await HHVacancyCardCollector()._wait_for_cards(page, timeout_sec=10, poll_sec=0)

    assert found == 50  # not the 20 that were there at first


async def test_wait_gives_up_on_an_empty_result_page():
    from src.collector.collector import HHVacancyCardCollector

    class EmptyPage(GrowingListPage):
        async def wait_for_selector(self, selector, **kwargs):
            raise TimeoutError("no cards")

    found = await HHVacancyCardCollector()._wait_for_cards(EmptyPage([0]), timeout_sec=1, poll_sec=0)
    assert found == 0


async def test_wait_respects_the_timeout_if_the_list_never_settles():
    import time

    from src.collector.collector import HHVacancyCardCollector

    class NeverStable(GrowingListPage):
        async def query_selector_all(self, selector):
            self.calls += 1
            return [object()] * self.calls  # grows forever

    started = time.monotonic()
    found = await HHVacancyCardCollector()._wait_for_cards(NeverStable([1]), timeout_sec=1, poll_sec=0.05)
    assert time.monotonic() - started < 3
    assert found > 0


def test_instant_is_the_default_load_mode(client):
    assert client.app.state.settings.scroller_config().load_mode == "instant"

    page = client.get("/settings").text
    assert 'name="scroller.load_mode"' in page

    client.post("/actions/settings", data={"scroller.load_mode": "scroll"}, follow_redirects=False)
    assert client.app.state.settings.scroller_config().load_mode == "scroll"
