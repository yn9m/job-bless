"""Splitting the plain text of an hh.ru resume into meaningful sections.

The DOM markup of hh.ru differs between the public view and the owner's own
profile page and changes often, but the rendered text always keeps the same
headings. Parsing that text is therefore the reliable path; CSS selectors stay
as an optional bonus.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Headings that open a section. The first spelling is the canonical name.
SECTION_HEADINGS: Dict[str, Tuple[str, ...]] = {
    "experience": ("опыт работы",),
    "skills": ("навыки", "ключевые навыки"),
    "education": ("образование", "высшее образование"),
    "summary": ("о себе", "обо мне"),
    "certificates": ("сертификаты", "электронные сертификаты"),
}

# Interface chrome and hh.ru promo blocks: they end a section and never belong
# to its content.
STOP_HEADINGS: Tuple[str, ...] = (
    "подтверждение навыков",
    "завершённость резюме",
    "завершенность резюме",
    "ещё вы можете добавить",
    "еще вы можете добавить",
    "поднятие резюме",
    "видимость резюме",
    "по-русски",
    "in english",
    "подобрали для вас",
    "сертификаты о ваших подтверждённых навыках",
    "сертификаты о ваших подтвержденных навыках",
    "перейти к тестам",
    "посмотреть направления",
    "похожие вакансии",
    "рекомендуемые вакансии",
    "статистика просмотров",
)

# Buttons and hints rendered inside sections.
NOISE_LINES: Tuple[str, ...] = (
    "добавить",
    "редактировать",
    "изменить",
    "развернуть",
    "свернуть",
    "показать полностью",
    "указать уровни",
    "удалить",
    "профиль",
    "/",
    "...",
)

# Skill-level captions that group the tags inside the skills block.
SKILL_LEVEL_LINES: Tuple[str, ...] = (
    "продвинутый уровень",
    "средний уровень",
    "базовый уровень",
    "экспертный уровень",
    "уровень не указан",
)

YEAR_RE = re.compile(r"^(19|20)\d{2}$")


@dataclass
class ResumeSections:
    experience: str = ""
    skills: List[str] = field(default_factory=list)
    education: str = ""
    summary: str = ""
    certificates: List[str] = field(default_factory=list)


def parse_sections(raw_text: str) -> ResumeSections:
    """Split resume text into sections; every field is best-effort."""
    lines = [line.strip() for line in (raw_text or "").splitlines()]
    if not any(lines):
        return ResumeSections()

    bounds = _section_bounds(lines)
    sections = ResumeSections()

    if "experience" in bounds:
        sections.experience = _join(_content(lines, bounds["experience"]))
    if "education" in bounds:
        sections.education = _join(_content(lines, bounds["education"]))
    if "summary" in bounds:
        sections.summary = _join(_content(lines, bounds["summary"]))
    if "skills" in bounds:
        sections.skills = _skills(_content(lines, bounds["skills"]))
    if "certificates" in bounds:
        sections.certificates = _certificates(_content(lines, bounds["certificates"]))

    return sections


def _section_bounds(lines: List[str]) -> Dict[str, Tuple[int, int]]:
    """Map section -> (first content line, end line, exclusive)."""
    starts: List[Tuple[int, str]] = []
    for index, line in enumerate(lines):
        name = _heading_name(line)
        if name and not any(existing == name for _, existing in starts):
            starts.append((index, name))

    bounds: Dict[str, Tuple[int, int]] = {}
    for position, (index, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        stop = _first_stop(lines, index + 1, end)
        bounds[name] = (index + 1, stop)
    return bounds


def _heading_name(line: str) -> Optional[str]:
    """Recognise a section heading, tolerating suffixes like «Опыт работы: 3 года»."""
    normalized = line.strip().lower().rstrip(":").strip()
    if not normalized or len(normalized) > 60:
        return None
    for name, variants in SECTION_HEADINGS.items():
        for variant in variants:
            if normalized == variant or normalized.startswith(variant + ":"):
                return name
    return None


def _first_stop(lines: List[str], start: int, end: int) -> int:
    for index in range(start, end):
        normalized = lines[index].strip().lower()
        if any(normalized.startswith(stop) for stop in STOP_HEADINGS):
            return index
    return end


def _content(lines: List[str], bounds: Tuple[int, int]) -> List[str]:
    start, end = bounds
    return [line for line in lines[start:end] if line and not _is_noise(line)]


def _is_noise(line: str) -> bool:
    return line.strip().lower() in NOISE_LINES


def _join(lines: List[str]) -> str:
    return "\n".join(lines).strip()


def _skills(lines: List[str]) -> List[str]:
    skills: List[str] = []
    for line in lines:
        if line.lower() in SKILL_LEVEL_LINES or len(line) > 80:
            continue
        if line not in skills:
            skills.append(line)
    return skills


def _certificates(lines: List[str]) -> List[str]:
    """Certificate names, with the year attached when it follows on its own line."""
    certificates: List[str] = []
    for line in lines:
        if YEAR_RE.match(line):
            if certificates and not certificates[-1].endswith(")"):
                certificates[-1] = f"{certificates[-1]} ({line})"
            continue
        certificates.append(line)
    return certificates
