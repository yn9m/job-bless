import os
import logging
from pathlib import Path
import aiosqlite
import asyncpg

from src.config import DatabaseConfig

logger = logging.getLogger(__name__)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    search_url TEXT NOT NULL,
    browser_session_id TEXT NOT NULL DEFAULT '',
    transport TEXT NOT NULL DEFAULT 'playwright',
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    completed_at TEXT,
    total_pages_processed INTEGER NOT NULL DEFAULT 0,
    total_cards_found INTEGER NOT NULL DEFAULT 0,
    unique_vacancies_count INTEGER NOT NULL DEFAULT 0,
    duplicate_cards_count INTEGER NOT NULL DEFAULT 0,
    invalid_cards_count INTEGER NOT NULL DEFAULT 0,
    last_processed_url TEXT NOT NULL DEFAULT '',
    completion_reason TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    collector_version TEXT NOT NULL DEFAULT '0.2.0',
    resume_id INTEGER
);

CREATE TABLE IF NOT EXISTS search_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    page_key TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    current_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'receiving',
    cards_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    completed_at TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    CONSTRAINT unique_run_page_key UNIQUE (search_run_id, page_key)
);

CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'hh',
    external_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    company_id TEXT NOT NULL DEFAULT '',
    company_url TEXT NOT NULL DEFAULT '',
    salary_text TEXT NOT NULL DEFAULT '',
    salary_from INTEGER,
    salary_to INTEGER,
    currency TEXT NOT NULL DEFAULT '',
    gross INTEGER NOT NULL DEFAULT 0,
    city TEXT NOT NULL DEFAULT '',
    work_format TEXT NOT NULL DEFAULT '',
    schedule TEXT NOT NULL DEFAULT '',
    experience TEXT NOT NULL DEFAULT '',
    employment_type TEXT NOT NULL DEFAULT '',
    first_discovered_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    last_discovered_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    raw_json TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT unique_source_external_id UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS vacancy_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    search_page_id INTEGER NOT NULL REFERENCES search_pages(id) ON DELETE CASCADE,
    vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    position_on_page INTEGER NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    raw_json TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT unique_run_vacancy UNIQUE (search_run_id, vacancy_id)
);

CREATE TABLE IF NOT EXISTS vacancy_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id INTEGER REFERENCES vacancies(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    vacancy_url TEXT NOT NULL,
    status TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    response_text TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    cover_letter TEXT NOT NULL DEFAULT '',
    CONSTRAINT unique_vacancy_app UNIQUE (external_id)
);

-- Settings edited from the web UI. YAML stays the source of defaults.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);

CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    experience_text TEXT NOT NULL DEFAULT '',
    salary_text TEXT NOT NULL DEFAULT '',
    skills_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    education_text TEXT NOT NULL DEFAULT '',
    certificates_json TEXT NOT NULL DEFAULT '[]',
    raw_text TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    search_query TEXT NOT NULL DEFAULT '',
    context_text TEXT NOT NULL DEFAULT '',
    profile_summary TEXT NOT NULL DEFAULT '',
    profile_model TEXT NOT NULL DEFAULT '',
    profile_hash TEXT NOT NULL DEFAULT '',
    profile_updated_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    CONSTRAINT unique_resume_source_url UNIQUE (source_url)
);

CREATE TABLE IF NOT EXISTS vacancy_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
    resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    score INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT '',
    matched_skills_json TEXT NOT NULL DEFAULT '[]',
    missing_skills_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    scored_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    CONSTRAINT unique_vacancy_resume_score UNIQUE (vacancy_id, resume_id)
);

-- History of jobs triggered from the web UI or the scheduler.
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    trigger TEXT NOT NULL DEFAULT 'manual',
    params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_search_runs_status ON search_runs(status);
