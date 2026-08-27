"""Internal rate limiting for everything the bot does against hh.ru.

Two layers, both configurable:

* a minimum interval between page openings — vacancies are processed slowly,
  one at a time, instead of in a burst;
* a token bucket over every meaningful HTTP request (documents and XHR),
  capped at N requests per minute.

The caller never polls: `acquire()` computes exactly how long the limit needs
and sleeps for that time, so waiting costs nothing.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Sleep is split into chunks of this size so a stop request is noticed quickly.
SLEEP_CHUNK_SEC = 0.25


@dataclass
class RateLimitConfig:
    enabled: bool = True
    # Floor between two page openings (a vacancy, a search page, a resume).
    min_interval_sec: float = 1.0
    # Random extra on top of the interval, so the pace is not machine-perfect.
    jitter_sec: float = 0.5
    # Ceiling over every request the browser makes to hh.ru.
    requests_per_minute: int = 100
    # Whether to throttle browser requests at all (documents/XHR interception).
    throttle_browser_requests: bool = True

    @property
    def burst(self) -> int:
        """Requests allowed back-to-back after an idle period."""
        return max(1, self.requests_per_minute // 4)


@dataclass
class RateLimitStats:
    acquired: int = 0
    waits: int = 0
    total_wait_sec: float = 0.0
    max_wait_sec: float = 0.0

    def as_dict(self) -> dict:
        return {
            "acquired": self.acquired,
            "waits": self.waits,
            "total_wait_sec": round(self.total_wait_sec, 1),
            "max_wait_sec": round(self.max_wait_sec, 1),
        }


class RateLimiter:
    """Token bucket plus a minimum spacing between operations.

    Shared by every job, so the budget is global: the collector, the applier and
    the resume import cannot each spend a full quota of their own.
    """

    def __init__(
        self,
        config: Optional[RateLimitConfig] = None,
        *,
        name: str = "hh",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.name = name
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self.stats = RateLimitStats()
        self.config = config or RateLimitConfig()
        self._tokens = float(self.config.burst)
        self._updated_at = clock()
        self._next_slot_at = 0.0
        self._last_acquired_at = 0.0

    # --- configuration ----------------------------------------------------

    def reconfigure(self, config: RateLimitConfig) -> None:
        """Apply new settings without losing the current budget.

        A longer interval takes effect at once — it is measured from the last
        request, not from whatever slot the old setting had scheduled.
        """
        self.config = config
        self._tokens = min(self._tokens, float(config.burst))
        if self._last_acquired_at:
            self._next_slot_at = max(
                self._next_slot_at, self._last_acquired_at + config.min_interval_sec
            )
        logger.info(
            "rate limiter %s: %s, interval=%.1fs, %d req/min",
            self.name,
            "on" if config.enabled else "off",
            config.min_interval_sec,
            config.requests_per_minute,
        )

    # --- inspection -------------------------------------------------------

    def wait_time(self, *, spacing: bool = True) -> float:
        """Seconds until the next operation is allowed, without consuming it."""
        if not self.config.enabled:
            return 0.0
        now = self._clock()
        waits = [self._token_wait(now)]
        if spacing:
            waits.append(max(0.0, self._next_slot_at - now))
        return max(waits)

    # --- acquiring --------------------------------------------------------

    async def acquire(
        self,
        *,
        spacing: bool = True,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> float:
        """Wait until one request is allowed and take it. Returns waited seconds.

        `spacing=False` skips the minimum interval and only respects the
        per-minute ceiling — used for the many small requests a page makes.
        """
        if not self.config.enabled:
            return 0.0

        # The slot is reserved under the lock, but the waiting happens outside
        # it. Holding the lock while sleeping would serialise every caller
        # behind the current pause: a background XHR of one lane would wait out
        # the page-opening interval of another, and the lanes would stop being
        # parallel in any meaningful sense.
        async with self._lock:
            now = self._clock()
            self._refill(now)

            delay = self._token_wait(now)
            if spacing:
                delay = max(delay, self._next_slot_at - now)
            delay = max(0.0, delay)

            # Reserve: the token is spent now, so the next caller queues behind
            # this one instead of racing for the same slot.
            self._tokens -= 1.0
            reserved_at = now + delay
            self._last_acquired_at = reserved_at
            if spacing:
                interval = self.config.min_interval_sec
                if self.config.jitter_sec > 0:
                    interval += random.uniform(0, self.config.jitter_sec)
                self._next_slot_at = reserved_at + interval

        waited = await self._sleep_for(delay, should_stop) if delay > 0 else 0.0

        self.stats.acquired += 1
        if waited > 0:
            self.stats.waits += 1
            self.stats.total_wait_sec += waited
            self.stats.max_wait_sec = max(self.stats.max_wait_sec, waited)
        return waited

    async def _sleep_for(self, delay: float, should_stop: Optional[Callable[[], bool]]) -> float:
        """Sleep in chunks so «Стоп» does not have to wait out the whole pause."""
        remaining = delay
        slept = 0.0
        while remaining > 0:
            if should_stop and should_stop():
                raise asyncio.CancelledError("stopped while waiting for the rate limit")
            chunk = min(SLEEP_CHUNK_SEC, remaining)
            await self._sleep(chunk)
            remaining -= chunk
            slept += chunk
        return slept

    # --- internals --------------------------------------------------------

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        per_second = self.config.requests_per_minute / 60.0
        self._tokens = min(float(self.config.burst), self._tokens + elapsed * per_second)

    def _token_wait(self, now: float) -> float:
        self._refill(now)
        if self._tokens >= 1.0:
            return 0.0
        per_second = self.config.requests_per_minute / 60.0
        if per_second <= 0:
            return float("inf")
        return (1.0 - self._tokens) / per_second


class PacedLogger:
    """Reports long waits to the task journal without flooding it."""

    def __init__(self, log: Callable[[str], None], threshold_sec: float = 1.0, quiet_sec: float = 20.0):
        self._log = log
        self._threshold = threshold_sec
        self._quiet = quiet_sec
        self._last_reported = 0.0

    def report(self, waited: float, limiter: RateLimiter) -> None:
        if waited < self._threshold:
            return
        now = time.monotonic()
        if now - self._last_reported < self._quiet:
            return
        self._last_reported = now
        self._log(
            f"пауза {waited:.1f} с — держу лимит "
            f"{limiter.config.requests_per_minute} запросов/мин"
        )
