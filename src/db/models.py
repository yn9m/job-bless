from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict, Any


class SearchRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MANUAL_ACTION_REQUIRED = "manual_action_required"


class SearchPageStatus(str, Enum):
    RECEIVING = "receiving"
    COMPLETED = "completed"
    FAILED = "failed"


class ApplicationStatus(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    SKIPPED_QUESTIONS = "skipped_questions"
    SKIPPED_LOW_SCORE = "skipped_low_score"
    FAILED = "failed"


class TaskKind(str, Enum):
    COLLECT = "collect"
    SCORE = "score"
    APPLY = "apply"
    RESUME_IMPORT = "resume_import"
    RESUME_TOUCH = "resume_touch"
    PROFILE = "profile"
    ACTIVITY = "activity"
    LOGIN = "login"


class TaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApplyMode(str, Enum):
    MANUAL = "manual"  # UI decides what to apply to
    AUTO = "auto"      # everything above the threshold, right after scoring


@dataclass
class Resume:
    """A resume imported from hh.ru and used as the scoring baseline."""

    source_url: str = ""
    external_id: str = ""
    title: str = ""
    full_name: str = ""
    city: str = ""
    experience_text: str = ""
    salary_text: str = ""
    skills: List[str] = field(default_factory=list)
    summary: str = ""
    education_text: str = ""
    certificates: List[str] = field(default_factory=list)
    raw_text: str = ""
    is_active: bool = False
    # What to search on hh.ru with this resume — each resume hunts its own roles.
    search_query: str = ""
    # Free-form context the candidate adds by hand: projects, details, anything
    # the hh.ru resume does not say. Can be long.
    context_text: str = ""
    # Condensed profile built once by a heavy model out of the two texts above.
    # Everything that talks to the LLM about the candidate uses this.
    profile_summary: str = ""
    profile_model: str = ""
    profile_hash: str = ""
    profile_updated_at: Optional[datetime] = None
    id: Optional[int] = None
    imported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def source_text(self, max_chars: int = 200000) -> str:
        """Everything known about the candidate, for building the profile."""
        parts = [
            f"РЕЗЮМЕ С HH.RU:\n{self.raw_text}" if self.raw_text else "",
            f"ДОПОЛНИТЕЛЬНО ОТ КАНДИДАТА:\n{self.context_text}" if self.context_text else "",
        ]
        return "\n\n".join(p for p in parts if p)[:max_chars]

    def content_fingerprint(self) -> str:
        """Changes whenever the profile would need rebuilding."""
        import hashlib

        payload = f"{self.raw_text}\x00{self.context_text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    @property
    def profile_is_stale(self) -> bool:
        return bool(self.profile_summary) and self.profile_hash != self.content_fingerprint()

    def as_prompt_text(self, max_chars: int = 8000) -> str:
        """Textual form fed to the LLM about the candidate.

        The condensed profile wins when it exists: it is built once from the
        resume plus the free-form context, so it carries more than the hh.ru page
        alone and costs far fewer tokens per vacancy. Without it the structured
        fields come first and the recognised page text is appended while it fits.
        """
        if self.profile_summary.strip():
            return self.profile_summary[:max_chars]

        parts = [
            f"Желаемая должность: {self.title}" if self.title else "",
            f"Город: {self.city}" if self.city else "",
            f"Зарплатные ожидания: {self.salary_text}" if self.salary_text else "",
            f"Ключевые навыки: {', '.join(self.skills)}" if self.skills else "",
            f"Опыт работы:\n{self.experience_text}" if self.experience_text else "",
            f"Образование:\n{self.education_text}" if self.education_text else "",
            f"Сертификаты: {'; '.join(self.certificates)}" if self.certificates else "",
            f"О себе:\n{self.summary}" if self.summary else "",
        ]
        text = "\n\n".join(p for p in parts if p)
        if not text:
            return self.raw_text[:max_chars]

        remaining = max_chars - len(text)
        if self.raw_text and remaining > 500:
            text += "\n\nПолный текст резюме:\n" + self.raw_text[:remaining - 30]
        return text[:max_chars]


@dataclass
class VacancyScore:
    """LLM verdict on how well a vacancy matches the active resume."""

    vacancy_id: int
    resume_id: int
    score: int = 0
    verdict: str = ""
    matched_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)
    model: str = ""
    error_message: str = ""
    scored_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TaskRun:
    """One background job started from the web UI or by the scheduler."""

    id: str
    kind: TaskKind
    status: TaskStatus = TaskStatus.RUNNING
    trigger: str = "manual"  # manual | schedule
    params: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None


@dataclass
class VacancyApplication:
    external_id: str
    vacancy_url: str
    status: ApplicationStatus
    vacancy_id: Optional[int] = None
    response_text: str = ""
    error_message: str = ""
    # Kept apart from response_text so the UI can show what was actually sent.
    cover_letter: str = ""
    applied_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class VacancyCard:
    source: str = "hh"
    external_id: str = ""
    url: str = ""
    title: str = ""
    company_name: str = ""
    company_id: str = ""
    company_url: str = ""
    salary_text: str = ""
    salary_from: Optional[int] = None
    salary_to: Optional[int] = None
    currency: str = ""
    gross: bool = False
    city: str = ""
    work_format: str = ""
    schedule: str = ""
    experience: str = ""
    employment_type: str = ""
    published_at_text: str = ""
    tags: List[str] = field(default_factory=list)
    snippet: str = ""
    skills: List[str] = field(default_factory=list)
    is_direct_employer: bool = False
    is_accredited_it: bool = False
    is_premium: bool = False
    raw_text: str = ""
    page_key: str = ""
    page_number: int = 1
    position_on_page: int = 1
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    search_url: str = ""


@dataclass
class CollectionSummary:
    task_id: str
    total_pages_processed: int = 0
    total_cards_found: int = 0
    unique_vacancies: int = 0
    duplicate_cards: int = 0
    invalid_cards: int = 0
    partial_cards: int = 0
    last_processed_url: str = ""
    completion_reason: str = "completed_successfully"
    duration_seconds: float = 0.0
    final_status: str = "completed"


@dataclass
class SearchRun:
    id: str
    task_id: str
    search_url: str
    browser_session_id: str = ""
    transport: str = "playwright"
    status: SearchRunStatus = SearchRunStatus.RUNNING
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    total_pages_processed: int = 0
    total_cards_found: int = 0
    unique_vacancies_count: int = 0
    duplicate_cards_count: int = 0
    invalid_cards_count: int = 0
    last_processed_url: str = ""
    completion_reason: str = ""
    error_code: str = ""
    error_message: str = ""
    collector_version: str = "0.2.0"
    # Which resume this search was run for — the vacancies list filters by it.
    resume_id: Optional[int] = None


@dataclass
class PageCommitParams:
    search_run_id: str
    page_key: str
    page_number: int
    current_url: str
    canonical_url: str
    cards: List[VacancyCard]
