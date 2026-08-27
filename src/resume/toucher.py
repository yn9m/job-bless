"""Refreshing an hh.ru resume so it rises in search without changing content.

Preferred path is the native «Поднять в поиске» button. hh.ru only enables it
once the cooldown has passed; when it is unavailable there is the well known
trick of re-saving the resume with a trailing space, which bumps the update
date while leaving the text identical. That path edits the resume, so it is
off by default.
"""

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from playwright.async_api import Page

logger = logging.getLogger(__name__)

RAISE_BUTTON_SELECTORS = (
    '[data-qa="resume-update-button"]',
    '[data-qa="resume-update-button_updateResume"]',
    'button[data-qa*="resume-update"]',
    'text=Поднять в поиске',
    'text=Обновить дату',
)

# Line hh.ru shows next to the button, e.g. «Последнее — 21 июля в 14:03».
LAST_RAISE_SELECTORS = (
    '[data-qa="resume-update-date"]',
    '[data-qa="resume-updated-date"]',
)

ABOUT_EDIT_SELECTORS = (
    '[data-qa="resume-block-skills"] [data-qa="resume-block-edit"]',
    '[data-qa="resume-block-skills-edit"]',
    '[data-qa="resume-block-about-edit"]',
)
ABOUT_TEXTAREA_SELECTORS = (
    'textarea[data-qa="resume-textarea-skills"]',
    '[data-qa="resume-block-skills"] textarea',
    "form textarea",
)
SAVE_BUTTON_SELECTORS = (
    '[data-qa="resume-submit"]',
    'button[data-qa="resume-block-save"]',
    'form button[type="submit"]',
)


class ResumeToucher:
    def __init__(self, allow_edit_fallback: bool = False):
        self.allow_edit_fallback = allow_edit_fallback

    async def touch(
        self,
        page: Page,
        resume_url: str,
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        say = log or logger.info
        say(f"открываю резюме {resume_url}")
        await page.goto(resume_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.0)

        before = await self._last_raise_text(page)

        button = await self._find_enabled(page, RAISE_BUTTON_SELECTORS)
        if button:
            say("нажимаю «Поднять в поиске»")
            await button.click()
            await asyncio.sleep(2.0)
            after = await self._last_raise_text(page)
            updated = after != before or not await self._find_enabled(page, RAISE_BUTTON_SELECTORS)
            if updated:
                say("резюме поднято в поиске")
            return {"method": "raise_button", "updated": updated, "before": before, "after": after}

        say("кнопка поднятия недоступна (не прошёл интервал hh.ru)")
        if not self.allow_edit_fallback:
            return {"method": "none", "updated": False, "before": before}

        return await self._touch_by_editing(page, say, before)

    async def _touch_by_editing(self, page: Page, say: Callable[[str], None], before: str) -> Dict[str, Any]:
        """Re-save «О себе» with a trailing space toggled, leaving the text intact."""
        say("пробую обновить дату пересохранением блока «О себе»")

        edit = await self._find_enabled(page, ABOUT_EDIT_SELECTORS)
        if not edit:
            say("не нашёл кнопку редактирования блока «О себе»")
            return {"method": "edit", "updated": False, "error": "edit button not found"}

        await edit.click()
        await asyncio.sleep(1.5)

        textarea = await self._find_enabled(page, ABOUT_TEXTAREA_SELECTORS)
        if not textarea:
            say("не нашёл поле «О себе» — ничего не меняю")
            return {"method": "edit", "updated": False, "error": "textarea not found"}

        current = await textarea.input_value()
        # Toggle a single trailing space: the meaning never changes, and the
        # value always differs from what hh.ru has stored.
        updated_text = current.rstrip() if current.endswith(" ") else current + " "
        await textarea.fill(updated_text)

        save = await self._find_enabled(page, SAVE_BUTTON_SELECTORS)
        if not save:
            say("не нашёл кнопку сохранения — изменения не отправлены")
            return {"method": "edit", "updated": False, "error": "save button not found"}

        await save.click()
        await asyncio.sleep(2.5)
        after = await self._last_raise_text(page)
        say("резюме пересохранено")
        return {"method": "edit", "updated": True, "before": before, "after": after}

    async def _find_enabled(self, page: Page, selectors) -> Any:  # noqa: ANN001
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible() and await element.is_enabled():
                    return element
            except Exception:  # noqa: BLE001 - selector may be invalid on this page
                continue
        return None

    async def _last_raise_text(self, page: Page) -> str:
        for selector in LAST_RAISE_SELECTORS:
            try:
                element = await page.query_selector(selector)
                if element:
                    return (await element.inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
        return ""
