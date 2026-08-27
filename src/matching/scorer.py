"""Scoring vacancies against the active resume with the configured LLM."""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from src.db.models import Resume, VacancyScore
from src.llm import ChatRequest, LLMClient, LLMError, Message, ResponseFormat

logger = logging.getLogger(__name__)

SCORE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Соответствие резюме вакансии от 0 до 100"},
        "verdict": {"type": "string", "description": "Обоснование в одном-двух предложениях"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "verdict"],
}

MAX_VACANCY_CHARS = 6000
MAX_RESUME_CHARS = 8000


def build_vacancy_text(row: Dict[str, Any]) -> str:
    """Assemble the vacancy description from the stored card."""
    raw: Dict[str, Any] = {}
    raw_json = row.get("raw_json")
    if raw_json:
        try:
            parsed = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            raw = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            raw = {}

    skills = raw.get("skills") or []
    tags = raw.get("tags") or []
    lines = [
        f"Название: {row.get('title') or raw.get('title', '')}",
        f"Компания: {row.get('company_name') or raw.get('company_name', '')}",
        f"Зарплата: {row.get('salary_text') or raw.get('salary_text', '')}",
        f"Город: {row.get('city') or raw.get('city', '')}",
        f"Формат работы: {row.get('work_format') or raw.get('work_format', '')}",
        f"График: {row.get('schedule') or raw.get('schedule', '')}",
        f"Требуемый опыт: {row.get('experience') or raw.get('experience', '')}",
        f"Тип занятости: {row.get('employment_type') or raw.get('employment_type', '')}",
        f"Навыки в вакансии: {', '.join(str(s) for s in skills)}" if skills else "",
        f"Теги: {', '.join(str(t) for t in tags)}" if tags else "",
        f"Описание: {raw.get('snippet', '')}" if raw.get("snippet") else "",
    ]
    text = "\n".join(line for line in lines if line and not line.endswith(": "))
    return text[:MAX_VACANCY_CHARS]


class VacancyScorer:
    """Turns (resume, vacancy) pairs into `VacancyScore` records."""

    def __init__(self, llm: LLMClient, prompt: str, model_name: str = ""):
        self.llm = llm
        self.prompt = prompt
        self.model_name = model_name

    async def score_one(self, resume: Resume, vacancy_row: Dict[str, Any]) -> VacancyScore:
        vacancy_id = int(vacancy_row["id"])
        resume_text = resume.as_prompt_text()[:MAX_RESUME_CHARS]
        vacancy_text = build_vacancy_text(vacancy_row)

        request = ChatRequest(
            messages=[
                Message.user(
                    f"РЕЗЮМЕ КАНДИДАТА:\n{resume_text}\n\n"
                    f"ВАКАНСИЯ:\n{vacancy_text}\n\n"
                    "Оцени соответствие и верни JSON."
                )
            ],
            system=self.prompt,
            response_format=ResponseFormat(type="json_schema", schema=SCORE_SCHEMA),
        )

        try:
            response = await self.llm.chat(request)
        except LLMError as e:
            logger.warning("scoring failed for vacancy %s: %s", vacancy_id, e)
            return VacancyScore(
                vacancy_id=vacancy_id, resume_id=resume.id or 0,
                error_message=str(e), model=self.model_name,
            )

        payload = _extract_json(response.text)
        if payload is None:
            logger.warning("model returned non-JSON for vacancy %s: %.200s", vacancy_id, response.text)
            return VacancyScore(
                vacancy_id=vacancy_id, resume_id=resume.id or 0,
                error_message="модель вернула не-JSON ответ",
                model=response.model or self.model_name,
            )

        return VacancyScore(
            vacancy_id=vacancy_id,
            resume_id=resume.id or 0,
            score=_clamp_score(payload.get("score")),
            verdict=str(payload.get("verdict", ""))[:2000],
            matched_skills=[str(s) for s in (payload.get("matched_skills") or [])][:30],
            missing_skills=[str(s) for s in (payload.get("missing_skills") or [])][:30],
            model=response.model or self.model_name,
        )

    async def score_many(
        self,
        resume: Resume,
        vacancy_rows: List[Dict[str, Any]],
        *,
        concurrency: int = 3,
        on_result: Optional[Callable[[VacancyScore, Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> List[VacancyScore]:
        """Score a batch with bounded concurrency, reporting each result."""
        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: List[VacancyScore] = []

        async def worker(row: Dict[str, Any]) -> None:
            if should_stop and should_stop():
                return
            async with semaphore:
                if should_stop and should_stop():
                    return
                score = await self.score_one(resume, row)
                results.append(score)
                if on_result:
                    outcome = on_result(score, row)
                    if asyncio.iscoroutine(outcome):
                        await outcome

        await asyncio.gather(*(worker(row) for row in vacancy_rows))
        return results


def _clamp_score(value: Any) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse the model answer, tolerating ```json fences and surrounding prose."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()

    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