CREATE INDEX IF NOT EXISTS idx_search_pages_run_id ON search_pages(search_run_id);
CREATE INDEX IF NOT EXISTS idx_vacancies_external_id ON vacancies(source, external_id);
CREATE INDEX IF NOT EXISTS idx_discoveries_run_id ON vacancy_discoveries(search_run_id);
CREATE INDEX IF NOT EXISTS idx_applications_ext_id ON vacancy_applications(external_id);
CREATE INDEX IF NOT EXISTS idx_scores_vacancy ON vacancy_scores(vacancy_id);
CREATE INDEX IF NOT EXISTS idx_scores_score ON vacancy_scores(score);
CREATE INDEX IF NOT EXISTS idx_resumes_active ON resumes(is_active);
CREATE INDEX IF NOT EXISTS idx_task_runs_kind ON task_runs(kind, started_at);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_runs (
    id VARCHAR(64) PRIMARY KEY,
    task_id VARCHAR(64) NOT NULL,
    search_url TEXT NOT NULL,
    browser_session_id VARCHAR(255) NOT NULL DEFAULT '',
    transport VARCHAR(32) NOT NULL DEFAULT 'playwright',
    status VARCHAR(64) NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    total_pages_processed INT NOT NULL DEFAULT 0,
    total_cards_found INT NOT NULL DEFAULT 0,
    unique_vacancies_count INT NOT NULL DEFAULT 0,
    duplicate_cards_count INT NOT NULL DEFAULT 0,
    invalid_cards_count INT NOT NULL DEFAULT 0,
    last_processed_url TEXT NOT NULL DEFAULT '',
    completion_reason VARCHAR(128) NOT NULL DEFAULT '',
    error_code VARCHAR(128) NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    collector_version VARCHAR(32) NOT NULL DEFAULT '0.2.0',
    resume_id BIGINT
);

CREATE TABLE IF NOT EXISTS search_pages (
    id BIGSERIAL PRIMARY KEY,
    search_run_id VARCHAR(64) NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    page_key VARCHAR(128) NOT NULL,
    page_number INT NOT NULL,
    current_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'receiving',
    cards_count INT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error_message TEXT NOT NULL DEFAULT '',
    CONSTRAINT unique_run_page_key UNIQUE (search_run_id, page_key)
);

