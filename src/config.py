import os
import yaml
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class AppConfig:
    is_production: bool = False
    address: str = ":8080"


@dataclass
class LogConfig:
    level: str = "info"
    encoding: str = "console"


@dataclass
class LocalProcessConfig:
    command: str = "chrome"
    args: List[str] = field(default_factory=lambda: [
        "--remote-debugging-port=9222",
        "--user-data-dir=./data/browser-profile",
        "--new-window",
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
        "https://hh.ru"
    ])


@dataclass
class CDPConfig:
    endpoint: str = "http://localhost:9222"
    timeout_ms: int = 30000


@dataclass
class PlaywrightConfig:
    endpoint: str = "ws://localhost:3000"
    browser_type: str = "chromium"
    timeout_ms: int = 30000


@dataclass
class BrowserConfig:
    provider: str = "local_process"  # local_process | docker | external
    transport: str = "cdp"           # cdp | playwright
    headless: bool = False
    # Close tabs left over from previous runs before starting a new one.
    close_stale_tabs: bool = True
    local_process: LocalProcessConfig = field(default_factory=LocalProcessConfig)
    cdp: CDPConfig = field(default_factory=CDPConfig)
    playwright: PlaywrightConfig = field(default_factory=PlaywrightConfig)


@dataclass
class DatabaseConfig:
    driver: str = "sqlite"            # sqlite | postgres
    sqlite_path: str = "./data/career_agent.db"
    host: str = "localhost"
    port: str = "5432"
    username: str = "postgres"
    password: str = "password"
    dbname: str = "job_bless_db"
    sslmode: str = "disable"

    def get_dsn(self) -> str:
        if self.driver == "sqlite":
            return self.sqlite_path
        return f"postgres://{self.username}:{self.password}@{self.host}:{self.port}/{self.dbname}?sslmode={self.sslmode}"


@dataclass
class ScrollerConfig:
    search_url: str = "https://hh.ru/search/vacancy?text=Python"
    # instant — wait for the list and parse it (hh.ru renders it all at once);
    # scroll — the old behaviour, kept for pages that load on scroll.
    load_mode: str = "instant"
    # Used in scroll mode only. Collecting is not imitation: bigger, faster steps.
    scroll_step_min: int = 600
    scroll_step_max: int = 1200
    scroll_pause_min_sec: float = 0.1
    scroll_pause_max_sec: float = 0.25
    page_timeout_sec: float = 30.0
    max_scroll_steps_per_page: int = 200
    max_scroll_time_sec_per_page: float = 120.0
    max_pages: int = 10
    stable_cycles: int = 3


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    # Empty token = no auth (the default for a local run on 127.0.0.1).
    token: str = ""
    reload: bool = False


@dataclass
class LLMModifiersConfig:
    """AI Studio bridge modifiers encoded as model-name suffixes."""

    thinking: str = ""      # minimal | low | medium | high
    stream_mode: str = ""   # real | fake
    search: bool = False    # force Grounding with Google Search
    code: bool = False      # force Code Execution


@dataclass
class LLMConfig:
    enabled: bool = False
    standard: str = "openai"          # openai | gemini | anthropic
    base_url: str = "http://localhost:7860"
    api_key: str = ""
    model: str = "gemini-2.5-flash-lite"
    embedding_model: str = "gemini-embedding-001"
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout_sec: float = 120.0
    max_retries: int = 3
    retry_backoff_sec: float = 1.0
    gemini_api_version: str = "v1beta"     # used by standard=gemini
    anthropic_version: str = "2023-06-01"  # used by standard=anthropic
    extra_headers: Dict[str, str] = field(default_factory=dict)
    modifiers: LLMModifiersConfig = field(default_factory=LLMModifiersConfig)


