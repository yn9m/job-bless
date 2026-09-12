import asyncio
import logging
import time
from asyncio import CancelledError, wait_for
from contextlib import AsyncExitStack
from typing import AsyncGenerator, Optional, Union, Callable
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.config import BrowserConfig, ScrollerConfig
from src.browser.connector import BrowserConnector
from src.browser.errors import is_transient_browser_error
from src.browser.intervention import HHInterventionRequired
from src.collector.card_parser import HHSelectors, VacancyCardParser
from src.collector.scroll_engine import ScrollEngine
from src.collector.page_guard import HHPageGuard
from src.collector.popup_handler import PopupHandler
from src.db.models import VacancyCard, CollectionSummary, PageCommitParams

logger = logging.getLogger(__name__)


class SearchPageNotReady(RuntimeError):
    """HH has not provided a usable result page yet; keep the current checkpoint."""


class HHVacancyCardCollector:
    """
    Direct in-memory Python collector. Operates Playwright, scrolls pages,
    parses HH vacancy cards incrementally, and yields cards & page commit parameters.
    """

    MAX_ATTEMPTS = 3
    PAGE_READ_TIMEOUT_SEC = 90

    NEXT_PAGE_SELECTORS = [
        '[data-qa="pager-next"]',
        'a[data-qa="pager-next"]',
        'a.bloko-button[data-qa="pager-next"]',
    ]
    EMPTY_RESULTS_SELECTOR = '[data-qa="empty-vacancy-search-block"]'

    def __init__(
        self,
        card_parser: Optional[VacancyCardParser] = None,
        scroll_engine: Optional[ScrollEngine] = None,
        page_guard: Optional[HHPageGuard] = None,
        popup_handler: Optional[PopupHandler] = None,
    ):
        self.card_parser = card_parser or VacancyCardParser()
        self.page_guard = page_guard or HHPageGuard()
        self.popup_handler = popup_handler or PopupHandler()
        self.scroll_engine = scroll_engine or ScrollEngine(
            page_guard=self.page_guard,
            popup_handler=self.popup_handler,
        )
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True
        self.scroll_engine.stop()

    async def _wait_for_cards(
        self,
        page: Page,
        timeout_sec: float,
        settle_checks: int = 2,
        poll_sec: float = 0.4,
    ) -> int:
        """Wait until the result list stops growing; return the number of cards.

        hh.ru fills the list in after the initial render — around 20 cards are
        there first and the rest appear a second later. Scrolling has nothing to
        do with it, so the wait is for the count to stabilise. Polling the DOM
        costs no requests.
        """
        try:
            await page.wait_for_selector(
                f'{HHSelectors.VACANCY_CARD}, {self.EMPTY_RESULTS_SELECTOR}',
                timeout=max(1.0, timeout_sec) * 1000, state="attached",
            )
        except PlaywrightTimeoutError as error:
            await self.page_guard.check_page_state(page, is_navigation_step=True)
            raise SearchPageNotReady('HH не загрузил карточки вакансий; повторяю эту страницу') from error

        if not await page.query_selector_all(HHSelectors.VACANCY_CARD):
            empty = await page.query_selector(self.EMPTY_RESULTS_SELECTOR)
            if empty and await empty.is_visible():
                return 0
            raise SearchPageNotReady('HH пока не показал ни вакансии, ни сообщение о пустой выдаче')

        deadline = time.monotonic() + max(1.0, timeout_sec)
        last_count, stable = -1, 0

        while time.monotonic() < deadline:
            count = len(await page.query_selector_all(HHSelectors.VACANCY_CARD))
            if count == last_count and count > 0:
                stable += 1
                if stable >= settle_checks:
                    break
            else:
                stable = 0
                last_count = count
            await asyncio.sleep(poll_sec)

        logger.info(f"Result list ready: {last_count} cards on the page.")
        if last_count <= 0:
            raise SearchPageNotReady('Карточки исчезли до окончания загрузки страницы HH')
        return last_count

    async def _read_page(self, page, page_number, current_url, sc_cfg):
        """Keep an attempt local: a crash halfway through must not mark cards as saved."""
        candidates = {}

        async def parse_step():
            for card, _ in await self.card_parser.parse_cards_from_page(
                page, page_number=page_number, search_url=current_url,
            ):
                candidates[(card.source, card.external_id)] = card

        found = await self._wait_for_cards(page, sc_cfg.page_timeout_sec)
        if not found:
            return [], False
        if sc_cfg.load_mode == 'instant':
            await parse_step()
            if not candidates:
                await self.scroll_engine.scroll_page(page, on_step_callback=parse_step)
        else:
            await self.scroll_engine.scroll_page(page, on_step_callback=parse_step)
            await parse_step()
        if not candidates and not self._stop_requested:
            raise SearchPageNotReady('Карточки HH найдены, но прочитать их не удалось')
        return list(candidates.values()), True

    async def _retry_pause(self, attempt):
        remaining = min(30, 2 ** min(attempt, 5))
        while remaining > 0 and not self._stop_requested:
            interval = min(.25, remaining)
            await asyncio.sleep(interval)
            remaining -= interval

    def build_scroll_engine(self, sc_cfg: ScrollerConfig) -> ScrollEngine:
        """Scroll engine for collecting: fast, driven by the settings.

        Collecting scrolls to load the list, not to look human — the imitation
        of a live user is a separate module with its own pace.
        """
        return ScrollEngine(
            wheel_step_min_px=sc_cfg.scroll_step_min,
            wheel_step_max_px=sc_cfg.scroll_step_max,
            step_delay_sec=sc_cfg.scroll_pause_min_sec,
            post_step_wait_sec=sc_cfg.scroll_pause_max_sec,
            max_scroll_steps_per_page=sc_cfg.max_scroll_steps_per_page,
            max_scroll_time_sec_per_page=sc_cfg.max_scroll_time_sec_per_page,
            stable_height_cycles_threshold=sc_cfg.stable_cycles,
            page_guard=self.page_guard,
            popup_handler=self.popup_handler,
        )

    async def collect(
        self,
        browser_config: BrowserConfig,
        search_url: str,
        task_id: str,
        scroller_config: Optional[ScrollerConfig] = None,
        limiter=None,
        on_retry: Optional[Callable[[str], None]] = None,
    ) -> AsyncGenerator[Union[VacancyCard, PageCommitParams, CollectionSummary], None]:
        if not task_id:
            raise ValueError("task_id must not be empty.")

        sc_cfg = scroller_config or ScrollerConfig()
        self.scroll_engine = self.build_scroll_engine(sc_cfg)
        self._stop_requested = False
        start_timestamp = time.monotonic()

        summary = CollectionSummary(
            task_id=task_id,
            last_processed_url=search_url,
        )

        seen_vacancies = set()
        visited_urls = set()
        resume_url = search_url
        page_number = 1
        page_committed = False
        page = None
        failures = 0
        connector = BrowserConnector(browser_config, limiter=limiter)
        try:
            async with AsyncExitStack() as tabs:
                while not self._stop_requested:
                    try:
                        if page is None:
                            page = await wait_for(tabs.enter_async_context(connector.connect()), 30)
                            self.popup_handler.setup_dialog_handler(page)
                            logger.info("Opening page #%s for task '%s': %s", page_number, task_id, resume_url)
                            if limiter:
                                await limiter.acquire(should_stop=lambda: self._stop_requested)
                            response = await page.goto(resume_url, wait_until="domcontentloaded", timeout=browser_config.timeout_ms)
                            await self.page_guard.check_page_state(page, is_navigation_step=True)
                            if response and response.status in (401, 403, 429):
                                raise HHInterventionRequired('HH ограничил доступ. Откройте браузер HH и проверьте сообщение сайта.')
                            if response and response.status >= 500:
                                raise SearchPageNotReady(f'HH временно недоступен (HTTP {response.status})')
                            if response and response.status >= 400:
                                raise RuntimeError(f'Страница поиска недоступна (HTTP {response.status})')
                            await self.popup_handler.dismiss_known_overlays(page)
                            # Recovery of a committed page must wait for its pager too.
                            if page_committed:
                                if not await self._wait_for_cards(page, sc_cfg.page_timeout_sec):
                                    raise SearchPageNotReady('HH не восстановил уже обработанную страницу')

                        await self.page_guard.check_page_state(page, is_navigation_step=True)
                        if not page_committed:
                            resume_url = page.url
                            if resume_url in visited_urls:
                                raise RuntimeError('HH вернул уже обработанную страницу вместо следующей')
                            logger.info("Task '%s' -> Processing page #%s: %s", task_id, page_number, resume_url)
                            candidates, has_results = await wait_for(
                                self._read_page(page, page_number, resume_url, sc_cfg), self.PAGE_READ_TIMEOUT_SEC,
                            )
                            if self._stop_requested and not candidates:
                                break
                            page_key = f'page_{page_number}'
                            page_cards = []
                            for card in candidates:
                                key = (card.source, card.external_id)
                                if key in seen_vacancies:
                                    summary.duplicate_cards += 1
                                    continue
                                seen_vacancies.add(key)
                                card.page_key, card.page_number = page_key, page_number
                                page_cards.append(card)
                            summary.total_cards_found += len(page_cards)
                            summary.unique_vacancies += len(page_cards)
                            summary.total_pages_processed += 1
                            summary.last_processed_url = resume_url
                            visited_urls.add(resume_url)
                            page_committed = True
                            yield PageCommitParams(
                                search_run_id=task_id, page_key=page_key, page_number=page_number,
                                current_url=resume_url, canonical_url=resume_url, cards=page_cards,
                            )
                            if not has_results:
                                summary.completion_reason = 'no_results'
                                break

                        if self._stop_requested:
                            break
                        if limiter:
                            await limiter.acquire(should_stop=lambda: self._stop_requested)
                        if self._stop_requested:
                            break
                        if not await wait_for(self._go_to_next_page(page), 45):
                            summary.completion_reason = 'no_more_pages'
                            break
                        resume_url = page.url
                        page_number += 1
                        page_committed = False
                        failures = 0
                    except Exception as error:
                        if not isinstance(error, SearchPageNotReady) and not is_transient_browser_error(error):
                            raise
                        # Preserve the last committed page and its cards. A retry
                        # reopens only this page, never restarts the entire search.
                        await wait_for(tabs.aclose(), 5)
                        page = None
                        failures += 1
                        if failures >= self.MAX_ATTEMPTS:
                            raise RuntimeError(
                                f'Страница #{page_number}: не удалось продолжить сбор за {self.MAX_ATTEMPTS} попытки. '
                                'Уже собранные вакансии сохранены. Попробуйте запустить сбор позже.'
                            ) from error
                        message = (f'страница #{page_number}: сбой загрузки, восстанавливаю сбор '
                                   f'(попытка {failures + 1}/{self.MAX_ATTEMPTS}) '
                                   f'через {min(30, 2 ** min(failures, 5))} с — {(str(error) or "таймаут").splitlines()[0][:180]}')
                        logger.warning(message)
                        if on_retry:
                            on_retry(message)
                        await self._retry_pause(failures)
                if self._stop_requested:
                    summary.completion_reason = 'stopped_by_user'
                    summary.final_status = 'cancelled'
        except (CancelledError, GeneratorExit):
            summary.completion_reason = 'stopped_by_user' if self._stop_requested else 'cancelled'
            summary.final_status = 'cancelled'
            raise
        except Exception as e:
            logger.error(f"Error during collection task '{task_id}': {e}", exc_info=True)
            summary.completion_reason = f"error: {e}"
            summary.final_status = "failed"
            raise

        finally:
            summary.duration_seconds = round(time.monotonic() - start_timestamp, 2)
            logger.info(
                f"Completed collection for task '{task_id}': "
                f"pages={summary.total_pages_processed}, unique_cards={summary.unique_vacancies}, reason={summary.completion_reason}"
            )
        yield summary

    async def _go_to_next_page(self, page: Page) -> bool:
        next_button = None
        for sel in self.NEXT_PAGE_SELECTORS:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible() and await elem.is_enabled():
                next_button = elem
                break

        if not next_button:
            return False

        logger.info("Clicking next page button...")
        old_url = page.url
        previous_cards = await page.evaluate('''() => Array.from(
            document.querySelectorAll('[data-qa="vacancy-serp__vacancy"] [data-qa="serp-item__title"]')
        ).map(link => link.href.split('?')[0]).join('|')''')
        await next_button.click()

        try:
            await page.wait_for_function(
                "oldUrl => window.location.href !== oldUrl",
                arg=old_url,
                timeout=10000,
            )
            # HH changes history before its asynchronous search response arrives.
            # A stable count alone can still describe the preceding 50 cards.
            await page.wait_for_function('''({previous, emptySelector}) => {
                const links = Array.from(document.querySelectorAll(
                    '[data-qa="vacancy-serp__vacancy"] [data-qa="serp-item__title"]'));
                const current = links.map(link => link.href.split('?')[0]).join('|');
                const empty = document.querySelector(emptySelector);
                return (links.length > 0 && current !== previous) ||
                    (empty && empty.getClientRects().length > 0 && links.length === 0);
            }''', arg={'previous': previous_cards, 'emptySelector': self.EMPTY_RESULTS_SELECTOR}, timeout=30000)
        except PlaywrightTimeoutError as error:
            raise SearchPageNotReady('HH не загрузил выдачу следующей страницы') from error

        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
        return True
