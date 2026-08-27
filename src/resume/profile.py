"""Building the candidate profile: one condensed text for every LLM task.

The resume from hh.ru plus the free-form context the candidate writes by hand
can be very long. Feeding all of it into every vacancy comparison is expensive
and blurs the signal, so it is summarised once by a model chosen separately —
usually a heavier one, because this runs rarely and the quality of everything
downstream depends on it.

The result is what scoring, cover letters and (later) employer questions read.
"""

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from src.db.models import Resume
from src.llm import ChatRequest, LLMClient, LLMError, Message

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "Ты составляешь профиль кандидата для сравнения с вакансиями и для написания "
    "сопроводительных писем. На вход — резюме с hh.ru и дополнительный рассказ кандидата "
    "о своём опыте. Собери из них один связный профиль на русском языке.\n"
    "Обязательно сохрани: должности и уровень, годы и длительность опыта, все технологии "
    "и инструменты, доменные области, достижения с цифрами, образование и сертификаты, "
    "языки, формат работы и пожелания по условиям, контактные предпочтения.\n"
    "Ничего не выдумывай и не обобщай в ущерб деталям: лучше сухой перечень фактов, "
    "чем красивый текст. Без markdown и без вступлений вида «Вот профиль»."
)

# Above this the source is summarised in chunks first (map), then merged (reduce).
CHUNK_THRESHOLD_CHARS = 14000
CHUNK_CHARS = 12000
MAX_CHUNKS = 8
MIN_PROFILE_CHARS = 200


class ProfileError(RuntimeError):
    """The profile could not be built."""


@dataclass
class ProfileConfig:
    model: str = ""
    prompt: str = DEFAULT_PROMPT
    max_chars: int = 6000
    max_tokens: int = 4000
    timeout_sec: float = 180.0


@dataclass
class ProfileResult:
    text: str
    model: str
    chunks: int
    source_chars: int


class ProfileBuilder:
    def __init__(self, llm: LLMClient, config: ProfileConfig):
        self.llm = llm
        self.config = config

    async def build(
        self, resume: Resume, log: Optional[Callable[[str], None]] = None
    ) -> ProfileResult:
        say = log or logger.info
        source = resume.source_text()
        if not source.strip():
            raise ProfileError("нечего суммировать: нет ни текста резюме, ни контекста")

        if len(source) <= CHUNK_THRESHOLD_CHARS:
            say(f"собираю профиль из {len(source)} символов одним запросом")
            text = await self._summarise(source, part=None)
            chunks = 1
        else:
            pieces = _split(source, CHUNK_CHARS)[:MAX_CHUNKS]
            say(f"источник большой ({len(source)} символов) — сжимаю по частям: {len(pieces)}")
            partials: List[str] = []
            for index, piece in enumerate(pieces, start=1):
                partials.append(await self._summarise(piece, part=(index, len(pieces))))
                say(f"часть {index}/{len(pieces)} готова")
            say("свожу части в единый профиль")
            text = await self._summarise("\n\n".join(partials), part=None, merging=True)
            chunks = len(pieces)

        text = text.strip()[: self.config.max_chars]
        if len(text) < MIN_PROFILE_CHARS:
            raise ProfileError(f"профиль получился слишком коротким ({len(text)} символов)")

        say(f"профиль готов: {len(text)} символов, модель {self.config.model or 'по умолчанию'}")
        return ProfileResult(
            text=text, model=self.config.model, chunks=chunks, source_chars=len(source)
        )

    async def _summarise(self, text: str, part=None, merging: bool = False) -> str:
        if merging:
            instruction = (
                "Ниже — несколько кусков профиля одного и того же кандидата. "
                "Объедини их в один профиль без повторов, ничего не потеряв."
            )
        elif part:
            instruction = (
                f"Это часть {part[0]} из {part[1]} материалов кандидата. "
                "Выпиши из неё все факты о кандидате, ничего не додумывая."
            )
        else:
            instruction = "Составь профиль кандидата по материалам ниже."

        request = ChatRequest(
            messages=[Message.user(f"{instruction}\n\n{text}")],
            system=self.config.prompt or DEFAULT_PROMPT,
            model=self.config.model or None,
            max_tokens=self.config.max_tokens,
            timeout_sec=self.config.timeout_sec,
        )
        try:
            response = await self.llm.chat(request)
        except LLMError as e:
            raise ProfileError(f"нейросеть не ответила: {e}") from e
        return response.text


def _split(text: str, size: int) -> List[str]:
    """Split on paragraph boundaries so facts are not cut mid-sentence."""
    paragraphs = text.split("\n\n")
    chunks: List[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= size:
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue
        if current:
            chunks.append(current)
        # A single paragraph longer than the limit is cut by length.
        while len(paragraph) > size:
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        current = paragraph

    if current:
        chunks.append(current)
    return chunks
