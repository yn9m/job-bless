"""Runtime settings editable from the web UI.

Layering: YAML (`configs/*.yaml`) provides defaults, the `app_settings` table
overrides them. Services keep consuming the dataclasses from `src/config.py` —
`SettingsService` just rebuilds those dataclasses with the overrides applied.
"""

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from src.config import BrowserConfig, Config, LLMConfig, LLMModifiersConfig, ScrollerConfig
from src.activity.service import ActivityConfig
from src.applier.cover_letter import DEFAULT_PROMPT as DEFAULT_LETTER_PROMPT, CoverLetterConfig
from src.resume.profile import DEFAULT_PROMPT as DEFAULT_PROFILE_PROMPT, ProfileConfig
from src.ratelimit import RateLimitConfig

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_URL = "https://hh.ru/search/vacancy?text="


def extract_search_query(search_url: str) -> str:
    """Pull the `text` parameter out of an hh.ru search URL."""
    try:
        params = parse_qs(urlparse(search_url).query)
    except ValueError:
        return ""
    values = params.get("text") or []
    return values[0] if values else ""


def build_search_url(template_url: str, query: str) -> str:
    """Put the query into the URL template, keeping every other filter intact."""
    url = template_url or DEFAULT_SEARCH_URL
    try:
        parts = urlparse(url)
    except ValueError:
        parts = urlparse(DEFAULT_SEARCH_URL)

    params = parse_qs(parts.query, keep_blank_values=True)
    params["text"] = [query]
    return urlunparse(parts._replace(query=urlencode(params, doseq=True)))


DEFAULT_SCORING_PROMPT = (
    "Ты помогаешь соискателю оценивать вакансии. Сравни вакансию с резюме и оцени "
    "соответствие числом от 0 до 100, где 100 — идеальное совпадение по стеку, уровню и условиям. "
    "Учитывай требуемый опыт, технологии, формат работы и зарплату. "
    "Ответь строго JSON-объектом по заданной схеме, обоснование пиши на русском в одном-двух предложениях."
)


@dataclass(frozen=True)
class SettingField:
    """One editable setting: how to render it and how to parse it back."""

    key: str
    label: str
    type: str  # str | text | int | float | bool | choice | secret
    group: str
    default: Callable[[Config], Any]
    choices: Tuple[str, ...] = ()
    help: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # Tuned once and then forgotten — hidden behind a spoiler in the UI.
    advanced: bool = False


GROUP_SEARCH = "Поиск и сбор"
GROUP_LIMITS = "Ограничение нагрузки"
GROUP_MATCHING = "Оценка соответствия"
GROUP_APPLY = "Отклики"
GROUP_PROFILE = "Профиль кандидата"
GROUP_LETTER = "Сопроводительное письмо"
GROUP_ACTIVITY = "Имитация активности"
GROUP_RESUME = "Обновление резюме"
GROUP_SCHEDULE = "Расписание"
GROUP_LLM = "Нейросеть"
GROUP_BROWSER = "Браузер"

