"""Follow-up messages are tied to the vacancy that was just applied to."""

from unittest.mock import AsyncMock

import pytest

from src.applier.auto_applier import HHAutoApplier
from src.applier.chat import ChatResult, DEFAULT_FOLLOW_UP, HHChat
from src.db.connection import init_sqlite
from src.db.models import ApplicationStatus, VacancyApplication
from src.db.repository import DatabaseRepository


class Element:
    def __init__(self, *, href=None, editable=False):
        self.href = href
        self.editable = editable
        self.value = ""
        self.clicked = False
        self.on_click = None

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def get_attribute(self, name):
        if name == "href":
            return self.href
        if name == "contenteditable":
            return "true" if self.editable else None
        return None

    async def fill(self, value):
        self.value = value

    async def type(self, value, delay=0):
        self.value += value

    async def input_value(self):
        return self.value

    async def inner_text(self):
        return self.value

    async def click(self):
        self.clicked = True
        if self.on_click:
            self.on_click()


class ChatPage:
    def __init__(self, href="/applicant/negotiations/123"):
        self.topic = Element(href=href)
        self.composer = Element()
        self.send_button = Element()
        self.sent_text = ""
        self.send_button.on_click = self._send
        self.url = ""
        self.frames = []

    def _send(self):
        self.sent_text = self.composer.value
        self.composer.value = ""

    async def query_selector(self, selector):
        if selector == HHChat.TOPIC_SELECTORS[0] and not self.url:
            return self.topic
        if self.url and selector == HHChat.COMPOSER_SELECTORS[0]:
            return self.composer
        if self.url and self.composer.value and selector == HHChat.SEND_SELECTORS[0]:
            return self.send_button
        return None

    async def goto(self, url, **kwargs):
        self.url = url

    async def query_selector_all(self, selector):
        return []

    def get_by_text(self, text, exact=False):
        assert exact
        return TextLocator(self, text)


class TextLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text
        self.first = self

    async def count(self):
        return int(self.page.sent_text == self.text)

    async def is_visible(self):
        return self.page.sent_text == self.text


class ChatFrame:
    def __init__(self):
        self.url = "https://chatik.hh.ru/chat/123?dest=iframe"
        self.composer = Element()
        self.send_button = Element()
        self.sent_text = ""
        self.send_button.on_click = self._send

    def _send(self):
        self.sent_text = self.composer.value
        self.composer.value = ""

    async def query_selector(self, selector):
        if selector == HHChat.CHATIK_COMPOSER_SELECTORS[0]:
            return self.composer
        if selector == HHChat.CHATIK_SEND_SELECTORS[0] and self.composer.value:
            return self.send_button
        return None

    def get_by_text(self, text, exact=False):
        assert exact
        return TextLocator(self, text)


@pytest.mark.asyncio
async def test_chat_uses_current_vacancys_topic_and_confirms_send():
    page = ChatPage()
    result = await HHChat().send_after_apply(page, "https://hh.ru/vacancy/42")

    assert result == ChatResult("sent", message=DEFAULT_FOLLOW_UP)
    assert page.url == "https://hh.ru/applicant/negotiations/123"
    assert page.send_button.clicked


@pytest.mark.asyncio
async def test_chat_sends_through_chatik_iframe():
    page = ChatPage(href=None)
    frame = ChatFrame()
    page.topic.on_click = lambda: page.frames.append(frame)

    result = await HHChat().send_after_apply(page, "https://hh.ru/vacancy/42")

    assert result == ChatResult("sent", message=DEFAULT_FOLLOW_UP)
    assert frame.send_button.clicked
    assert frame.sent_text == DEFAULT_FOLLOW_UP


@pytest.mark.asyncio
async def test_existing_chat_draft_is_left_untouched():
    page = ChatPage(href=None)
    frame = ChatFrame()
    frame.composer.value = "Мой черновик"
    page.topic.on_click = lambda: page.frames.append(frame)

    result = await HHChat().send_after_apply(page, "https://hh.ru/vacancy/42")

    assert result.status == "unavailable"
    assert frame.composer.value == "Мой черновик"
    assert not frame.send_button.clicked