CREATE TABLE IF NOT EXISTS vacancies (
    id BIGSERIAL PRIMARY KEY,
    source VARCHAR(32) NOT NULL DEFAULT 'hh',
    external_id VARCHAR(128) NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    company_id VARCHAR(128) NOT NULL DEFAULT '',
    company_url TEXT NOT NULL DEFAULT '',
    salary_text TEXT NOT NULL DEFAULT '',
    salary_from BIGINT,
    salary_to BIGINT,
    currency VARCHAR(16) NOT NULL DEFAULT '',
    gross BOOLEAN NOT NULL DEFAULT FALSE,
    city TEXT NOT NULL DEFAULT '',
    work_format TEXT NOT NULL DEFAULT '',
    schedule TEXT NOT NULL DEFAULT '',
    experience TEXT NOT NULL DEFAULT '',
    employment_type TEXT NOT NULL DEFAULT '',
    first_discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT unique_source_external_id UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS vacancy_discoveries (
    id BIGSERIAL PRIMARY KEY,
    search_run_id VARCHAR(64) NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    search_page_id BIGINT NOT NULL REFERENCES search_pages(id) ON DELETE CASCADE,
    vacancy_id BIGINT NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
    page_number INT NOT NULL,
    position_on_page INT NOT NULL,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT unique_run_vacancy UNIQUE (search_run_id, vacancy_id)
);

CREATE TABLE IF NOT EXISTS vacancy_applications (
    id BIGSERIAL PRIMARY KEY,
    vacancy_id BIGINT REFERENCES vacancies(id) ON DELETE CASCADE,
    external_id VARCHAR(128) NOT NULL,
    vacancy_url TEXT NOT NULL,
    status VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    response_text TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    cover_letter TEXT NOT NULL DEFAULT '',
    CONSTRAINT unique_vacancy_app UNIQUE (external_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key VARCHAR(128) PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at VARCHAR(40) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS resumes (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL DEFAULT '',
    external_id VARCHAR(128) NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    experience_text TEXT NOT NULL DEFAULT '',
    salary_text TEXT NOT NULL DEFAULT '',
    skills_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    education_text TEXT NOT NULL DEFAULT '',
    certificates_json TEXT NOT NULL DEFAULT '[]',
    raw_text TEXT NOT NULL DEFAULT '',
    is_active INT NOT NULL DEFAULT 0,
    search_query TEXT NOT NULL DEFAULT '',
    context_text TEXT NOT NULL DEFAULT '',
    profile_summary TEXT NOT NULL DEFAULT '',
    profile_model VARCHAR(128) NOT NULL DEFAULT '',
    profile_hash VARCHAR(64) NOT NULL DEFAULT '',
    profile_updated_at VARCHAR(40) NOT NULL DEFAULT '',
    imported_at VARCHAR(40) NOT NULL DEFAULT '',
    updated_at VARCHAR(40) NOT NULL DEFAULT '',
    CONSTRAINT unique_resume_source_url UNIQUE (source_url)
);

CREATE TABLE IF NOT EXISTS vacancy_scores (
    id BIGSERIAL PRIMARY KEY,
    vacancy_id BIGINT NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
    resume_id BIGINT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    score INT NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT '',
    matched_skills_json TEXT NOT NULL DEFAULT '[]',
    missing_skills_json TEXT NOT NULL DEFAULT '[]',
    model VARCHAR(128) NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    scored_at VARCHAR(40) NOT NULL DEFAULT '',
    CONSTRAINT unique_vacancy_resume_score UNIQUE (vacancy_id, resume_id)
);

CREATE TABLE IF NOT EXISTS task_runs (
    id VARCHAR(64) PRIMARY KEY,
    kind VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    trigger VARCHAR(32) NOT NULL DEFAULT 'manual',
    params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    started_at VARCHAR(40) NOT NULL DEFAULT '',
    finished_at VARCHAR(40)
);

CREATE INDEX IF NOT EXISTS idx_search_runs_status ON search_runs(status);
CREATE INDEX IF NOT EXISTS idx_search_pages_run_id ON search_pages(search_run_id);
CREATE INDEX IF NOT EXISTS idx_vacancies_external_id ON vacancies(source, external_id);
CREATE INDEX IF NOT EXISTS idx_discoveries_run_id ON vacancy_discoveries(search_run_id);
CREATE INDEX IF NOT EXISTS idx_applications_ext_id ON vacancy_applications(external_id);
CREATE INDEX IF NOT EXISTS idx_scores_vacancy ON vacancy_scores(vacancy_id);
CREATE INDEX IF NOT EXISTS idx_scores_score ON vacancy_scores(score);
CREATE INDEX IF NOT EXISTS idx_resumes_active ON resumes(is_active);
CREATE INDEX IF NOT EXISTS idx_task_runs_kind ON task_runs(kind, started_at);
"""


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` never
# touches an existing table, so they are applied separately and idempotently.
ADDED_COLUMNS = (
    ("resumes", "education_text", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "certificates_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("vacancy_applications", "cover_letter", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "search_query", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "context_text", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "profile_summary", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "profile_model", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "profile_hash", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "profile_updated_at", "TEXT NOT NULL DEFAULT ''"),
    # Which resume this search run was made for.
    ("search_runs", "resume_id", "INTEGER"),
)


async def _migrate_sqlite(conn: aiosqlite.Connection) -> None:
    for table, column, ddl in ADDED_COLUMNS:
        async with conn.execute(f"PRAGMA table_info({table});") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        if column not in existing:
            logger.info(f"Migrating: adding column {table}.{column}")
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")
    await conn.commit()


async def _migrate_postgres(conn) -> None:
    for table, column, ddl in ADDED_COLUMNS:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl};")


async def init_sqlite(db_path: str) -> aiosqlite.Connection:
    if db_path != ":memory:":
        parent_dir = Path(db_path).parent
        parent_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Connecting to SQLite database at {db_path}...")
    conn = await aiosqlite.connect(db_path)
    await conn.executescript(SQLITE_SCHEMA)
    await conn.commit()
    await _migrate_sqlite(conn)
    logger.info("SQLite database schema initialized successfully.")
    return conn


async def init_postgres(config: DatabaseConfig) -> asyncpg.Pool:
    logger.info(f"Connecting to PostgreSQL pool at {config.host}:{config.port}/{config.dbname}...")
    pool = await asyncpg.create_pool(
        host=config.host,
        port=config.port,
        user=config.username,
        password=config.password,
        database=config.dbname,
    )
    async with pool.acquire() as conn:
        await conn.execute(POSTGRES_SCHEMA)
        await _migrate_postgres(conn)
    logger.info("PostgreSQL database pool & schema initialized successfully.")
    return pool