FIELDS: Tuple[SettingField, ...] = (
    # --- поиск и сбор ---
    SettingField("scroller.load_mode", "Как читать страницу результатов", "choice", GROUP_SEARCH,
                 lambda c: c.scroller.load_mode, choices=("instant", "scroll"),
                 help="instant — дождаться списка и сразу его разобрать: hh.ru отдаёт все "
                      "50 вакансий сразу, прокрутка ничего не добавляет. "
                      "scroll — старое поведение, если страница вдруг начнёт догружаться."),
    SettingField("scroller.max_pages", "Максимум страниц за прогон", "int", GROUP_SEARCH,
                 lambda c: c.scroller.max_pages, minimum=1, maximum=9999, advanced=True),
    SettingField("scroller.max_scroll_steps_per_page", "Максимум шагов скролла на страницу", "int", GROUP_SEARCH,
                 lambda c: c.scroller.max_scroll_steps_per_page, minimum=1, maximum=1000, advanced=True,
                 help="Только для режима scroll."),
    SettingField("scroller.max_scroll_time_sec_per_page", "Лимит времени на страницу, сек", "float", GROUP_SEARCH,
                 lambda c: c.scroller.max_scroll_time_sec_per_page, minimum=1, maximum=600, advanced=True),
    SettingField("scroller.stable_cycles", "Циклов без роста страницы до остановки", "int", GROUP_SEARCH,
                 lambda c: c.scroller.stable_cycles, minimum=1, maximum=20, advanced=True),
    SettingField("scroller.scroll_step_min", "Шаг скролла, мин (px)", "int", GROUP_SEARCH,
                 lambda c: c.scroller.scroll_step_min, minimum=50, maximum=5000, advanced=True,
                 help="Сбор листает быстро: это не имитация активности, "
                      "у неё свои настройки в отдельном разделе."),
    SettingField("scroller.scroll_step_max", "Шаг скролла, макс (px)", "int", GROUP_SEARCH,
                 lambda c: c.scroller.scroll_step_max, minimum=50, maximum=5000, advanced=True),
    SettingField("scroller.scroll_pause_min_sec", "Пауза внутри шага, сек", "float", GROUP_SEARCH,
                 lambda c: c.scroller.scroll_pause_min_sec, minimum=0, maximum=10, advanced=True),
    SettingField("scroller.scroll_pause_max_sec", "Пауза после шага, сек", "float", GROUP_SEARCH,
                 lambda c: c.scroller.scroll_pause_max_sec, minimum=0, maximum=10, advanced=True),

    # --- ограничение нагрузки на hh.ru ---
    SettingField("ratelimit.enabled", "Ограничивать нагрузку на hh.ru", "bool", GROUP_LIMITS,
                 lambda c: True,
                 help="Выключать не стоит: без пауз hh.ru быстро отвечает капчей."),
    SettingField("ratelimit.min_interval_sec", "Минимум секунд между открытием страниц", "float", GROUP_LIMITS,
                 lambda c: 1.0, minimum=0, maximum=120,
                 help="Одна вакансия (или страница поиска) не чаще, чем раз в это время."),
    SettingField("ratelimit.jitter_sec", "Случайная добавка к паузе, сек", "float", GROUP_LIMITS,
                 lambda c: 0.5, minimum=0, maximum=60,
                 help="Разброс, чтобы ритм не выглядел машинным."),
    SettingField("ratelimit.requests_per_minute", "Максимум запросов к hh.ru в минуту", "int", GROUP_LIMITS,
                 lambda c: 100, minimum=1, maximum=1000,
                 help="Общий потолок для всех задач сразу: страницы, XHR, отклики."),
    SettingField("ratelimit.throttle_browser_requests", "Считать и запросы самой страницы", "bool", GROUP_LIMITS,
                 lambda c: True, advanced=True,
                 help="Учитывать фоновые запросы hh.ru (XHR), а не только открытие страниц."),

    # --- оценка соответствия ---
    SettingField("matching.enabled", "Оценивать вакансии нейросетью", "bool", GROUP_MATCHING,
                 lambda c: c.llm.enabled,
                 help="Если выключено, вакансии собираются, но не оцениваются."),
    SettingField("matching.threshold", "Порог соответствия для отклика", "int", GROUP_MATCHING,
                 lambda c: 70, minimum=0, maximum=100,
                 help="Отклик отправляется только на вакансии с оценкой не ниже порога."),
    SettingField("matching.batch_size", "Сколько вакансий оценивать за прогон", "int", GROUP_MATCHING,
                 lambda c: 50, minimum=1, maximum=500),
    SettingField("matching.concurrency", "Параллельных запросов к нейросети", "int", GROUP_MATCHING,
                 lambda c: 3, minimum=1, maximum=20),
    SettingField("matching.prompt", "Инструкция для нейросети", "text", GROUP_MATCHING,
                 lambda c: DEFAULT_SCORING_PROMPT),

    # --- отклики ---
    SettingField("apply.mode", "Режим откликов", "choice", GROUP_APPLY,
                 lambda c: "manual", choices=("manual", "auto"),
                 help="manual — отклик только по кнопке из интерфейса; "
                      "auto — бот сам откликается на всё выше порога после оценки."),
    SettingField("apply.batch_limit", "Максимум откликов за прогон", "int", GROUP_APPLY,
                 lambda c: 20, minimum=1, maximum=200),
    SettingField("apply.delay_sec", "Пауза между откликами, сек", "float", GROUP_APPLY,
                 lambda c: 2.0, minimum=0, maximum=120),
    SettingField("apply.skip_questions", "Пропускать вакансии с вопросами работодателя", "bool", GROUP_APPLY,
                 lambda c: True),
    SettingField("apply.recheck_with_llm", "Перепроверять нейросетью на странице вакансии", "bool", GROUP_APPLY,
                 lambda c: False,
                 help="Дополнительная проверка перед откликом по полному тексту вакансии, "
                      "а не по карточке из поиска. Медленнее и расходует токены."),

    # --- профиль кандидата (саммари резюме + контекста) ---
    SettingField("profile.model", "Модель для сборки профиля", "model", GROUP_PROFILE,
                 lambda c: "",
                 help="Считается один раз на резюме, поэтому здесь уместна тяжёлая модель: "
                      "от качества профиля зависят и оценки, и письма. "
                      "Пусто — та же модель, что для оценки вакансий."),
    SettingField("profile.max_chars", "Длина профиля, символов", "int", GROUP_PROFILE,
                 lambda c: 6000, minimum=500, maximum=20000),
    SettingField("profile.max_tokens", "Лимит токенов ответа", "int", GROUP_PROFILE,
                 lambda c: 4000, minimum=500, maximum=32000, advanced=True),
    SettingField("profile.timeout_sec", "Таймаут запроса, сек", "float", GROUP_PROFILE,
                 lambda c: 180.0, minimum=10, maximum=900, advanced=True,
                 help="Тяжёлая модель думает дольше обычной."),
    SettingField("profile.prompt", "Инструкция для сборки профиля", "text", GROUP_PROFILE,
                 lambda c: DEFAULT_PROFILE_PROMPT),

    # --- сопроводительное письмо ---
    SettingField("cover_letter.enabled", "Генерировать сопроводительное письмо", "bool", GROUP_LETTER,
                 lambda c: True,
                 help="Если вакансия просит письмо, бот напишет его нейросетью и всё равно "
                      "откликнется. Выключено — такие вакансии пропускаются."),
    SettingField("cover_letter.model", "Модель для писем", "model", GROUP_LETTER,
                 lambda c: "",
                 help="Пусто — та же модель, что оценивает вакансии."),
    SettingField("cover_letter.when", "Когда прикладывать письмо", "choice", GROUP_LETTER,
                 lambda c: "required", choices=("required", "always"),
                 help="required — только когда hh.ru требует письмо; "
                      "always — прикладывать и когда поле необязательное."),
    SettingField("cover_letter.max_chars", "Максимальная длина письма", "int", GROUP_LETTER,
                 lambda c: 1200, minimum=200, maximum=5000),
    SettingField("cover_letter.prompt", "Инструкция для письма", "text", GROUP_LETTER,
                 lambda c: DEFAULT_LETTER_PROMPT),
    SettingField("cover_letter.fallback_text", "Запасной текст письма", "text", GROUP_LETTER,
                 lambda c: "", advanced=True,
                 help="Отправляется, если нейросеть недоступна. Пусто — вакансия пропускается."),

    # --- имитация активности (отдельный модуль, работает параллельно сбору) ---
    SettingField("activity.url", "Страница для активности", "str", GROUP_ACTIVITY,
                 lambda c: "",
                 help="Пусто — берётся страница поиска по текущему запросу."),
    SettingField("activity.duration_min", "Длительность прогона, мин", "float", GROUP_ACTIVITY,
                 lambda c: 10.0, minimum=1, maximum=600),
    SettingField("activity.pause_min_sec", "Пауза между прокрутками, от (сек)", "float", GROUP_ACTIVITY,
                 lambda c: 1.5, minimum=0.1, maximum=120),
    SettingField("activity.pause_max_sec", "Пауза между прокрутками, до (сек)", "float", GROUP_ACTIVITY,
                 lambda c: 5.0, minimum=0.2, maximum=300),
    SettingField("activity.open_vacancies", "Иногда открывать вакансии", "bool", GROUP_ACTIVITY,
                 lambda c: False,
                 help="Похоже на живое поведение, но расходует лимит запросов."),
    SettingField("activity.open_vacancy_chance", "Вероятность открыть вакансию", "float", GROUP_ACTIVITY,
                 lambda c: 0.15, minimum=0, maximum=1, advanced=True),
    SettingField("activity.scroll_step_min", "Шаг прокрутки, мин (px)", "int", GROUP_ACTIVITY,
                 lambda c: 250, minimum=50, maximum=3000, advanced=True),
    SettingField("activity.scroll_step_max", "Шаг прокрутки, макс (px)", "int", GROUP_ACTIVITY,
                 lambda c: 700, minimum=50, maximum=3000, advanced=True),
    SettingField("activity.scroll_up_chance", "Вероятность прокрутки вверх", "float", GROUP_ACTIVITY,
                 lambda c: 0.2, minimum=0, maximum=1, advanced=True),
    SettingField("activity.read_seconds_min", "Чтение вакансии, от (сек)", "float", GROUP_ACTIVITY,
                 lambda c: 5.0, minimum=1, maximum=300, advanced=True),
    SettingField("activity.read_seconds_max", "Чтение вакансии, до (сек)", "float", GROUP_ACTIVITY,
                 lambda c: 20.0, minimum=1, maximum=600, advanced=True),

    # --- обновление резюме ---
    SettingField("resume_touch.edit_fallback", "Пересохранять «О себе», если кнопка недоступна", "bool",
                 GROUP_RESUME, lambda c: False,
                 help="Переключает один пробел в конце текста: смысл не меняется, "
                      "а дата обновления поднимается. Меняет резюме на hh.ru."),

    # --- расписание ---
    SettingField("schedule.enabled", "Включить автозапуск по расписанию", "bool", GROUP_SCHEDULE,
                 lambda c: False),
    SettingField("schedule.interval_minutes", "Интервал запуска, минут", "int", GROUP_SCHEDULE,
                 lambda c: 180, minimum=5, maximum=10080),
    SettingField("schedule.do_collect", "Собирать вакансии", "bool", GROUP_SCHEDULE, lambda c: True),
    SettingField("schedule.do_score", "Оценивать собранное", "bool", GROUP_SCHEDULE, lambda c: True),
    SettingField("schedule.do_apply", "Откликаться (только в режиме auto)", "bool", GROUP_SCHEDULE,
                 lambda c: False),
    SettingField("schedule.activity_enabled", "Запускать активность по расписанию", "bool", GROUP_SCHEDULE,
                 lambda c: False,
                 help="Отдельный цикл: крутится параллельно сбору и откликам."),
    SettingField("schedule.activity_interval_minutes", "Интервал активности, минут", "int", GROUP_SCHEDULE,
                 lambda c: 120, minimum=5, maximum=10080),
    SettingField("schedule.resume_touch_enabled", "Обновлять резюме по расписанию", "bool", GROUP_SCHEDULE,
                 lambda c: False),
    SettingField("schedule.resume_touch_interval_hours", "Интервал обновления резюме, часов", "float",
                 GROUP_SCHEDULE, lambda c: 4.0, minimum=0.5, maximum=168,
                 help="hh.ru разрешает поднимать резюме в поиске раз в 4 часа."),

    # --- нейросеть ---
    SettingField("llm.enabled", "Использовать нейросеть", "bool", GROUP_LLM, lambda c: c.llm.enabled),
    SettingField("llm.standard", "Стандарт API", "choice", GROUP_LLM,
                 lambda c: c.llm.standard, choices=("openai", "gemini", "anthropic")),
    SettingField("llm.base_url", "Базовый URL", "str", GROUP_LLM, lambda c: c.llm.base_url),
    SettingField("llm.api_key", "API-ключ", "secret", GROUP_LLM, lambda c: c.llm.api_key),
    # "model" renders as a dropdown filled from the endpoint's model list,
    # falling back to a text input when the list cannot be fetched.
    SettingField("llm.model", "Модель", "model", GROUP_LLM, lambda c: c.llm.model),
    SettingField("llm.temperature", "Температура", "float", GROUP_LLM,
                 lambda c: c.llm.temperature, minimum=0, maximum=2),
    SettingField("llm.max_tokens", "Максимум токенов в ответе", "int", GROUP_LLM,
                 lambda c: c.llm.max_tokens, minimum=64, maximum=32000),
    SettingField("llm.timeout_sec", "Таймаут запроса, сек", "float", GROUP_LLM,
                 lambda c: c.llm.timeout_sec, minimum=5, maximum=600),
    SettingField("llm.modifiers.thinking", "Режим размышления", "choice", GROUP_LLM,
                 lambda c: c.llm.modifiers.thinking, choices=("", "minimal", "low", "medium", "high")),
    SettingField("llm.modifiers.search", "Принудительный веб-поиск", "bool", GROUP_LLM,
                 lambda c: c.llm.modifiers.search),

    # --- браузер ---
    SettingField("browser.provider", "Провайдер браузера", "choice", GROUP_BROWSER,
                 lambda c: c.browser.provider, choices=("local_process", "docker", "external")),
    SettingField("browser.transport", "Транспорт", "choice", GROUP_BROWSER,
                 lambda c: c.browser.transport, choices=("cdp", "playwright")),
    SettingField("browser.headless", "Headless-режим", "bool", GROUP_BROWSER,
                 lambda c: c.browser.headless,
                 help="Для ручного входа на hh.ru и решения капчи нужен видимый браузер."),
    SettingField("browser.close_stale_tabs", "Закрывать лишние вкладки при старте", "bool", GROUP_BROWSER,
                 lambda c: c.browser.close_stale_tabs,
                 help="Вкладки, оставшиеся от прошлых запусков, продолжают грузить hh.ru "
                      "и тратят лимит запросов."),
    SettingField("browser.cdp.endpoint", "CDP endpoint", "str", GROUP_BROWSER,
                 lambda c: c.browser.cdp.endpoint),
    SettingField("browser.playwright.endpoint", "Playwright WS endpoint", "str", GROUP_BROWSER,
                 lambda c: c.browser.playwright.endpoint),
)