@pytest.mark.asyncio
async def test_chat_does_not_open_an_unrelated_or_unsafe_link():
    page = ChatPage("https://elsewhere.example/negotiations/123")
    result = await HHChat().send_after_apply(page, "https://hh.ru/vacancy/42")

    assert result.status == "failed"
    assert page.url == ""
    assert not page.send_button.clicked


@pytest.mark.asyncio
async def test_chat_accepts_regional_hh_domain():
    page = ChatPage("/applicant/negotiations/123")
    result = await HHChat().send_after_apply(page, "https://spb.hh.ru/vacancy/42")

    assert result.status == "sent"
    assert page.url == "https://spb.hh.ru/applicant/negotiations/123"


@pytest.mark.asyncio
async def test_chat_finds_unique_negotiation_link_when_qa_changes():
    class NewLayout(ChatPage):
        async def query_selector(self, selector):
            if not self.url and selector in HHChat.TOPIC_SELECTORS:
                return None
            return await super().query_selector(selector)

        async def query_selector_all(self, selector):
            return [self.topic]

    page = NewLayout()
    result = await HHChat().send_after_apply(page, "https://hh.ru/vacancy/42")

    assert result.status == "sent"


@pytest.mark.asyncio
async def test_chat_absence_is_recorded_without_sending():
    page = ChatPage()
    page.query_selector = AsyncMock(return_value=None)
    result = await HHChat().send_after_apply(page, "https://hh.ru/vacancy/42")

    assert result.status == "unavailable"
    assert not page.send_button.clicked


@pytest.mark.asyncio
async def test_unconfirmed_send_is_not_retried(monkeypatch):
    monkeypatch.setattr("src.applier.chat.asyncio.sleep", AsyncMock())
    page = ChatPage()
    page.send_button.on_click = None  # the site ignored the click

    result = await HHChat().send_after_apply(page, "https://hh.ru/vacancy/42")

    assert result.status == "unknown"
    assert page.send_button.clicked
    assert page.composer.value == DEFAULT_FOLLOW_UP


@pytest.mark.asyncio
async def test_successful_application_keeps_its_status_if_chat_is_unavailable(monkeypatch):
    monkeypatch.setattr("src.applier.auto_applier.asyncio.sleep", AsyncMock())
    applied = False
    apply_button = Element()

    def mark_applied():
        nonlocal applied
        applied = True

    apply_button.on_click = mark_applied
    applied_marker = Element()
    page = AsyncMock()

    async def find(selector):
        if selector in HHAutoApplier.ALREADY_APPLIED_SELECTORS:
            return applied_marker if applied else None
        if selector == HHAutoApplier.APPLY_BUTTON_SELECTORS[0]:
            return apply_button
        return None

    page.query_selector.side_effect = find
    page.query_selector_all.return_value = []
    chat = AsyncMock()
    chat.send_after_apply.return_value = ChatResult("unavailable", error="Чат выключен")

    result = await HHAutoApplier(chat=chat).apply_to_vacancy(
        page, "https://hh.ru/vacancy/42", "42"
    )

    assert result.status == ApplicationStatus.APPLIED
    assert result.chat_status == "unavailable"
    assert result.chat_error == "Чат выключен"
    chat.send_after_apply.assert_awaited_once_with(page, "https://hh.ru/vacancy/42")


@pytest.mark.asyncio
async def test_chat_result_is_persisted(tmp_path):
    conn = await init_sqlite(str(tmp_path / "chat.db"))
    repo = DatabaseRepository(conn, driver="sqlite")
    await repo.record_application(VacancyApplication(
        external_id="42", vacancy_url="https://hh.ru/vacancy/42",
        status=ApplicationStatus.APPLIED, chat_message=DEFAULT_FOLLOW_UP,
        chat_status="sent",
    ))

    rows = await repo.list_applications()
    assert rows[0]["chat_message"] == DEFAULT_FOLLOW_UP
    assert rows[0]["chat_status"] == "sent"
    await conn.close()
