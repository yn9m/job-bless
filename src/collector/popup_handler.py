import logging
from playwright.async_api import Page

logger = logging.getLogger(__name__)


class PopupHandler:
    """
    Handles known overlays, modal popups, and cookies banners on HH.ru.
    """

    KNOWN_POPUP_CLOSE_SELECTORS = [
        '[data-qa="cookies-policy-informer-accept"]',
        '[data-qa="bloko-modal-close"]',
        '.bloko-modal-close-button',
    ]

    def setup_dialog_handler(self, page: Page) -> None:
        page.on("dialog", lambda dialog: logger.info(f"Browser dialog opened: '{dialog.message}'. Dismissing..."))

    async def dismiss_known_overlays(self, page: Page) -> None:
        for sel in self.KNOWN_POPUP_CLOSE_SELECTORS:
            try:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible():
                    logger.info(f"Dismissing overlay using selector: '{sel}'")
                    await elem.click()
            except Exception:
                pass
