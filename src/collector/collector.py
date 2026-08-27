import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import AsyncGenerator, Set, Tuple, Optional, Union, Dict, Any
from playwright.async_api import Page

from src.config import BrowserConfig, ScrollerConfig
from src.browser.connector import BrowserConnector
from src.collector.card_parser import HHSelectors, VacancyCardParser
from src.collector.scroll_engine import ScrollEngine
from src.collector.page_guard import HHPageGuard
from src.collector.popup_handler import PopupHandler
from src.db.models import VacancyCard, CollectionSummary, PageCommitParams

logger = logging.getLogger(__name__)


class HHVacancyCardCollector:
    """
    Direct in-memory Python collector. Operates Playwright, scrolls pages,
    parses HH vacancy cards incrementally, and yields cards & page commit parameters.
    """

    NEXT_PAGE_SELECTORS = [
        '[data-qa="pager-next"]',
        'a[data-qa="pager-next"]',
        'a.bloko-button[data-qa="pager-next"]',
    ]

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
                HHSelectors.VACANCY_CARD, timeout=max(1.0, timeout_sec) * 1000, state="attached"
            )
        except Exception as e:  # noqa: BLE001 - an empty result page is valid
            logger.warning(f"No vacancy cards appeared within {timeout_sec:.0f}s: {e}")
            return 0

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
        return max(0, last_count)

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

        seen_vacancies: Set[Tuple[str, str]] = set()  # (source, external_id)
        visited_urls: Set[str] = set()

        connector = BrowserConnector(browser_config, limiter=limiter)
        try:
            async with connector.connect() as page:
                self.popup_handler.setup_dialog_handler(page)

                logger.info(f"Opening search URL for task '{task_id}': {search_url}")
                if limiter:
                    await limiter.acquire(should_stop=lambda: self._stop_requested)
                await page.goto(search_url, wait_until="domcontentloaded", timeout=browser_config.cdp.timeout_ms)
                await asyncio.sleep(1.0)

                await self.page_guard.check_page_state(page, is_navigation_step=True)
                await self.popup_handler.dismiss_known_overlays(page)

                page_number = 1
                while not self._stop_requested:
                    current_page_url = page.url
                    if current_page_url in visited_urls:
                        logger.warning(f"Loop detected on URL: '{current_page_url}'. Halting collection.")
                        summary.completion_reason = "loop_detected"
                        break
                    visited_urls.add(current_page_url)
                    summary.last_processed_url = current_page_url

                    logger.info(f"Task '{task_id}' -> Processing page #{page_number}: {current_page_url}")
                    page_key = f"page_{page_number}"
                    page_cards: list[VacancyCard] = []

                    async def incremental_card_parser_step() -> None:
                        nonlocal page_cards
                        step_cards = await self.card_parser.parse_cards_from_page(
                            page, page_number=page_number, search_url=current_page_url
                        )
                        new_cards_in_step = []
                        for card, err_msg in step_cards:
                            card_key = (card.source, card.external_id)
                            if card_key in seen_vacancies:
                                summary.duplicate_cards += 1
                                continue

                            seen_vacancies.add(card_key)
                            card.page_key = page_key
                            card.page_number = page_number
                            summary.total_cards_found += 1
                            summary.unique_vacancies += 1
                            page_cards.append(card)
                            new_cards_in_step.append(card)

                    # 3. Read the cards. hh.ru renders the whole page of results
                    # server-side, so scrolling reveals nothing new — waiting for
                    # the list and parsing it once is both faster and quieter.
                    if sc_cfg.load_mode == "instant":
                        found = await self._wait_for_cards(page, sc_cfg.page_timeout_sec) > 0
                        await incremental_card_parser_step()

                        if not page_cards and found:
                            logger.warning(
                                "Cards are present but none were parsed — falling back to scrolling."
                            )
                            await self.scroll_engine.scroll_page(
                                page, on_step_callback=incremental_card_parser_step
                            )
                    else:
                        await self.scroll_engine.scroll_page(
                            page, on_step_callback=incremental_card_parser_step
                        )
                        # Final pass to capture any remaining unparsed cards on page
                        await incremental_card_parser_step()

                    # Yield page commit params to trigger database transaction for page
                    page_params = PageCommitParams(
                        search_run_id=task_id,
                        page_key=page_key,
                        page_number=page_number,
                        current_url=current_page_url,
                        canonical_url=current_page_url,
                        cards=page_cards,
                    )
                    yield page_params

                    summary.total_pages_processed += 1

                    if self._stop_requested:
                        summary.completion_reason = "stopped_by_user"
                        break

                    if page_number >= sc_cfg.max_pages:
                        logger.info(f"Reached max pages limit ({sc_cfg.max_pages}). Stopping.")
                        summary.completion_reason = "max_pages_reached"
                        break

                    # 4. Navigate to Next Page
                    if limiter:
                        await limiter.acquire(should_stop=lambda: self._stop_requested)
                    has_next = await self._go_to_next_page(page)
                    if not has_next:
                        logger.info("No next page button found. Finished search results.")
                        summary.completion_reason = "no_more_pages"
                        break

                    page_number += 1

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
            try:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible() and await elem.is_enabled():
                    next_button = elem
                    break
            except Exception:
                pass

        if not next_button:
            return False

        logger.info("Clicking next page button...")
        old_url = page.url
        await next_button.click()

        try:
            await page.wait_for_function(
                "oldUrl => window.location.href !== oldUrl",
                arg=old_url,
                timeout=10000,
            )
        except Exception:
            await asyncio.sleep(2.0)

        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
        return True
