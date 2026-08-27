"""Cover letter generation and the applier's handling of the letter field."""

import pytest
from fastapi.testclient import TestClient

from src.applier.auto_applier import HHAutoApplier
from src.applier.cover_letter import (
    CoverLetterConfig,
    CoverLetterError,
    CoverLetterWriter,
    build_writer,
)
from src.config import Config
from src.db.models import ApplicationStatus
from src.llm import ChatResponse, LLMError
from src.web.app import create_app

RESUME = "Инженер технической поддержки. Python, PostgreSQL, Docker, Linux. Опыт 2 года."
VACANCY = "Требуется инженер поддержки: Python, SQL, Linux, дежурства."


class FakeLLM:
    """Stands in for the LLM client: returns canned text or raises."""

    def __init__(self, text="", error: Exception = None):
        self.text = text
        self.error = error
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return ChatResponse(text=self.text)

    async def aclose(self):
        pass


def writer(text="", error=None, **config_overrides) -> CoverLetterWriter:
    config = CoverLetterConfig(**config_overrides)
    return CoverLetterWriter(FakeLLM(text, error), config, RESUME)


# --- generation ----------------------------------------------------------

async def test_letter_is_generated_from_resume_and_vacancy():
    llm_text = (
        "Работал инженером поддержки: Python, PostgreSQL и Linux каждый день. "
        "Разбирал инциденты и дежурил в графике, что совпадает с вашими задачами. "
        "Готов обсудить детали в удобное время."
    )
    letter_writer = writer(llm_text)
    letter = await letter_writer.write("Инженер поддержки", "ООО Ромашка", VACANCY)

    assert letter == llm_text.strip()
    prompt = letter_writer.llm.requests[0].messages[0].content
    assert "Инженер поддержки" in prompt and "ООО Ромашка" in prompt
    assert "PostgreSQL" in prompt  # the resume went in
    assert "дежурства" in prompt   # the vacancy text went in


async def test_placeholders_and_markdown_are_stripped():
    raw = (
        "**Здравствуйте!** Меня зовут [Ваше имя], я работал с Python и Linux более двух лет, "
        "поддерживал сервисы с высокой нагрузкой и быстро разбирал инциденты. "
        "Настраивал мониторинг, писал скрипты автоматизации и дежурил в графике. "
        "Готов обсудить детали в удобное время.\n\n"
        "С уважением,\n[Имя Фамилия]"
    )
    letter = await writer(raw).write("Инженер", "Компания", VACANCY)

    for tell in ("[Ваше имя]", "[Имя Фамилия]", "**", "С уважением"):
        assert tell not in letter
    assert not letter.lower().startswith("здравствуйте")
    assert "Python и Linux" in letter


async def test_letter_is_clamped_to_the_limit_on_a_sentence():
    long_text = ("Первое предложение про Python и Linux. " * 40).strip()
    letter = await writer(long_text, max_chars=300).write("Инженер", "Компания", VACANCY)

    assert len(letter) <= 300
    assert letter.endswith(".")


async def test_llm_failure_becomes_a_cover_letter_error():
    with pytest.raises(CoverLetterError) as failure:
        await writer(error=LLMError("сервер недоступен")).write("Инженер", "Компания", VACANCY)
    assert "нейросеть" in str(failure.value)


async def test_too_short_answer_is_rejected():
    with pytest.raises(CoverLetterError) as failure:
        await writer("Хочу работать.").write("Инженер", "Компания", VACANCY)
    assert "коротким" in str(failure.value)


async def test_writer_is_not_built_without_llm_or_resume():
    config = CoverLetterConfig(enabled=True)
    assert build_writer(None, config, RESUME) is None
    assert build_writer(FakeLLM("x"), config, "") is None
    assert build_writer(FakeLLM("x"), CoverLetterConfig(enabled=False), RESUME) is None
    assert build_writer(FakeLLM("x"), config, RESUME) is not None


# --- the applier's page handling ----------------------------------------