@dataclass
class ApplierConfig:
    resume_path: str = "./data/resume.txt"
    min_llm_score: int = 7


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    log: LogConfig = field(default_factory=LogConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    scroller: ScrollerConfig = field(default_factory=ScrollerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    web: WebConfig = field(default_factory=WebConfig)
    applier: ApplierConfig = field(default_factory=ApplierConfig)

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        path = config_path or os.getenv("CONFIG_PATH", "configs/config.local.yaml")
        if not os.path.exists(path):
            path = "configs/config.yaml"

        data: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        # Parse sections
        app_data = data.get("app", {})
        app_cfg = AppConfig(
            is_production=app_data.get("isProduction", False),
            address=app_data.get("address", ":8080"),
        )

        log_data = data.get("log", {})
        log_cfg = LogConfig(
            level=log_data.get("level", "info"),
            encoding=log_data.get("encoding", "console"),
        )

        b_data = data.get("browser", {})
        lp_data = b_data.get("local_process", {})
        cdp_data = b_data.get("cdp", {})
        pw_data = b_data.get("playwright", {})

        browser_cfg = BrowserConfig(
            provider=os.getenv("BROWSER_PROVIDER", b_data.get("provider", "local_process")),
            transport=b_data.get("transport", "cdp"),
            headless=(os.getenv("BROWSER_HEADLESS", str(b_data.get("headless", False))).lower() in ("true", "1")),
            close_stale_tabs=bool(b_data.get("close_stale_tabs", True)),
            local_process=LocalProcessConfig(
                command=lp_data.get("command", "chrome"),
                args=lp_data.get("args", LocalProcessConfig().args),
            ),
            cdp=CDPConfig(
                endpoint=cdp_data.get("endpoint", "http://localhost:9222"),
                timeout_ms=cdp_data.get("timeout_ms", 30000),
            ),
            playwright=PlaywrightConfig(
                endpoint=pw_data.get("endpoint", "ws://localhost:3000"),
                browser_type=pw_data.get("browser_type", "chromium"),
                timeout_ms=pw_data.get("timeout_ms", 30000),
            ),
        )

        db_data = data.get("postgres", {})
        db_cfg = DatabaseConfig(
            driver=os.getenv("DB_DRIVER", db_data.get("driver", "sqlite")),
            sqlite_path=os.getenv("SQLITE_PATH", db_data.get("sqlite_path", "./data/career_agent.db")),
            host=os.getenv("POSTGRES_HOST", db_data.get("host", "localhost")),
            port=os.getenv("POSTGRES_PORT", str(db_data.get("port", "5432"))),
            username=os.getenv("POSTGRES_USER", db_data.get("username", "postgres")),
            password=os.getenv("POSTGRES_PASSWORD", db_data.get("password", "password")),
            dbname=os.getenv("POSTGRES_DB", db_data.get("dbname", "job_bless_db")),
            sslmode=os.getenv("POSTGRES_SSLMODE", db_data.get("sslmode", "disable")),
        )

        s_data = data.get("hh_autoscroller", {})
        scroller_cfg = ScrollerConfig(
            search_url=s_data.get("search_url", "https://hh.ru/search/vacancy?text=Python"),
            load_mode=s_data.get("load_mode", "instant"),
            scroll_step_min=s_data.get("scroll_step_min", 600),
            scroll_step_max=s_data.get("scroll_step_max", 1200),
            scroll_pause_min_sec=float(s_data.get("scroll_pause_min_sec", 0.1)),
            scroll_pause_max_sec=float(s_data.get("scroll_pause_max_sec", 0.25)),
            max_scroll_steps_per_page=s_data.get("max_scroll_steps_per_page", 200),
            max_scroll_time_sec_per_page=float(s_data.get("max_scroll_time_sec_per_page", 120.0)),
            max_pages=s_data.get("max_pages", 10),
            stable_cycles=s_data.get("stable_cycles", 3),
        )

        llm_data = data.get("llm", {})
        mod_data = llm_data.get("modifiers", {})
        llm_cfg = LLMConfig(
            enabled=(os.getenv("LLM_ENABLED", str(llm_data.get("enabled", False))).lower() in ("true", "1")),
            standard=os.getenv("LLM_STANDARD", llm_data.get("standard", "openai")),
            base_url=os.getenv("LLM_BASE_URL", llm_data.get("base_url", "http://localhost:7860")),
            api_key=os.getenv("LLM_API_KEY", llm_data.get("api_key", "")),
            model=os.getenv("LLM_MODEL", llm_data.get("model", "gemini-2.5-flash-lite")),
            embedding_model=os.getenv("LLM_EMBEDDING_MODEL", llm_data.get("embedding_model", "gemini-embedding-001")),
            temperature=float(llm_data.get("temperature", 0.7)),
            max_tokens=int(llm_data.get("max_tokens", 2048)),
            timeout_sec=float(llm_data.get("timeout_sec", 120.0)),
            max_retries=int(llm_data.get("max_retries", 3)),
            retry_backoff_sec=float(llm_data.get("retry_backoff_sec", 1.0)),
            gemini_api_version=llm_data.get("gemini_api_version", "v1beta"),
            anthropic_version=llm_data.get("anthropic_version", "2023-06-01"),
            extra_headers=dict(llm_data.get("extra_headers", {}) or {}),
            modifiers=LLMModifiersConfig(
                thinking=mod_data.get("thinking", ""),
                stream_mode=mod_data.get("stream_mode", ""),
                search=bool(mod_data.get("search", False)),
                code=bool(mod_data.get("code", False)),
            ),
        )

        web_data = data.get("web", {})
        web_cfg = WebConfig(
            host=os.getenv("WEB_HOST", web_data.get("host", "127.0.0.1")),
            port=int(os.getenv("WEB_PORT", web_data.get("port", 8080))),
            token=os.getenv("WEB_TOKEN", web_data.get("token", "")),
            reload=bool(web_data.get("reload", False)),
        )

        applier_data = data.get("applier", {})
        applier_cfg = ApplierConfig(
            resume_path=applier_data.get("resume_path", "./data/resume.txt"),
            min_llm_score=int(applier_data.get("min_llm_score", 7)),
        )

        return cls(
            app=app_cfg,
            log=log_cfg,
            browser=browser_cfg,
            db=db_cfg,
            scroller=scroller_cfg,
            llm=llm_cfg,
            applier=applier_cfg,
            web=web_cfg,
        )
