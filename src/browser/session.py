"""One browser connection shared by every lane.

Each lane used to start its own Playwright driver and open its own connection
to the same Chrome. That meant two independent object graphs: a page created by
one lane was an unrelated object in the other, so the tab cleanup could not tell
«another lane is working here» from «leftover from a crashed run» — and closed
the tab out from under a running job.

One driver, one connection, one context: page objects are shared, tabs are
distinguishable, and there is a single node process instead of one per lane.
"""

import asyncio
import logging
import os
from typing import Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from src.config import BrowserConfig

logger = logging.getLogger(__name__)

STORAGE_STATE_FILE = "./data/storage_state.json"


class SharedBrowserSession:
    """Reference-counted connection to the browser.

    The connection is opened on the first `acquire()` and closed when the last
    user releases it, so short CLI runs still shut down cleanly.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._users = 0

    @property
    def is_connected(self) -> bool:
        return bool(self._browser and self._browser.is_connected())

    async def acquire(self, config: BrowserConfig) -> Tuple[Browser, BrowserContext]:
        async with self._lock:
            if not self.is_connected:
                await self._disconnect()  # drop a stale connection, if any
                await self._connect(config)
            self._users += 1
            logger.debug("Browser session acquired (users=%d).", self._users)
            return self._browser, self._context

    async def release(self) -> None:
        async with self._lock:
            self._users = max(0, self._users - 1)
            logger.debug("Browser session released (users=%d).", self._users)
            if self._users == 0:
                await self._disconnect()

    async def close(self) -> None:
        async with self._lock:
            self._users = 0
            await self._disconnect()

    # --- internals --------------------------------------------------------

    async def _connect(self, config: BrowserConfig) -> None:
        transport = config.transport.lower()
        self._playwright = await async_playwright().start()

        if transport == "playwright":
            pw_cfg = config.playwright
            browser_type = getattr(self._playwright, pw_cfg.browser_type.lower(), None)
            if not browser_type:
                raise RuntimeError(f"Playwright browser type '{pw_cfg.browser_type}' not found.")
            logger.info(f"Connecting to remote browser via Playwright WS: {pw_cfg.endpoint}")
            self._browser = await browser_type.connect(pw_cfg.endpoint, timeout=pw_cfg.timeout_ms)
        elif transport == "cdp":
            cdp_cfg = config.cdp
            logger.info(f"Connecting to remote browser via CDP: {cdp_cfg.endpoint}")
            self._browser = await self._playwright.chromium.connect_over_cdp(
                cdp_cfg.endpoint, timeout=cdp_cfg.timeout_ms
            )
        else:
            raise RuntimeError(f"Unsupported transport '{transport}'")

        self._context = await self._pick_context()

    async def _pick_context(self) -> BrowserContext:
        contexts = self._browser.contexts
        if contexts:
            logger.info("Using the existing BrowserContext (profile session).")
            return contexts[0]

        kwargs = {}
        if os.path.exists(STORAGE_STATE_FILE):
            logger.info(f"Detected saved session state file at '{STORAGE_STATE_FILE}'.")
            kwargs["storage_state"] = STORAGE_STATE_FILE
        logger.info("Creating a new BrowserContext.")
        return await self._browser.new_context(**kwargs)

    async def _disconnect(self) -> None:
        self._context = None
        if self._browser:
            try:
                if self._browser.is_connected():
                    await self._browser.close()
            except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001
                logger.warning(f"Error while closing the browser connection: {e}")
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001
                logger.warning(f"Error while stopping Playwright driver instance: {e}")
            self._playwright = None


# Process-wide: lanes must share it, that is the whole point.
SESSION = SharedBrowserSession()