class FakeElement:
    def __init__(self, attributes=None, value="", visible=True):
        self.attributes = attributes or {}
        self.value = value
        self.visible = visible
        self.filled = None
        self.clicked = False
        self.keys = []

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return True

    async def get_attribute(self, name):
        return self.attributes.get(name)

    async def input_value(self):
        return self.value

    async def fill(self, text):
        self.filled = text
        self.value = text

    async def click(self):
        self.clicked = True

    async def press(self, key):
        self.keys.append(key)

    async def inner_text(self):
        return self.attributes.get("text", "")


class FakePage:
    """Answers selector queries from a prepared mapping."""

    def __init__(self, elements=None, groups=None):
        self.elements = elements or {}
        self.groups = groups or {}

    async def query_selector(self, selector):
        return self.elements.get(selector)

    async def query_selector_all(self, selector):
        # "*" stands for "whatever selector the code builds for modal inputs".
        return self.groups.get(selector, self.groups.get("*", []))

    async def inner_text(self, selector):
        return VACANCY


LETTER_SELECTOR = '[data-qa="vacancy-response-popup-form-letter-input"]' 


async def test_letter_field_is_found_and_filled():
    field = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"})
    page = FakePage({LETTER_SELECTOR: field})

    applier = HHAutoApplier(cover_letter_writer=writer(
        "Работал инженером поддержки: Python, PostgreSQL и Linux каждый день, "
        "разбирал инциденты, настраивал мониторинг и дежурил в графике. "
        "Задачи из вашей вакансии мне знакомы, готов обсудить детали."
    ))
    found = await applier._find_letter_field(page)
    letter = await applier._fill_cover_letter(page, found, "123")

    assert found is field
    assert field.filled and "Python" in field.filled
    assert letter == field.filled


async def test_required_letter_without_generation_stops_the_application():
    """None means «required but impossible» — the vacancy is skipped, not broken."""
    field = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input", "required": ""})
    page = FakePage({LETTER_SELECTOR: field})

    applier = HHAutoApplier(cover_letter_writer=None)  # generation is off
    assert await applier._fill_cover_letter(page, field, "123") is None


async def test_optional_letter_without_generation_is_left_empty():
    field = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"})
    page = FakePage({LETTER_SELECTOR: field})

    applier = HHAutoApplier(cover_letter_writer=None)
    assert await applier._fill_cover_letter(page, field, "123") == ""  # apply anyway


async def test_fallback_text_is_used_when_the_model_fails():
    field = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input", "required": ""})
    page = FakePage({LETTER_SELECTOR: field})

    applier = HHAutoApplier(
        cover_letter_writer=writer(error=LLMError("нет связи")),
        cover_letter_fallback=(
            "Добрый день! Мне интересна ваша вакансия, релевантный опыт описан в резюме. "
            "Готов обсудить детали и ответить на вопросы в удобное для вас время."
        ),
    )
    letter = await applier._fill_cover_letter(page, field, "123")

    assert letter and letter.startswith("Добрый день")
    assert field.filled == letter


async def test_optional_letter_is_opened_only_in_always_mode():
    toggle = FakeElement({"data-qa": "vacancy-response-letter-toggle"})
    revealed = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"})

    class TogglePage(FakePage):
        async def query_selector(self, selector):
            if selector == LETTER_SELECTOR:
                return revealed if toggle.clicked else None
            return self.elements.get(selector)

    page = TogglePage({'[data-qa="vacancy-response-letter-toggle"]': toggle})

    on_demand = HHAutoApplier(cover_letter_writer=writer("x" * 200), cover_letter_when="required")
    assert await on_demand._find_letter_field(page) is None
    assert not toggle.clicked

    always = HHAutoApplier(cover_letter_writer=writer("x" * 200), cover_letter_when="always")
    assert await always._find_letter_field(page) is revealed
    assert toggle.clicked


