"""Parsing of an hh.ru resume page into the `Resume` entity.

Structured fields are best-effort: hh.ru markup changes often, so the whole
page text is always kept in `raw_text` and used as the scoring fallback.
"""

import asyncio
import logging
import re
from typing import List, Optional
from playwright.async_api import Page

from src.db.models import Resume
from src.resume.sections import parse_sections

logger = logging.getLogger(__name__)

RESUME_URL_RE = re.compile(r"^https?://(www\.)?(hh\.ru|hh\.kz|hh\.uz|rabota\.by)/resume/([0-9a-zA-Z]+)", re.I)

TITLE_SELECTORS = ['[data-qa="resume-block-title-position"]', '[data-qa="resume-block-position"]']
SALARY_SELECTORS = ['[data-qa="resume-block-salary"]']
NAME_SELECTORS = ['[data-qa="resume-personal-name"]']
CITY_SELECTORS = ['[data-qa="resume-personal-address"]']
EXPERIENCE_TOTAL_SELECTORS = [
    '[data-qa="resume-block-experience"] .bloko-text_strong',
    '[data-qa="resume-block-experience"] h2 + span',
    '[data-qa="resume-block-experience"] .resume-block__title-text_sub',
]
SUMMARY_SELECTORS = ['[data-qa="resume-block-skills-content"]', '[data-qa="resume-block-about"]']
SKILL_SELECTORS = [
    '[data-qa="skills-table"] [data-qa="bloko-tag__text"]',
    '[data-qa="resume-block-skills"] [data-qa="bloko-tag__text"]',
    '.bloko-tag-list [data-qa="bloko-tag__text"]',
]
EXPERIENCE_ITEM_SELECTORS = ['[data-qa="resume-block-experience"] .resume-block-item-gap']


def extract_resume_id(url: str) -> str:
    match = RESUME_URL_RE.match(url.strip())
    return match.group(3) if match else ""


def is_resume_url(url: str) -> bool:
    return bool(RESUME_URL_RE.match(url.strip()))


class HHResumeParser:
    """Opens a resume page in an authenticated browser and extracts its content."""

    async def parse(self, page: Page, resume_url: str, timeout_ms: int = 30000) -> Resume:
        logger.info("opening resume page: %s", resume_url)
        await page.goto(resume_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await asyncio.sleep(1.5)

        if "/account/login" in page.url or "/auth/" in page.url:
            raise PermissionError(
                "hh.ru потребовал вход. Войдите в аккаунт через кнопку «Войти в hh.ru» и повторите импорт."
            )

        raw_text = await self._page_text(page)
        if not raw_text.strip():
            raise ValueError("страница резюме пустая — возможно, ссылка неверная или доступ закрыт")

        # Section headings survive hh.ru markup changes, CSS selectors do not,
        # so the text split is the primary source and the DOM only refines it.
        sections = parse_sections(raw_text)

        resume = Resume(
            source_url=resume_url,
            external_id=extract_resume_id(resume_url),
            title=await self._first_text(page, TITLE_SELECTORS),
            full_name=await self._first_text(page, NAME_SELECTORS),
            city=await self._first_text(page, CITY_SELECTORS),
            salary_text=await self._first_text(page, SALARY_SELECTORS),
            summary=sections.summary or await self._first_text(page, SUMMARY_SELECTORS),
            skills=sections.skills or await self._skills(page),
            education_text=sections.education,
            certificates=sections.certificates,
            raw_text=raw_text[:60000],
        )
        resume.experience_text = sections.experience or await self._experience_text(page, raw_text)

        logger.info(
            "resume parsed: title=%r skills=%d experience=%d chars education=%s "
            "certificates=%d summary=%d chars text=%d chars",
            resume.title, len(resume.skills), len(resume.experience_text),
            bool(resume.education_text), len(resume.certificates),
            len(resume.summary), len(resume.raw_text),
        )
        if not resume.skills and not resume.summary:
            logger.warning(
                "resume %s parsed without skills or summary — hh.ru markup may have changed",
                resume_url,
            )
        return resume

    async def _first_text(self, page: Page, selectors: List[str]) -> str:
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    text = (await element.inner_text()).strip()
                    if text:
                        return _clean(text)
            except Exception:
                continue
        return ""

    async def _skills(self, page: Page) -> List[str]:
        skills: List[str] = []
        for selector in SKILL_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
            except Exception:
                continue
            for element in elements:
                try:
                    text = _clean((await element.inner_text()).strip())
                except Exception:
                    continue
                if text and text not in skills:
                    skills.append(text)
            if skills:
                break
        return skills[:100]

    async def _experience_text(self, page: Page, raw_text: str) -> str:
        header = await self._first_text(page, EXPERIENCE_TOTAL_SELECTORS)
        if header:
            return header

        match = re.search(r"Опыт работы\s*[—-]?\s*([^\n]{0,60})", raw_text)
        if match:
            return _clean(match.group(0))

        # Fall back to the list of positions, which is enough context for scoring.
        try:
            items = await page.query_selector_all(EXPERIENCE_ITEM_SELECTORS[0])
        except Exception:
            return ""
        chunks = []
        for item in items[:10]:
            try:
                chunks.append(_clean((await item.inner_text()).strip()))
            except Exception:
                continue
        return "\n".join(c for c in chunks if c)[:4000]

    async def _page_text(self, page: Page) -> str:
        for selector in ('[data-qa="resume"]', "main", "body"):
            try:
                element = await page.query_selector(selector)
                if element:
                    text = await element.inner_text()
                    if text and text.strip():
                        return _clean(text)
            except Exception:
                continue
        return ""


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
