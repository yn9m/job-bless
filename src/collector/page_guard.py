import logging
from playwright.async_api import Page

logger = logging.getLogger(__name__)


class HHPageGuard:
    """
    Checks for captcha, access denied, or login overlays on HH.ru pages.
    """

    async def check_page_state(self, page: Page, is_navigation_step: bool = False) -> None:
        url = page.url.lower()
        if "captcha" in url or "check-captcha" in url:
            logger.warning(f"Captcha detected on URL: {page.url}")
        if "403" in url or "forbidden" in url:
            logger.warning(f"Access forbidden (403) detected on URL: {page.url}")