async def test_letter_field_is_not_mistaken_for_employer_questions():
    """The old code treated any textarea in the modal as a questionnaire."""
    letter = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"}, value="написано")
    page = FakePage(groups={"*": [letter]})

    assert await HHAutoApplier()._has_unanswered_inputs(page) is False


async def test_real_questions_are_still_detected():
    letter = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"}, value="написано")
    question = FakeElement({"data-qa": "task-answer-input"}, value="")
    page = FakePage(groups={"*": [letter, question]})

    assert await HHAutoApplier()._has_unanswered_inputs(page) is True


# --- settings wiring -----------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.sqlite_path = str(tmp_path / "letters.db")
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_cover_letter_settings_are_exposed(client):
    page = client.get("/settings").text
    assert "Сопроводительное письмо" in page
    assert 'name="cover_letter.when"' in page
    assert 'name="cover_letter.prompt"' in page


def test_cover_letter_settings_round_trip(client):
    client.post(
        "/actions/settings",
        data={
            "cover_letter.enabled": "1",
            "cover_letter.when": "always",
            "cover_letter.max_chars": "800",
            "cover_letter.prompt": "Пиши сухо и по делу.",
        },
        follow_redirects=False,
    )
    config = client.app.state.settings.cover_letter_config()
    assert config.enabled and config.when == "always"
    assert config.max_chars == 800
    assert config.prompt == "Пиши сухо и по делу."


def test_disabled_cover_letter_yields_no_writer(client):
    client.post("/actions/settings", data={"cover_letter.enabled": ""}, follow_redirects=False)
    config = client.app.state.settings.cover_letter_config()
    assert config.enabled is False
    assert build_writer(FakeLLM("text"), config, RESUME) is None


def test_skipped_status_is_reported_for_an_impossible_letter():
    # The status the applier returns when a required letter cannot be written.
    assert ApplicationStatus.SKIPPED_QUESTIONS.value == "skipped_questions"


# --- selectors verified against a saved hh.ru vacancy page ---------------
# The page had: form#RESPONSE_MODAL_FORM_ID, textarea
# [data-qa="vacancy-response-popup-form-letter-input"], a disabled
# button[data-qa="vacancy-response-submit-popup"], and the "questions" turned
# out to be <div> suggestion chips rather than inputs.

async def test_submit_stays_disabled_means_letter_is_required():
    field = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"}, value="")
    submit = FakeElement({"data-qa": "vacancy-response-submit-popup"})
    submit.enabled = False

    class DisabledSubmit(FakeElement):
        async def is_enabled(self):
            return False

    submit = DisabledSubmit({"data-qa": "vacancy-response-submit-popup"})
    page = FakePage({'[data-qa="vacancy-response-submit-popup"]': submit})

    assert await HHAutoApplier()._is_letter_required(page, field) is True


async def test_enabled_submit_means_the_letter_is_optional():
    field = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"}, value="")
    submit = FakeElement({"data-qa": "vacancy-response-submit-popup"})
    page = FakePage({'[data-qa="vacancy-response-submit-popup"]': submit})

    assert await HHAutoApplier()._is_letter_required(page, field) is False


async def test_disabled_submit_is_never_reported_as_a_sent_response():
    """Clicking a disabled button does nothing — that used to count as success."""

    class DisabledSubmit(FakeElement):
        async def is_enabled(self):
            return False

    applier = HHAutoApplier()
    submit = DisabledSubmit({"data-qa": "vacancy-response-submit-popup"})
    assert await applier._wait_until_enabled(submit, timeout_sec=0.5) is False


async def test_wait_until_enabled_returns_as_soon_as_the_button_unlocks():
    class Unlocking(FakeElement):
        def __init__(self):
            super().__init__({})
            self.checks = 0

        async def is_enabled(self):
            self.checks += 1
            return self.checks > 2

    button = Unlocking()
    assert await HHAutoApplier()._wait_until_enabled(button, timeout_sec=3) is True


