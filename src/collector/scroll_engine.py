import asyncio
import logging
import random
import time
from typing import Callable, Awaitable, Optional
from playwright.async_api import Page

from src.collector.page_guard import HHPageGuard
from src.collector.popup_handler import PopupHandler

logger = logging.getLogger(__name__)


class ScrollEngine:
    """
    Reusable scroll engine performing smooth wheel scrolling and end-of-page detection.
    Allows passing step callbacks for real-time incremental DOM parsing during motion.
    """

    def __init__(
        self,
        wheel_step_px: int = 150,
        sub_steps_per_step: int = 3,
        step_delay_sec: float = 0.2,
        post_step_wait_sec: float = 0.5,
        max_scroll_steps_per_page: int = 300,
        max_scroll_time_sec_per_page: float = 120.0,
        stable_height_cycles_threshold: int = 4,
        page_guard: Optional[HHPageGuard] = None,
        popup_handler: Optional[PopupHandler] = None,
        wheel_step_min_px: Optional[int] = None,
        wheel_step_max_px: Optional[int] = None,
    ):
        # Collecting wants to reach the end of the list quickly; the range comes
        # from the settings, and a fixed step is just a range of one value.
        self.wheel_step_min_px = wheel_step_min_px or wheel_step_px
        self.wheel_step_max_px = max(wheel_step_max_px or wheel_step_px, self.wheel_step_min_px)
        self.wheel_step_px = wheel_step_px
        self.sub_steps_per_step = sub_steps_per_step
        self.step_delay_sec = step_delay_sec
        self.post_step_wait_sec = post_step_wait_sec
        self.max_scroll_steps_per_page = max_scroll_steps_per_page
        self.max_scroll_time_sec_per_page = max_scroll_time_sec_per_page
        self.stable_height_cycles_threshold = stable_height_cycles_threshold

        self.page_guard = page_guard or HHPageGuard()
        self.popup_handler = popup_handler or PopupHandler()
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    async def scroll_page(
        self,
        page: Page,
        on_step_callback: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._stop_requested = False
        start_time = time.monotonic()
        step_count = 0
        consecutive_stable_cycles = 0
        last_scroll_y = -1
        last_doc_height = -1

        while not self._stop_requested:
            step_count += 1
            step_px = random.randint(self.wheel_step_min_px, self.wheel_step_max_px)
            sub_delta = max(10, step_px // max(1, self.sub_steps_per_step))

            # Safeguard limits
            elapsed_time = time.monotonic() - start_time
            if elapsed_time > self.max_scroll_time_sec_per_page:
                logger.warning(
                    f"Scroll timeout reached on page ({elapsed_time:.1f}s > {self.max_scroll_time_sec_per_page}s). Finishing page scroll."
                )
                break
            if step_count > self.max_scroll_steps_per_page:
                logger.warning(
                    f"Max scroll steps reached on page ({step_count} > {self.max_scroll_steps_per_page}). Finishing page scroll."
                )
                break

            # Periodically check page state & overlays
            await self.page_guard.check_page_state(page)
            await self.popup_handler.dismiss_known_overlays(page)

            # Wheel scroll
            for _ in range(self.sub_steps_per_step):
                if self._stop_requested:
                    break
                await page.mouse.wheel(0, sub_delta)
                await asyncio.sleep(self.step_delay_sec / max(1, self.sub_steps_per_step))

            await asyncio.sleep(self.post_step_wait_sec)

            # Execute optional incremental step callback (e.g. card parsing)
            if on_step_callback and not self._stop_requested:
                try:
                    await on_step_callback()
                except Exception as e:
                    logger.warning(f"Error executing scroll step callback: {e}")

            # Evaluate scroll metrics
            scroll_metrics = await page.evaluate(
                """() => {
                    return {
                        scrollY: Math.round(window.scrollY),
                        innerHeight: Math.round(window.innerHeight),
                        scrollHeight: Math.round(document.documentElement.scrollHeight || document.body.scrollHeight)
                    };
                }"""
            )

            scroll_y = scroll_metrics.get("scrollY", 0)
            doc_height = scroll_metrics.get("scrollHeight", 0)
            inner_height = scroll_metrics.get("innerHeight", 0)

            # Check if reached bottom or position / document height stopped changing
            is_at_bottom = (scroll_y + inner_height) >= (doc_height - 15)
            is_position_stuck = (scroll_y == last_scroll_y) and (doc_height == last_doc_height)

            if is_at_bottom or is_position_stuck:
                consecutive_stable_cycles += 1
            else:
                consecutive_stable_cycles = 0

            last_scroll_y = scroll_y
            last_doc_height = doc_height

            if consecutive_stable_cycles >= self.stable_height_cycles_threshold:
                logger.info(
                    f"Page scroll completed: reached stable height threshold "
                    f"({consecutive_stable_cycles}/{self.stable_height_cycles_threshold} cycles, scrollY={scroll_y}, height={doc_height})"
                )
                break
