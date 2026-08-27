import asyncio
import os
import logging
import re
import weakref
from typing import AsyncGenerator
from contextlib import asynccontextmanager
from playwright.async_api import Browser, BrowserContext, Page

from src.browser.session import SESSION
from src.config import BrowserConfig

logger = logging.getLogger(__name__)


# Requests worth counting against the limit: the page itself and its data
# calls. Images, styles and fonts are not what makes hh.ru show a captcha.
THROTTLED_RESOURCE_TYPES = {"document", "xhr", "fetch"}
HH_URL_RE = re.compile(r"https?://([a-z0-9-]+\.)*(hh\.ru|hh\.kz|hh\.uz|rabota\.by)/", re.I)

# Tabs currently in use by the application. Lanes run in parallel, so a
# connector must never mistake another lane's working tab for a leftover and
# close it. Weak references let abandoned pages disappear on their own.
LIVE_PAGES: "weakref.WeakSet[Page]" = weakref.WeakSet()


class BrowserConnector:
    """
    Opens a tab in the shared browser connection (see `session.py`) and closes
    it on exit, leaving other lanes' tabs alone.
    """

    def __init__(self, config: BrowserConfig, limiter=None):
        self.config = config
        # Optional RateLimiter: when set, hh.ru requests made by the page are
        # paced through it as well, not just the explicit navigations.
        self.limiter = limiter
        self._holds_session = False
        self._browser: Browser | None = None
        self._created_page: Page | None = None

    async def _close_stale_tabs(self, context: BrowserContext) -> None:
        """Close tabs left over from previous runs.

        A cancelled job can leave its tab open; those tabs keep loading hh.ru in
        the background and cost us both CPU and requests against the rate limit.
        """
        if not self.config.close_stale_tabs:
            return

        closed = 0
        for page in list(context.pages):
            if page is self._created_page or page.is_closed():
                continue
            if page in LIVE_PAGES:
                logger.debug("Keeping a tab that another lane is working in.")
                continue
            url = page.url
            try:
                await page.close()
                closed += 1
                logger.info(f"Closed stale tab: {url[:100]}")
            except Exception as e:
                logger.warning(f"Could not close stale tab {url[:80]}: {e}")

        if closed:
            logger.info(f"Cleaned up {closed} stale tab(s) before starting.")

    async def _install_throttle(self, page: Page) -> None:
        limiter = self.limiter
        if not limiter or not limiter.config.enabled or not limiter.config.throttle_browser_requests:
            return

        async def handler(route, request) -> None:  # noqa: ANN001
            try:
                if request.resource_type in THROTTLED_RESOURCE_TYPES and HH_URL_RE.match(request.url):
                    # No spacing here: the interval applies to page openings,
                    # while background calls only spend the per-minute budget.
                    await limiter.acquire(spacing=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - never break page loading
                logger.warning(f"Rate limiter skipped for {request.url[:80]}: {e}")
            try:
                await route.continue_()
            except Exception:  # noqa: BLE001 - request already handled or page gone
                pass

        await page.route(HH_URL_RE, handler)
        logger.info(
            f"Request throttling enabled: {limiter.config.requests_per_minute} req/min to hh.ru"
        )

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[Page, None]:
        # The connection itself is shared process-wide: lanes work in the same
        # browser context, which is what makes their tabs distinguishable.
        try:
            self._browser, context = await SESSION.acquire(self.config)
            self._holds_session = True

            # The new tab is opened first: closing every page would make Chrome
            # itself exit, and the browser must stay alive between jobs.
            self._created_page = await context.new_page()
            LIVE_PAGES.add(self._created_page)
            await self._close_stale_tabs(context)
            await self._install_throttle(self._created_page)
            yield self._created_page

        except Exception as e:
            logger.error(f"Error during browser connection: {e}")
            raise
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        # CancelledError is caught alongside Exception on purpose: when a task is
        # cancelled (the "Стоп" button), the first await here would otherwise
        # abort cleanup and leave the tab open.
        if self._created_page:
            LIVE_PAGES.discard(self._created_page)
            try:
                if not self._created_page.is_closed():
                    logger.info("Closing created page tab...")
                    await self._created_page.close()
            except (Exception, asyncio.CancelledError) as e:
                logger.warning(f"Error while closing page tab: {e}")
            self._created_page = None

        if self._holds_session:
            self._holds_session = False
            try:
                await SESSION.release()
            except (Exception, asyncio.CancelledError) as e:
                logger.warning(f"Error while releasing the browser session: {e}")

        self._browser = None