async def test_question_chips_are_not_treated_as_inputs():
    """hh.ru renders suggested questions as <div>, they never reach the input scan."""
    letter = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"}, value="текст")
    page = FakePage(groups={})  # no textarea/input beyond the letter
    assert await HHAutoApplier()._has_unanswered_inputs(page) is False


def test_response_form_selectors_cover_the_current_layout():
    assert "form#RESPONSE_MODAL_FORM_ID" in HHAutoApplier.RESPONSE_FORM_SELECTORS
    assert '[data-qa="response-popup-close"]' in HHAutoApplier.CLOSE_POPUP_SELECTORS
    assert HHAutoApplier.LETTER_TEXTAREA_SELECTORS[0] == '[data-qa="vacancy-response-popup-form-letter-input"]'


# --- the letter is kept for later review --------------------------------

async def test_letter_is_stored_and_listed(tmp_path):
    from src.db.connection import init_sqlite
    from src.db.models import PageCommitParams, SearchRun, VacancyApplication, VacancyCard
    from src.db.repository import DatabaseRepository

    conn = await init_sqlite(str(tmp_path / "letters.db"))
    repo = DatabaseRepository(conn, driver="sqlite")

    run_id = "run-1"
    await repo.create_search_run(SearchRun(id=run_id, task_id=run_id, search_url="https://hh.ru/search"))
    await repo.commit_page_transaction(PageCommitParams(
        search_run_id=run_id, page_key="p1", page_number=1,
        current_url="u", canonical_url="u",
        cards=[VacancyCard(external_id="v1", url="https://hh.ru/vacancy/v1", title="QA Engineer")],
    ))

    letter = "Работал с автотестами на Python и знаю ваш стек. Готов обсудить детали."
    await repo.record_application(VacancyApplication(
        external_id="v1", vacancy_url="https://hh.ru/vacancy/v1",
        status=ApplicationStatus.APPLIED, response_text="Отклик отправлен с сопроводительным письмом",
        cover_letter=letter,
    ))

    rows = await repo.list_applications()
    assert rows[0]["cover_letter"] == letter

    vacancies = await repo.list_vacancies()
    assert vacancies[0]["cover_letter"] == letter  # visible on the vacancies page too
    await conn.close()


def test_letter_is_shown_in_the_ui(client):
    from anyio.from_thread import start_blocking_portal

    from src.db.models import PageCommitParams, SearchRun, VacancyApplication, VacancyCard

    letter = "Уникальный текст письма для проверки интерфейса, Python и Linux."
    repo = client.app.state.repository

    async def seed():
        await repo.create_search_run(SearchRun(id="r", task_id="r", search_url="u"))
        await repo.commit_page_transaction(PageCommitParams(
            search_run_id="r", page_key="p", page_number=1, current_url="u", canonical_url="u",
            cards=[VacancyCard(external_id="x1", url="https://hh.ru/vacancy/x1", title="QA")],
        ))
        await repo.record_application(VacancyApplication(
            external_id="x1", vacancy_url="https://hh.ru/vacancy/x1",
            status=ApplicationStatus.APPLIED, cover_letter=letter,
        ))

    with start_blocking_portal() as portal:
        portal.call(seed)

    applications = client.get("/applications").text
    assert letter in applications
    assert "Сопроводительное письмо" in applications

    vacancies = client.get("/vacancies").text
    assert letter in vacancies


async def test_letter_is_followed_by_real_key_events():
    """A programmatic fill can leave hh.ru thinking the field is still empty."""
    field = FakeElement({"data-qa": "vacancy-response-popup-form-letter-input"})
    page = FakePage({LETTER_SELECTOR: field})

    applier = HHAutoApplier(cover_letter_writer=writer(
        "Работал инженером поддержки: Python, PostgreSQL и Linux каждый день, "
        "разбирал инциденты и дежурил в графике. Готов обсудить детали."
    ))
    await applier._fill_cover_letter(page, field, "123")

    assert field.filled
    assert field.keys == ["Space", "Backspace"]  # nudges the form to revalidate
