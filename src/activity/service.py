"""Activity module: human-like browsing of hh.ru, separate from collecting.

Collecting vacancies and looking active are different goals: the collector
wants pages parsed as fast as the rate limit allows, while this module just
keeps the account alive — slow scrolling, pauses, an occasional vacancy opened.
They run in parallel, on their own settings and their own tab.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.browser.connector import BrowserConnector
from src.config import BrowserConfig

logger = logging.getLogger(__name__)


@dataclass
class ActivityConfig:
    url: str = "https://hh.ru/search/vacancy?text="
    duration_min: float = 10.0
    scroll_step_min: int = 250
    scroll_step_max: int = 700
    pause_min_sec: float = 1.5
    pause_max_sec: float = 5.0
    # Occasionally scroll back up, the way a person re-reads a card.
    scroll_up_chance: float = 0.2
    open_vacancies: bool = False
    open_vacancy_chance: float = 0.15
    read_seconds_min: float = 5.0
    read_seconds_max: float = 20.0


@dataclass
class ActivityReport:
    scrolls: int = 0
    pauses: int = 0
    vacancies_opened: int = 0
    seconds: float = 0.0
    stopped_early: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scrolls": self.scrolls,
            "vacancies_opened": self.vacancies_opened,
            "minutes": round(self.seconds / 60, 1),
            "stopped_early": self.stopped_early,
        }


VACANCY_LINK_SELECTORS = (
    '[data-qa="serp-item__title"]',
    'a[data-qa="vacancy-serp__vacancy-title"]',
    '[data-qa="vacancy-serp__vacancy_title"]',
)


class ActivityScroller:
    """Browses a search page slowly, without parsing or storing anything."""

    def __init__(self, config: ActivityConfig, limiter=None):
        self.config = config
        self.limiter = limiter

    async def run(
        self,
        browser_config: BrowserConfig,
        *,
        should_stop: Optional[Callable[[], bool]] = None,
        log: Optional[Callable[[str], None]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> ActivityReport:
        report = ActivityReport()
        say = log or (lambda message: logger.info(message))
        stop = should_stop or (lambda: False)
        deadline = time.monotonic() + self.config.duration_min * 60
        total_seconds = int(self.config.duration_min * 60)

        connector = BrowserConnector(browser_config, limiter=self.limiter)
        async with connector.connect() as page:
            if self.limiter:
                await self.limiter.acquire(should_stop=stop)
            say(f"открываю {self.config.url}")
            await page.goto(self.config.url, wait_until="domcontentloaded",
                            timeout=browser_config.cdp.timeout_ms)

            started = time.monotonic()
            while time.monotonic() < deadline:
                if stop():
                    report.stopped_early = True
                    break

                await self._scroll_once(page, report)
                await self._idle(report, stop)

                if self.config.open_vacancies and random.random() < self.config.open_vacancy_chance:
                    await self._open_random_vacancy(page, report, say, stop)

                if progress:
                    progress(int(time.monotonic() - started), total_seconds)

            report.seconds = time.monotonic() - started

        say(
            f"активность: {report.scrolls} прокруток, "
            f"{report.vacancies_opened} вакансий открыто, {report.seconds / 60:.1f} мин"
        )
        return report

    async def _scroll_once(self, page, report: ActivityReport) -> None:
        step = random.randint(self.config.scroll_step_min, self.config.scroll_step_max)
        if random.random() < self.config.scroll_up_chance:
            step = -step // 2
        try:
            await page.mouse.wheel(0, step)
            report.scrolls += 1
        except Exception as e:  # noqa: BLE001 - the tab may be gone
            logger.warning("activity scroll failed: %s", e)

    async def _idle(self, report: ActivityReport, stop: Callable[[], bool]) -> None:
        pause = random.uniform(self.config.pause_min_sec, self.config.pause_max_sec)
        report.pauses += 1
        await _sleep_interruptible(pause, stop)

    async def _open_random_vacancy(self, page, report, say, stop) -> None:  # noqa: ANN001
        links: List[Any] = []
        for selector in VACANCY_LINK_SELECTORS:
            try:
                links = await page.query_selector_all(selector)
            except Exception:  # noqa: BLE001
                continue
            if links:
                break
        if not links:
            return

        link = random.choice(links)
        try:
            href = await link.get_attribute("href")
            if not href:
                return
            if self.limiter:
                await self.limiter.acquire(should_stop=stop)
            say(f"смотрю вакансию {href[:70]}")
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            report.vacancies_opened += 1

            await _sleep_interruptible(
                random.uniform(self.config.read_seconds_min, self.config.read_seconds_max), stop
            )
            await self._scroll_once(page, report)

            if self.limiter:
                await self.limiter.acquire(should_stop=stop)
            await page.go_back(wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001 - browsing must never fail the job
            logger.warning("activity could not open a vacancy: %s", e)


async def _sleep_interruptible(seconds: float, stop: Callable[[], bool]) -> None:
    """Sleep in small chunks so «Стоп» is noticed within a fraction of a second."""
    remaining = seconds
    while remaining > 0:
        if stop():
            return
        chunk = min(0.25, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk
