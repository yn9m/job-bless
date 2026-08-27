import asyncio
import json
import logging
import re
from typing import Optional, Dict, Any, List
from playwright.async_api import Page

from src.applier.cover_letter import CoverLetterError, CoverLetterWriter
from src.llm.base import LLMClient
from src.db.models import VacancyApplication, ApplicationStatus

logger = logging.getLogger(__name__)


class HHAutoApplier:
    """
    Automates applying to HH.ru vacancies without employer questionnaires or tests.
    Skips vacancies that require answering custom questions or have low LLM match score.
    """

    DESCRIPTION_SELECTORS = [
        '[data-qa="vacancy-description"]',
        '.vacancy-description',
        '.g-user-content',
    ]

    APPLY_BUTTON_SELECTORS = [
        '[data-qa="vacancy-response-link-top"]',
        '[data-qa="vacancy-response-link-bottom"]',
        '[data-qa="serp-item__vacancy-response"]',
        'a[data-qa="vacancy-response-link-top"]',
        'button[data-qa="vacancy-response-button"]',
    ]

    ALREADY_APPLIED_SELECTORS = [
        '[data-qa="vacancy-response-link-view-topic"]',
        '[data-qa="serp-item__vacancy-response-view-topic"]',
    ]

    SUBMIT_POPUP_SELECTORS = [
        '[data-qa="vacancy-response-submit-popup"]',
        'button[data-qa="vacancy-response-submit-popup"]',
        '[data-qa="relocate-warning-confirm"]',
    ]

    QUESTION_CONTAINER_SELECTORS = [
        '[data-qa="vacancy-response-popup-questions"]',
        '.vacancy-response-popup-questions',
        '[data-qa="test-questions"]',
    ]

    CLOSE_POPUP_SELECTORS = [
        '[data-qa="response-popup-close"]',
        '[data-qa="bloko-modal-close"]',
        '.bloko-modal-close-button',
    ]

    # The response modal is a form; on the current (magritte) layout the letter
    # textarea is the only input inside it.
    RESPONSE_FORM_SELECTORS = [
        'form#RESPONSE_MODAL_FORM_ID',
        'form[name="vacancy_response"]',
        '.bloko-modal',  # legacy layout
    ]
    LETTER_TEXTAREA_SELECTORS = [
        '[data-qa="vacancy-response-popup-form-letter-input"]',
        'textarea[data-qa*="letter-input"]',
        'textarea[name="letter"]',
        'textarea[data-qa*="letter"]',
    ]
    LETTER_TOGGLE_SELECTORS = [
        '[data-qa="vacancy-response-letter-toggle"]',
        '[data-qa="vacancy-response-popup-form-letter-toggle"]',
        'button[data-qa*="letter-toggle"]',
    ]

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        resume_text: str = "",
        min_score: int = 7,
        cover_letter_writer: Optional["CoverLetterWriter"] = None,
        cover_letter_when: str = "required",
        cover_letter_fallback: str = "",
    ):
        self.llm_client = llm_client
        self.resume_text = resume_text
        self.min_score = min_score
        # When set, a vacancy asking for a cover letter is answered instead of skipped.
        self.cover_letter_writer = cover_letter_writer
        self.cover_letter_when = cover_letter_when
        self.cover_letter_fallback = cover_letter_fallback

    async def _evaluate_vacancy_match(self, page: Page, external_id: str) -> tuple[bool, int, str]:
        if not self.llm_client or not self.resume_text.strip():
            return True, 10, "LLM evaluation skipped (not enabled or no resume text)"

        # 1. Extract vacancy text
        vacancy_text = ""
        for sel in self.DESCRIPTION_SELECTORS:
            try:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible():
                    vacancy_text = await elem.inner_text()
                    break
            except Exception:
                pass

        if not vacancy_text:
            try:
                vacancy_text = await page.inner_text("body")
            except Exception:
                vacancy_text = ""

        if not vacancy_text:
            logger.warning(f"Could not extract description text for vacancy {external_id}")
            return True, 10, "Could not extract vacancy text for LLM scoring"

        # 2. Query LLM
        prompt = f"""Ты опытный IT-рекрутер. Оцени, насколько кандидат с данным резюме подходит на вакансию.
Выдай ответ ИСКЛЮЧИТЕЛЬНО в формате JSON со следующими полями:
- "score": целое число от 1 до 10 (где 10 — полное соответствие требованиям, 1 — кандидат абсолютно не подходит).
- "reason": краткое пояснение решения на русском языке (1-2 предложения).

РЕЗЮМЕ КАНДИДАТА:
{self.resume_text}

ТЕКСТ ВАКАНСИИ:
{vacancy_text[:3500]}
"""

        try:
            logger.info(f"Asking LLM to evaluate vacancy {external_id} against candidate resume...")
            raw_response = await self.llm_client.complete(prompt, system="Ты JSON-ассистент. Отвечай только валидным JSON.")
            
            # Extract JSON from potential markdown blocks ```json ... ```
            json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                score = int(data.get("score", 10))
                reason = str(data.get("reason", "No reason provided"))
                
                logger.info(f"LLM match evaluation for [{external_id}]: Score = {score}/{self.min_score}, Reason: {reason}")
                if score < self.min_score:
                    return False, score, reason
                return True, score, reason
        except Exception as e:
            logger.error(f"Error during LLM match evaluation for vacancy {external_id}: {e}")

        return True, 10, "LLM scoring failed, proceeding by default"

    async def apply_to_vacancy(self, page: Page, vacancy_url: str, external_id: str, vacancy_id: Optional[int] = None) -> VacancyApplication:
        logger.info(f"Navigating to vacancy URL: {vacancy_url}")
        try:
            await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Failed to navigate to {vacancy_url}: {e}")
            return VacancyApplication(
                vacancy_id=vacancy_id,
                external_id=external_id,
                vacancy_url=vacancy_url,
                status=ApplicationStatus.FAILED,
                error_message=f"Navigation failed: {e}"
            )

        # 1. Check if already applied
        for sel in self.ALREADY_APPLIED_SELECTORS:
            try:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible():
                    logger.info(f"Already applied to vacancy {external_id} ({vacancy_url})")
                    return VacancyApplication(
                        vacancy_id=vacancy_id,
                        external_id=external_id,
                        vacancy_url=vacancy_url,
                        status=ApplicationStatus.ALREADY_APPLIED,
                        response_text="Already applied"
                    )
            except Exception:
                pass

        # 1.5 Evaluate match with LLM if enabled
        is_match, score, reason = await self._evaluate_vacancy_match(page, external_id)
        if not is_match:
            logger.info(f"Skipping vacancy {external_id}: LLM score {score} < {self.min_score}. Reason: {reason}")
            return VacancyApplication(
                vacancy_id=vacancy_id,
                external_id=external_id,
                vacancy_url=vacancy_url,
                status=ApplicationStatus.SKIPPED_LOW_SCORE,
                response_text=f"Score: {score}/{self.min_score}. {reason}"
            )

        # 2. Find Apply button
        apply_btn = None
        for sel in self.APPLY_BUTTON_SELECTORS:
            try:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible() and await elem.is_enabled():
                    apply_btn = elem
                    break
            except Exception:
                pass

        if not apply_btn:
            logger.warning(f"Apply button not found or already applied for vacancy {external_id}")
            return VacancyApplication(
                vacancy_id=vacancy_id,
                external_id=external_id,
                vacancy_url=vacancy_url,
                status=ApplicationStatus.FAILED,
                error_message="Apply button not found"
            )

        # 3. Click Apply button
        logger.info(f"Clicking Apply button on vacancy {external_id}...")
        await apply_btn.click()
        await asyncio.sleep(1.5)

        # 4. Check if employer questions modal appeared
        has_questions = False
        for q_sel in self.QUESTION_CONTAINER_SELECTORS:
            try:
                q_elem = await page.query_selector(q_sel)
                if q_elem and await q_elem.is_visible():
                    has_questions = True
                    break
            except Exception:
                pass

        # 4.1 A cover letter is not a questionnaire: write it and carry on.
        letter_text = ""
        letter_field = None if has_questions else await self._find_letter_field(page)
        if letter_field is not None:
            letter_text = await self._fill_cover_letter(page, letter_field, external_id)
            if letter_text is None:
                # The letter is required but could not be produced.
                await self._close_modal(page)
                return VacancyApplication(
                    vacancy_id=vacancy_id,
                    external_id=external_id,
                    vacancy_url=vacancy_url,
                    status=ApplicationStatus.SKIPPED_QUESTIONS,
                    response_text="Требуется сопроводительное письмо, сгенерировать не удалось",
                )

        # Any other free-form input left in the modal is a real questionnaire.
        if not has_questions:
            has_questions = await self._has_unanswered_inputs(page)

        if has_questions:
            logger.info(f"Vacancy {external_id} requires answering questions. Skipping as requested.")
            # Close modal if present
            await self._close_modal(page)
            return VacancyApplication(
                vacancy_id=vacancy_id,
                external_id=external_id,
                vacancy_url=vacancy_url,
                status=ApplicationStatus.SKIPPED_QUESTIONS,
                response_text="Skipped due to mandatory questions/test"
            )

        # 5. Submit the response popup if it appeared.
        submit_btn = await self._first_visible(page, self.SUBMIT_POPUP_SELECTORS)

        if submit_btn:
            # hh.ru keeps the button disabled until the form is valid — clicking
            # it in that state does nothing and used to be reported as success.
            if not await self._wait_until_enabled(submit_btn):
                logger.warning(f"Submit button stayed disabled for vacancy {external_id}.")
                await self._close_modal(page)
                return VacancyApplication(
                    vacancy_id=vacancy_id,
                    external_id=external_id,
                    vacancy_url=vacancy_url,
                    status=ApplicationStatus.FAILED,
                    error_message="Кнопка отправки осталась заблокированной — форма не заполнена",
                )

            logger.info(f"Submitting popup response for vacancy {external_id}...")
            await submit_btn.click()
            await asyncio.sleep(1.5)

        # 6. Verify success
        for sel in self.ALREADY_APPLIED_SELECTORS:
            try:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible():
                    logger.info(f"Successfully applied to vacancy {external_id}!")
                    return VacancyApplication(
                        vacancy_id=vacancy_id,
                        external_id=external_id,
                        vacancy_url=vacancy_url,
                        status=ApplicationStatus.APPLIED,
                        response_text=_success_note(letter_text),
                        cover_letter=letter_text,
                    )
            except Exception:
                pass

        logger.info(f"Applied to vacancy {external_id} (direct response registered).")
        return VacancyApplication(
            vacancy_id=vacancy_id,
            external_id=external_id,
            vacancy_url=vacancy_url,
            status=ApplicationStatus.APPLIED,
            response_text=_success_note(letter_text),
            cover_letter=letter_text,
        )

    # --- cover letter ----------------------------------------------------

    async def _find_letter_field(self, page: Page):
        """Return the visible cover-letter textarea, opening its toggle if needed."""
        field = await self._first_visible(page, self.LETTER_TEXTAREA_SELECTORS)
        if field:
            return field

        # Only look behind the "add a cover letter" toggle when we are supposed
        # to write one even though hh.ru does not demand it.
        if self.cover_letter_when != "always" or not self.cover_letter_writer:
            return None

        toggle = await self._first_visible(page, self.LETTER_TOGGLE_SELECTORS)
        if not toggle:
            return None
        try:
            await toggle.click()
            await asyncio.sleep(0.8)
        except Exception as e:
            logger.warning(f"Could not open the cover letter field: {e}")
            return None
        return await self._first_visible(page, self.LETTER_TEXTAREA_SELECTORS)

    async def _fill_cover_letter(self, page: Page, field, external_id: str) -> Optional[str]:
        """Write the letter into the field. None means «required but impossible»."""
        required = await self._is_letter_required(page, field)

        if not self.cover_letter_writer:
            logger.info(f"Cover letter field found for {external_id}, generation is off.")
            return None if required else ""

        vacancy_text = await self._extract_vacancy_text(page)
        title, company = await self._vacancy_title_and_company(page)

        try:
            letter = await self.cover_letter_writer.write(title, company, vacancy_text)
        except CoverLetterError as e:
            logger.warning(f"Cover letter for {external_id} not generated: {e}")
            letter = self.cover_letter_fallback.strip()
            if not letter:
                return None if required else ""

        try:
            await field.click()
            await field.fill(letter)
            # hh.ru enables the submit button from its own input handler, so the
            # text is followed by real key events — a programmatic fill alone can
            # leave the form thinking it is still empty.
            await field.press("Space")
            await field.press("Backspace")
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.error(f"Could not type the cover letter for {external_id}: {e}")
            return None if required else ""

        logger.info(f"Cover letter filled in for {external_id} ({len(letter)} chars).")
        return letter

    async def _is_letter_required(self, page: Page, field) -> bool:
        """Is the letter mandatory for this vacancy?

        hh.ru does not mark the field as required; instead it keeps the submit
        button disabled until a letter is typed. An empty letter with a disabled
        submit button is therefore exactly the «letter required» case.
        """
        try:
            if await field.get_attribute("required") is not None:
                return True
            if str(await field.get_attribute("aria-required")).lower() == "true":
                return True
        except Exception:
            pass

        submit = await self._first_visible(page, self.SUBMIT_POPUP_SELECTORS)
        if not submit:
            return False
        try:
            return not await submit.is_enabled() and not (await field.input_value()).strip()
        except Exception:
            return False

    async def _wait_until_enabled(self, element, timeout_sec: float = 6.0) -> bool:
        """Wait for a button to become clickable; hh.ru enables it once valid."""
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            try:
                if await element.is_enabled():
                    return True
            except Exception:
                return False
            await asyncio.sleep(0.3)
        return False

    async def _has_unanswered_inputs(self, page: Page) -> bool:
        """Free-form fields other than the cover letter mean a questionnaire."""
        selector = ", ".join(
            f"{form} textarea, {form} input[type='text']" for form in self.RESPONSE_FORM_SELECTORS
        )
        try:
            inputs = await page.query_selector_all(selector)
        except Exception:
            return False

        for element in inputs:
            try:
                if not await element.is_visible():
                    continue
                if await self._is_letter_element(element):
                    continue
                if (await element.input_value()).strip():
                    continue  # already filled in (by us or by hh.ru)
                return True
            except Exception:
                continue
        return False

    async def _is_letter_element(self, element) -> bool:
        try:
            data_qa = (await element.get_attribute("data-qa") or "").lower()
            name = (await element.get_attribute("name") or "").lower()
        except Exception:
            return False
        return "letter" in data_qa or name == "letter"

    async def _first_visible(self, page: Page, selectors: List[str]):
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    return element
            except Exception:
                continue
        return None

    async def _extract_vacancy_text(self, page: Page) -> str:
        for selector in self.DESCRIPTION_SELECTORS:
            try:
                element = await page.query_selector(selector)
                if element:
                    text = (await element.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        try:
            return await page.inner_text("body")
        except Exception:
            return ""

    async def _vacancy_title_and_company(self, page: Page) -> tuple[str, str]:
        title = company = ""
        for selector in ('[data-qa="vacancy-title"]', "h1"):
            try:
                element = await page.query_selector(selector)
                if element:
                    title = (await element.inner_text()).strip()
                    break
            except Exception:
                continue
        for selector in ('[data-qa="vacancy-company-name"]', '[data-qa="vacancy-company__details"]'):
            try:
                element = await page.query_selector(selector)
                if element:
                    company = (await element.inner_text()).strip()
                    break
            except Exception:
                continue
        return title, company

    async def _close_modal(self, page: Page) -> None:
        for sel in self.CLOSE_POPUP_SELECTORS:
            try:
                elem = await page.query_selector(sel)
                if elem and await elem.is_visible():
                    await elem.click()
                    break
            except Exception:
                pass


def _success_note(letter_text: str) -> str:
    """What ends up in the applications table for a successful response."""
    if not letter_text:
        return "Applied successfully"
    return "Отклик отправлен с сопроводительным письмом:\n" + letter_text