FIELDS_BY_KEY: Dict[str, SettingField] = {f.key: f for f in FIELDS}


class SettingsService:
    """Reads/writes UI settings and rebuilds config dataclasses with them."""

    def __init__(self, config: Config, repository):
        self.config = config
        self.repository = repository
        self._values: Dict[str, Any] = {}
        # Filters other than the query live in the URL from YAML; the query now
        # belongs to a resume, and this template supplies everything else.
        self._search_url_template = config.scroller.search_url
        # The query from before queries moved onto resume cards; used to seed a
        # resume that has none yet, and as the fallback for display.
        self._legacy_query = extract_search_query(config.scroller.search_url)

    @property
    def legacy_query(self) -> str:
        return self._legacy_query

    async def load(self) -> None:
        stored = await self.repository.get_all_settings()

        # Earlier versions stored the whole search URL, then a global
        # `search.query`. Both are kept: the URL as the filter template, the
        # query to seed resumes that do not have their own yet.
        legacy_url = stored.get("search.url")
        if legacy_url:
            self._search_url_template = legacy_url
            self._legacy_query = extract_search_query(legacy_url)
        if stored.get("search.query"):
            self._legacy_query = stored["search.query"]

        self._values = {}
        for setting in FIELDS:
            raw = stored.get(setting.key)
            value = _coerce(setting, raw) if raw is not None else None
            self._values[setting.key] = setting.default(self.config) if value is None else value
        logger.info("settings loaded: %d keys (%d overridden in db)", len(self._values), len(stored))

    def get(self, key: str, fallback: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        setting = FIELDS_BY_KEY.get(key)
        return setting.default(self.config) if setting else fallback

    def all_values(self) -> Dict[str, Any]:
        return dict(self._values)

    async def save(self, raw_values: Dict[str, str]) -> List[str]:
        """Validate and persist a form submission. Returns human-readable errors."""
        errors: List[str] = []
        to_store: Dict[str, str] = {}

        for setting in FIELDS:
            if setting.type == "bool":
                # An unchecked checkbox is simply absent from the form payload.
                value: Any = setting.key in raw_values and raw_values[setting.key] not in ("", "0", "false")
            elif setting.key not in raw_values:
                continue
            else:
                try:
                    value = _coerce(setting, raw_values[setting.key], strict=True)
                except ValueError as e:
                    errors.append(f"{setting.label}: {e}")
                    continue

            if setting.type == "secret" and value == "":
                # An empty secret field means "keep the stored key".
                continue

            self._values[setting.key] = value
            to_store[setting.key] = _serialize(value)

        if errors:
            return errors

        await self.repository.save_settings(to_store)
        logger.info("settings saved: %s", ", ".join(sorted(to_store)))
        return []

    # --- rendering helpers ------------------------------------------------

    def grouped_fields(self) -> List[Tuple[str, Dict[str, List[Dict[str, Any]]]]]:
        """Fields per group, split into everyday ones and the advanced spoiler."""
        groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for setting in FIELDS:
            group = groups.setdefault(setting.group, {"main": [], "advanced": []})
            group["advanced" if setting.advanced else "main"].append(
                {
                    "key": setting.key,
                    "label": setting.label,
                    "type": setting.type,
                    "choices": setting.choices,
                    "help": setting.help,
                    "minimum": setting.minimum,
                    "maximum": setting.maximum,
                    "advanced": setting.advanced,
                    "value": self.get(setting.key),
                    "is_default": self.get(setting.key) == setting.default(self.config),
                }
            )
        return list(groups.items())

    # --- typed views used by the services ---------------------------------

    def browser_config(self) -> BrowserConfig:
        base = self.config.browser
        cdp = replace(base.cdp, endpoint=self.get("browser.cdp.endpoint", base.cdp.endpoint))
        playwright = replace(
            base.playwright, endpoint=self.get("browser.playwright.endpoint", base.playwright.endpoint)
        )
        return replace(
            base,
            provider=self.get("browser.provider", base.provider),
            transport=self.get("browser.transport", base.transport),
            headless=bool(self.get("browser.headless", base.headless)),
            close_stale_tabs=bool(self.get("browser.close_stale_tabs", base.close_stale_tabs)),
            cdp=cdp,
            playwright=playwright,
        )

    def search_url_for(self, query: str) -> str:
        """Full hh.ru search URL: filters from the YAML template + the resume's query."""
        return build_search_url(self._search_url_template, (query or "").strip())

    @property
    def search_url(self) -> str:
        """Fallback URL when no resume query is available (kept for display)."""
        return build_search_url(self._search_url_template, self._legacy_query)

    def scroller_config(self, query: Optional[str] = None) -> ScrollerConfig:
        base = self.config.scroller
        return replace(
            base,
            search_url=self.search_url_for(query) if query is not None else self.search_url,
            load_mode=self.get("scroller.load_mode", base.load_mode),
            max_pages=int(self.get("scroller.max_pages", base.max_pages)),
            max_scroll_steps_per_page=int(
                self.get("scroller.max_scroll_steps_per_page", base.max_scroll_steps_per_page)
            ),
            max_scroll_time_sec_per_page=float(
                self.get("scroller.max_scroll_time_sec_per_page", base.max_scroll_time_sec_per_page)
            ),
            stable_cycles=int(self.get("scroller.stable_cycles", base.stable_cycles)),
            scroll_step_min=int(self.get("scroller.scroll_step_min", base.scroll_step_min)),
            scroll_step_max=max(
                int(self.get("scroller.scroll_step_max", base.scroll_step_max)),
                int(self.get("scroller.scroll_step_min", base.scroll_step_min)),
            ),
            scroll_pause_min_sec=float(self.get("scroller.scroll_pause_min_sec", base.scroll_pause_min_sec)),
            scroll_pause_max_sec=float(self.get("scroller.scroll_pause_max_sec", base.scroll_pause_max_sec)),
        )

    def profile_config(self) -> ProfileConfig:
        return ProfileConfig(
            # Empty means "whatever the scoring model is".
            model=str(self.get("profile.model", "")).strip() or str(self.get("llm.model", "")),
            prompt=str(self.get("profile.prompt", "")),
            max_chars=int(self.get("profile.max_chars", 6000)),
            max_tokens=int(self.get("profile.max_tokens", 4000)),
            timeout_sec=float(self.get("profile.timeout_sec", 180.0)),
        )

    def cover_letter_config(self) -> CoverLetterConfig:
        return CoverLetterConfig(
            enabled=bool(self.get("cover_letter.enabled", True)),
            model=str(self.get("cover_letter.model", "")).strip(),
            when=str(self.get("cover_letter.when", "required")),
            max_chars=int(self.get("cover_letter.max_chars", 1200)),
            prompt=str(self.get("cover_letter.prompt", "")),
            fallback_text=str(self.get("cover_letter.fallback_text", "")),
        )

    def activity_config(self, query: Optional[str] = None) -> ActivityConfig:
        return ActivityConfig(
            # Empty means "browse whatever this resume is searching for".
            url=str(self.get("activity.url", "")).strip()
            or (self.search_url_for(query) if query else self.search_url),
            duration_min=float(self.get("activity.duration_min", 10.0)),
            scroll_step_min=int(self.get("activity.scroll_step_min", 250)),
            scroll_step_max=int(self.get("activity.scroll_step_max", 700)),
            pause_min_sec=float(self.get("activity.pause_min_sec", 1.5)),
            pause_max_sec=max(
                float(self.get("activity.pause_min_sec", 1.5)),
                float(self.get("activity.pause_max_sec", 5.0)),
            ),
            scroll_up_chance=float(self.get("activity.scroll_up_chance", 0.2)),
            open_vacancies=bool(self.get("activity.open_vacancies", False)),
            open_vacancy_chance=float(self.get("activity.open_vacancy_chance", 0.15)),
            read_seconds_min=float(self.get("activity.read_seconds_min", 5.0)),
            read_seconds_max=max(
                float(self.get("activity.read_seconds_min", 5.0)),
                float(self.get("activity.read_seconds_max", 20.0)),
            ),
        )

    def ratelimit_config(self) -> RateLimitConfig:
        return RateLimitConfig(
            enabled=bool(self.get("ratelimit.enabled", True)),
            min_interval_sec=float(self.get("ratelimit.min_interval_sec", 1.0)),
            jitter_sec=float(self.get("ratelimit.jitter_sec", 0.5)),
            requests_per_minute=int(self.get("ratelimit.requests_per_minute", 100)),
            throttle_browser_requests=bool(self.get("ratelimit.throttle_browser_requests", True)),
        )

    def llm_config(self) -> LLMConfig:
        base = self.config.llm
        modifiers = replace(
            base.modifiers,
            thinking=self.get("llm.modifiers.thinking", base.modifiers.thinking),
            search=bool(self.get("llm.modifiers.search", base.modifiers.search)),
        )
        return replace(
            base,
            enabled=bool(self.get("llm.enabled", base.enabled)),
            standard=self.get("llm.standard", base.standard),
            base_url=self.get("llm.base_url", base.base_url),
            api_key=self.get("llm.api_key", base.api_key),
            model=self.get("llm.model", base.model),
            temperature=float(self.get("llm.temperature", base.temperature)),
            max_tokens=int(self.get("llm.max_tokens", base.max_tokens)),
            timeout_sec=float(self.get("llm.timeout_sec", base.timeout_sec)),
            modifiers=modifiers,
        )


def _coerce(setting: SettingField, raw: Any, strict: bool = False) -> Any:
    """Turn a stored/submitted string into the field's declared type."""
    if setting.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes")

    text = str(raw).strip()
    if setting.type in ("str", "text", "secret", "model"):
        return text

    if setting.type == "choice":
        if text not in setting.choices:
            if strict:
                raise ValueError(f"допустимые значения: {', '.join(c or '(пусто)' for c in setting.choices)}")
            return setting.choices[0] if setting.choices else text
        return text

    try:
        number = int(text) if setting.type == "int" else float(text)
    except ValueError as e:
        if strict:
            raise ValueError("ожидается число") from e
        return None  # corrupted stored value -> caller falls back to the default

    if setting.minimum is not None and number < setting.minimum:
        if strict:
            raise ValueError(f"минимум {setting.minimum:g}")
        number = type(number)(setting.minimum)
    if setting.maximum is not None and number > setting.maximum:
        if strict:
            raise ValueError(f"максимум {setting.maximum:g}")
        number = type(number)(setting.maximum)
    return number


def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
