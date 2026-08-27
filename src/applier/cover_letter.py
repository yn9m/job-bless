"""Generating a cover letter for a vacancy that asks for one.

The text is produced from the active resume and the vacancy description, then
cleaned up: models like to leave placeholders like «[Ваше имя]», markdown and
sign-offs that make a letter look auto-generated at a glance.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from src.llm import ChatRequest, LLMClient, LLMError, Message

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "Ты пишешь сопроводительное письмо к отклику на вакансию от лица кандидата. "
    "Пиши по-русски, от первого лица, без приветствий вида «Здравствуйте» и без подписи. "
    "3–5 предложений: чем кандидат подходит именно этой вакансии, какой релевантный опыт "
    "и технологии у него есть, и короткое завершение о готовности обсудить детали. "
    "Опирайся только на факты из резюме, ничего не выдумывай. "
    "Без markdown, без списков, без плейсхолдеров в скобках, без темы письма."
)

MAX_VACANCY_CHARS = 4000
MAX_RESUME_CHARS = 4000
MIN_LETTER_CHARS = 120

# Anything the model might leave for a human to fill in.
PLACEHOLDER_RE = re.compile(r"[\[\{<](?:[^\]\}>\n]{0,60})[\]\}>]")
MARKDOWN_RE = re.compile(r"[*_`#]+")
GREETING_RE = re.compile(r"^\s*(здравствуйте|добрый день|добрый вечер|доброе утро|привет)[!,.\s]*", re.I)
SIGNOFF_RE = re.compile(
    r"\n\s*(с уважением|искренне ваш|заранее спасибо)[\s\S]{0,120}$", re.I
)


class CoverLetterError(RuntimeError):
    """The letter could not be produced in a usable form."""


@dataclass
class CoverLetterConfig:
    enabled: bool = True
    # Empty means "the same model that scores vacancies".
    model: str = ""
    # required — only when hh.ru asks for a letter; always — also fill the
    # optional letter field when the vacancy offers one.
    when: str = "required"
    max_chars: int = 1200
    prompt: str = DEFAULT_PROMPT
    fallback_text: str = ""


class CoverLetterWriter:
    def __init__(self, llm: LLMClient, config: CoverLetterConfig, resume_text: str):
        self.llm = llm
        self.config = config
        self.resume_text = resume_text

    async def write(self, vacancy_title: str, company: str, vacancy_text: str) -> str:
        """Produce a letter, or raise `CoverLetterError` if it cannot be trusted."""
        if not self.resume_text.strip():
            raise CoverLetterError("нет текста резюме для письма")

        request = ChatRequest(
            messages=[
                Message.user(
                    f"ВАКАНСИЯ: {vacancy_title}\n"
                    f"КОМПАНИЯ: {company}\n\n"
                    f"ОПИСАНИЕ ВАКАНСИИ:\n{vacancy_text[:MAX_VACANCY_CHARS]}\n\n"
                    f"РЕЗЮМЕ КАНДИДАТА:\n{self.resume_text[:MAX_RESUME_CHARS]}\n\n"
                    f"Напиши сопроводительное письмо не длиннее {self.config.max_chars} символов."
                )
            ],
            system=self.config.prompt or DEFAULT_PROMPT,
            model=self.config.model or None,
            max_tokens=min(2000, max(300, self.config.max_chars)),
        )

        try:
            response = await self.llm.chat(request)
        except LLMError as e:
            raise CoverLetterError(f"нейросеть не ответила: {e}") from e

        letter = self.clean(response.text, self.config.max_chars)
        if len(letter) < MIN_LETTER_CHARS:
            raise CoverLetterError(f"письмо получилось слишком коротким ({len(letter)} символов)")
        logger.info("cover letter generated: %d chars for %r", len(letter), vacancy_title[:60])
        return letter

    @staticmethod
    def clean(text: str, max_chars: int) -> str:
        """Strip the tells of a generated letter and clamp it to the limit."""
        letter = (text or "").strip()
        if letter.startswith("```"):
            letter = letter.strip("`")
            letter = re.sub(r"^\s*\w+\n", "", letter)

        letter = MARKDOWN_RE.sub("", letter)
        letter = PLACEHOLDER_RE.sub("", letter)
        letter = GREETING_RE.sub("", letter)
        letter = SIGNOFF_RE.sub("", letter)
        letter = re.sub(r"[ \t]{2,}", " ", letter)
        letter = re.sub(r"\n{3,}", "\n\n", letter).strip()

        if len(letter) > max_chars:
            cut = letter[:max_chars]
            # Prefer to end on a sentence rather than mid-word.
            boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
            letter = cut[: boundary + 1].strip() if boundary > max_chars * 0.5 else cut.strip()

        return letter


def build_writer(
    llm: Optional[LLMClient], config: CoverLetterConfig, resume_text: str
) -> Optional[CoverLetterWriter]:
    """Writer for the applier, or None when letters are off/unavailable."""
    if not config.enabled or not llm or not resume_text.strip():
        return None
    return CoverLetterWriter(llm, config, resume_text)
