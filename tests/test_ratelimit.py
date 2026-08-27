"""Internal rate limiting: spacing, per-minute ceiling, waiting behaviour.

The clock and sleep are injected, so the tests verify exact timings without
actually waiting.
"""

import asyncio

import pytest

from src.ratelimit import PacedLogger, RateLimitConfig, RateLimiter


class FakeClock:
    """Monotonic clock that only moves when someone sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def slept(self) -> float:
        return round(sum(self.sleeps), 3)


def make_limiter(**overrides) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    defaults = {"enabled": True, "min_interval_sec": 1.0, "jitter_sec": 0.0, "requests_per_minute": 100}
    config = RateLimitConfig(**{**defaults, **overrides})
    return RateLimiter(config, clock=clock.time, sleep=clock.sleep), clock


async def test_first_request_is_not_delayed():
    limiter, clock = make_limiter()
    assert await limiter.acquire() == 0.0
    assert clock.slept == 0.0


async def test_consecutive_pages_are_spaced_by_the_interval():
    limiter, clock = make_limiter(min_interval_sec=1.0)

    await limiter.acquire()
    waited = await limiter.acquire()

    assert waited == pytest.approx(1.0, abs=0.26)  # sleeps in 0.25s chunks
    assert clock.slept == pytest.approx(1.0, abs=0.26)


async def test_ten_vacancies_take_about_ten_seconds():
    limiter, clock = make_limiter(min_interval_sec=1.0)

    for _ in range(10):
        await limiter.acquire()

    # First one is free, the other nine are spaced out.
    assert clock.slept == pytest.approx(9.0, abs=0.5)
    assert limiter.stats.acquired == 10


async def test_jitter_adds_to_the_interval_but_stays_in_range():
    limiter, clock = make_limiter(min_interval_sec=1.0, jitter_sec=0.5)

    await limiter.acquire()
    for _ in range(5):
        await limiter.acquire()

    per_request = clock.slept / 5
    assert 1.0 <= per_request <= 1.75


async def test_per_minute_ceiling_blocks_a_burst():
    # No spacing: only the per-minute budget applies, as for page XHRs.
    limiter, clock = make_limiter(min_interval_sec=0.0, requests_per_minute=60)
    burst = limiter.config.burst  # 60 // 4 = 15

    for _ in range(burst):
        assert await limiter.acquire(spacing=False) == 0.0
    assert clock.slept == 0.0

    # The bucket is empty: the next one waits for a refill (60/min -> 1/s).
    waited = await limiter.acquire(spacing=False)
    assert waited == pytest.approx(1.0, abs=0.26)


async def test_budget_refills_over_time():
    limiter, clock = make_limiter(min_interval_sec=0.0, requests_per_minute=60)
    for _ in range(limiter.config.burst):
        await limiter.acquire(spacing=False)

    clock.now += 30  # idle half a minute
    assert await limiter.acquire(spacing=False) == 0.0
    assert clock.slept == 0.0


async def test_waiting_costs_no_polling():
    """One sleep of the computed length, not a loop of checks."""
    limiter, clock = make_limiter(min_interval_sec=5.0)

    await limiter.acquire()
    await limiter.acquire()

    # 5s at 0.25s chunks = 20 sleeps; a polling design would re-check far more.
    assert len(clock.sleeps) == 20
    assert all(chunk <= 0.25 for chunk in clock.sleeps)


async def test_wait_time_reports_without_consuming():
    limiter, clock = make_limiter(min_interval_sec=2.0)
    await limiter.acquire()

    assert limiter.wait_time() == pytest.approx(2.0, abs=0.01)
    assert limiter.wait_time() == pytest.approx(2.0, abs=0.01)  # no side effect
    assert limiter.stats.acquired == 1


async def test_disabled_limiter_never_waits():
    limiter, clock = make_limiter()
    limiter.reconfigure(RateLimitConfig(enabled=False))

    for _ in range(50):
        assert await limiter.acquire() == 0.0
    assert clock.slept == 0.0


async def test_stop_interrupts_a_long_wait():
    limiter, clock = make_limiter(min_interval_sec=30.0)
    await limiter.acquire()

    stopping = {"value": False}

    async def stop_soon():
        await asyncio.sleep(0)
        stopping["value"] = True

    # The flag flips before the wait starts: the limiter must not sleep it out.
    await stop_soon()
    with pytest.raises(asyncio.CancelledError):
        await limiter.acquire(should_stop=lambda: stopping["value"])


async def test_reconfigure_applies_new_limits_immediately():
    limiter, clock = make_limiter(min_interval_sec=1.0)
    await limiter.acquire()

    limiter.reconfigure(RateLimitConfig(min_interval_sec=10.0, jitter_sec=0.0, requests_per_minute=100))
    waited = await limiter.acquire()
    assert waited == pytest.approx(10.0, abs=0.3)


async def test_stats_are_collected():
    limiter, clock = make_limiter(min_interval_sec=1.0)
    for _ in range(3):
        await limiter.acquire()

    stats = limiter.stats.as_dict()
    assert stats["acquired"] == 3
    assert stats["waits"] == 2
    assert stats["total_wait_sec"] >= 2.0
    assert stats["max_wait_sec"] >= 1.0


def test_paced_logger_reports_once_per_quiet_period():
    lines = []
    limiter, _ = make_limiter()
    reporter = PacedLogger(lines.append, threshold_sec=1.0, quiet_sec=60.0)

    reporter.report(0.2, limiter)   # below the threshold
    reporter.report(5.0, limiter)   # reported
    reporter.report(6.0, limiter)   # muted, still inside the quiet period

    assert len(lines) == 1
    assert "лимит" in lines[0]


async def test_settings_feed_the_limiter(tmp_path):
    from fastapi.testclient import TestClient

    from src.config import Config
    from src.web.app import create_app

    config = Config.load("configs/config.local.yaml")
    config.db.sqlite_path = str(tmp_path / "limits.db")

    with TestClient(create_app(config)) as client:
        client.post(
            "/actions/settings",
            data={
                "ratelimit.enabled": "1",
                "ratelimit.min_interval_sec": "2.5",
                "ratelimit.requests_per_minute": "42",
                "ratelimit.jitter_sec": "0",
            },
            follow_redirects=False,
        )
        limiter = client.app.state.limiter
        assert limiter.config.min_interval_sec == 2.5
        assert limiter.config.requests_per_minute == 42
        # The running task manager shares that very instance.
        assert client.app.state.tasks.limiter is limiter


# --- browser request throttling -----------------------------------------

class FakeRequest:
    def __init__(self, url: str, resource_type: str):
        self.url = url
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self):
        self.continued = False

    async def continue_(self) -> None:
        self.continued = True


class FakePage:
    def __init__(self):
        self.handler = None
        self.pattern = None

    async def route(self, pattern, handler) -> None:
        self.pattern = pattern
        self.handler = handler


async def install_handler(**overrides):
    from src.browser.connector import BrowserConnector
    from src.config import BrowserConfig

    limiter, clock = make_limiter(**overrides)
    calls = []
    original = limiter.acquire

    async def counting_acquire(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    limiter.acquire = counting_acquire
    page = FakePage()
    await BrowserConnector(BrowserConfig(), limiter=limiter)._install_throttle(page)
    return page, calls


async def test_page_and_xhr_requests_are_counted():
    page, calls = await install_handler()

    for resource_type in ("document", "xhr", "fetch"):
        route = FakeRoute()
        await page.handler(route, FakeRequest("https://hh.ru/vacancy/1", resource_type))
        assert route.continued

    assert len(calls) == 3
    # Background requests must not be spaced like page openings.
    assert all(call.get("spacing") is False for call in calls)


async def test_assets_and_foreign_hosts_are_not_counted():
    page, calls = await install_handler()

    for url, resource_type in (
        ("https://hh.ru/logo.png", "image"),
        ("https://hhcdn.ru/bundle.js", "script"),
        ("https://example.com/api", "xhr"),
    ):
        route = FakeRoute()
        await page.handler(route, FakeRequest(url, resource_type))
        assert route.continued  # nothing is ever blocked

    assert calls == []


async def test_request_continues_even_if_the_limiter_fails():
    from src.browser.connector import BrowserConnector
    from src.config import BrowserConfig

    limiter, _ = make_limiter()

    async def broken_acquire(**kwargs):
        raise RuntimeError("limiter exploded")

    limiter.acquire = broken_acquire
    page = FakePage()
    await BrowserConnector(BrowserConfig(), limiter=limiter)._install_throttle(page)

    route = FakeRoute()
    await page.handler(route, FakeRequest("https://hh.ru/search", "document"))
    assert route.continued  # page loading must not depend on the limiter


async def test_throttling_can_be_switched_off():
    page, calls = await install_handler(throttle_browser_requests=False)
    assert page.handler is None  # no interception installed at all
