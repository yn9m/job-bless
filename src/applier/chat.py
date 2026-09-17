"""Send a follow-up in the hh.ru conversation opened by a vacancy response.

The browser is already authenticated by BrowserConnector.  We deliberately
open the conversation from the just-applied vacancy, never from the global
inbox, so a changed hh.ru layout cannot send the text to another employer.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from playwright.async_api import Frame, Page

logger = logging.getLogger(__name__)

DEFAULT_FOLLOW_UP = (
    "Здравствуйте! Меня заинтересовала ваша вакансия. "
    "Хотелось бы обсудить детали и продолжить общение."
)


@dataclass(frozen=True)
class ChatResult:
    status: str  # sent, unavailable, unknown, failed
    message: str = ""
    error: str = ""


class HHChat:
    """One-shot follow-up to the conversation for the current vacancy."""

    TOPIC_SELECTORS = (
        '[data-qa="vacancy-response-link-view-topic"]',
        '[data-qa="serp-item__vacancy-response-view-topic"]',
        '[data-qa="vacancy-chat-link"]',
        '[data-qa="vacancy-chat-button"]',
        '[data-qa="vacancy-response-link-chat"]',
    )
    CHAT_LINK_SELECTORS = (
        '[data-qa="negotiation-chat-link"]',
        '[data-qa="response-chat-link"]',
    )
    CHATIK_COMPOSER_SELECTORS = ('textarea[data-qa="text-input"]',)
    CHATIK_SEND_SELECTORS = ('button[data-qa="chatik-do-send-message"]',)
    COMPOSER_SELECTORS = (
        'textarea[data-qa*="chat"]',
        '[contenteditable="true"][data-qa*="chat"]',
        '[data-qa*="chat"] textarea',
        '[data-qa*="chat"] [contenteditable="true"]',
        'textarea[data-qa*="message"]',
        '[contenteditable="true"][data-qa*="message"]',
    )
    SEND_SELECTORS = (
        'button[data-qa*="chat-send"]',
        'button[data-qa*="send-message"]',
        'button[data-qa*="message-send"]',
        'button[aria-label="Отправить сообщение"]',
    )

    def __init__(self, message: str = DEFAULT_FOLLOW_UP):
        self.message = message.strip()
        if not self.message:
            raise ValueError("Chat follow-up must not be empty")

    async def send_after_apply(self, page: Page, vacancy_url: str) -> ChatResult:
        """Send once; an uncertain result is never retried automatically."""
        try:
            topic = await self._find_topic(page, vacancy_url)
            if topic is None:
                return ChatResult("unavailable", error="Ссылка на переписку не появилась после отклика")

            if not await self._open_link(page, topic, vacancy_url):
                return ChatResult("failed", error="Небезопасная ссылка на переписку")

            chat_context, composer = await self._find_composer(page)
            if composer is None:
                if self._chat_frames(page):
                    return ChatResult("unavailable", error="Чат работодателя недоступен или выключен")
                chat_link = await self._first_visible(page, self.CHAT_LINK_SELECTORS)
                if chat_link is None:
                    chat_link = await self._named_chat_link(page)
                if chat_link is not None:
                    if not await self._open_link(page, chat_link, page.url):
                        return ChatResult("failed", error="Небезопасная ссылка на чат")
                    chat_context, composer = await self._find_composer(page)
            if composer is None:
                return ChatResult("unavailable", error="Чат работодателя недоступен или выключен")

            if (await self._composer_text(composer)).strip():
                return ChatResult("unavailable", error="В чате уже есть черновик; сообщение не отправлено")
            await composer.click()
            # Chatik ignores Playwright's fill() for its React state. Real key
            # events make the send button appear in the current hh.ru layout.
            await composer.type(self.message, delay=30)
            if (await self._composer_text(composer)).strip() != self.message:
                return ChatResult("failed", error="Текст сообщения не появился в поле чата")
            send_button = await self._find_send_button(chat_context)
            if send_button is None:
                await composer.fill("")
                return ChatResult("unavailable", error="Кнопка отправки в чате не найдена")

            if not await self._wait_enabled(send_button):
                await composer.fill("")
                return ChatResult("failed", error="Кнопка отправки сообщения заблокирована")
            await send_button.click()

            # A click is not proof of delivery. Wait for both the cleared editor
            # and the outgoing message in the conversation.
            for _ in range(20):
                if not (await self._composer_text(composer)).strip():
                    message = chat_context.get_by_text(self.message, exact=True)
                    if await message.count() and await message.first.is_visible():
                        logger.info("Follow-up chat message sent after response to %s", vacancy_url)
                        return ChatResult("sent", message=self.message)
                await asyncio.sleep(0.25)
            return ChatResult("unknown", error="Не удалось подтвердить отправку сообщения в чате")
        except Exception as exc:
            logger.warning("Could not send chat message after response to %s: %s", vacancy_url, exc)
            return ChatResult("failed", error=f"Ошибка чата: {exc}")

    async def _find_composer(self, page: Page):
        for _ in range(20):
            frames = self._chat_frames(page)
            if len(frames) == 1:
                composer = await self._first_visible(frames[0], self.CHATIK_COMPOSER_SELECTORS)
                if composer is not None:
                    return frames[0], composer
            elif len(frames) > 1:
                return None, None  # several conversations would be ambiguous
            composer = await self._first_visible(page, self.COMPOSER_SELECTORS)
            if composer is None:
                composer = await self._unique_role(
                    page, "textbox", re.compile(r"^(?:Напишите|Введите) сообщение$|^Сообщение$", re.I)
                )
            if composer is not None:
                return page, composer
            await asyncio.sleep(0.25)
        return None, None

    @staticmethod
    def _chat_frames(page: Page) -> list[Frame]:
        return [frame for frame in page.frames
                if urlparse(frame.url).hostname == "chatik.hh.ru"
                and urlparse(frame.url).path.startswith("/chat/")]

    async def _find_topic(self, page: Page, vacancy_url: str):
        topic = await self._first_visible(page, self.TOPIC_SELECTORS)
        if topic is not None:
            return topic
        # The exact data-qa changes between vacancy layouts.  A unique link
        # to an individual negotiation on this vacancy is still unambiguous.
        candidates = []
        for link in await page.query_selector_all('a[href*="/negotiations/"]'):
            href = await link.get_attribute("href")
            target = urljoin(vacancy_url, href or "")
            path = urlparse(target).path
            if (re.search(r"/negotiations/[^/]+/?$", path)
                    and self._safe_hh_url(target) and await link.is_visible()):
                candidates.append(link)
        return candidates[0] if len(candidates) == 1 else None

    async def _find_send_button(self, page: Page | Frame):
        for _ in range(12):
            selectors = (self.CHATIK_SEND_SELECTORS if urlparse(page.url).hostname == "chatik.hh.ru"
                         else self.SEND_SELECTORS)
            button = await self._first_visible(page, selectors)
            if button is None:
                button = await self._unique_role(page, "button", "Отправить сообщение")
            if button is None:
                button = await self._unique_role(page, "button", "Отправить")
            if button is not None:
                return button
            await asyncio.sleep(0.25)
        return None

    @staticmethod
    async def _wait_enabled(button) -> bool:
        for _ in range(12):
            if await button.is_enabled():
                return True
            await asyncio.sleep(0.25)
        return False

    @staticmethod
    async def _open_link(page: Page, element, base_url: str) -> bool:
        href = await element.get_attribute("href")
        if not href:
            await element.click()
            return True
        target = urljoin(base_url, href)
        if not HHChat._safe_hh_url(target):
            return False
        await page.goto(target, wait_until="domcontentloaded", timeout=20000)
        return True

    @staticmethod
    def _safe_hh_url(url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return parsed.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))

    @staticmethod
    async def _first_visible(page: Page, selectors):
        for selector in selectors:
            element = await page.query_selector(selector)
            if element is not None and await element.is_visible():
                return element
        return None

    @staticmethod
    async def _named_chat_link(page: Page):
        for name in ("Перейти в чат",):
            for role in ("link", "button"):
                locator = await HHChat._unique_role(page, role, name)
                if locator is not None:
                    return locator
        return None

    @staticmethod
    async def _unique_role(page: Page, role: str, name):
        locator = page.get_by_role(role, name=name, exact=isinstance(name, str))
        if await locator.count() == 1 and await locator.is_visible():
            return locator
        return None

    @staticmethod
    async def _composer_text(composer) -> str:
        if await composer.get_attribute("contenteditable") == "true":
            return await composer.inner_text()
        return await composer.input_value()
