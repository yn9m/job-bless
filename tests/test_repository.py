import json

import pytest
import aiosqlite
from datetime import datetime, timezone

from src.db.connection import init_sqlite
from src.db.repository import DatabaseRepository
from src.db.models import SearchRun, SearchRunStatus, PageCommitParams, VacancyCard


@pytest.mark.asyncio
async def test_sqlite_repository_flow():
    conn = await init_sqlite(":memory:")
    repo = DatabaseRepository(conn, driver="sqlite")

    run = SearchRun(
        id="test_run_1",
        task_id="test_task_1",
        search_url="https://hh.ru/search/vacancy?text=Python",
        status=SearchRunStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    await repo.create_search_run(run)

    card = VacancyCard(
        source="hh",
        external_id="12345",
        url="https://hh.ru/vacancy/12345",
        title="Python Developer",
        company_name="Tech Corp",
        salary_text="150 000 руб.",
    )

    params = PageCommitParams(
        search_run_id="test_run_1",
        page_key="page_1",
        page_number=1,
        current_url="https://hh.ru/search/vacancy?text=Python",
        canonical_url="https://hh.ru/search/vacancy?text=Python",
        cards=[card],
    )
    await repo.commit_page_transaction(params)

    # Verify database insertion
    async with conn.execute("SELECT count(*) FROM search_runs;") as cur:
        assert (await cur.fetchone())[0] == 1

    async with conn.execute("SELECT count(*) FROM vacancies;") as cur:
        assert (await cur.fetchone())[0] == 1

    async with conn.execute("SELECT count(*) FROM vacancy_discoveries;") as cur:
        assert (await cur.fetchone())[0] == 1

    await repo.update_search_run_status("test_run_1", SearchRunStatus.COMPLETED, reason="test_done")
    
    async with conn.execute("SELECT status, completion_reason FROM search_runs WHERE id='test_run_1';") as cur:
        row = await cur.fetchone()
        assert row[0] == "completed"
        assert row[1] == "test_done"

    await conn.close()


@pytest.mark.asyncio
async def test_resume_upsert_is_committed(tmp_path):
    """A resume must survive a reconnect on its own.

    upsert_resume reads back the id via RETURNING; without an explicit commit
    SQLite kept the write in an open transaction and it was silently lost
    unless some later statement happened to commit.
    """
    from src.db.models import Resume

    db_path = str(tmp_path / "resumes.db")
    conn = await init_sqlite(db_path)
    repo = DatabaseRepository(conn, driver="sqlite")

    resume_id = await repo.upsert_resume(
        Resume(
            source_url="https://hh.ru/resume/abc",
            title="Инженер",
            skills=["Python", "Docker"],
            education_text="Университет\n2020 · Бакалавр",
            certificates=["ISTQB (2022)"],
            summary="О себе",
            experience_text="Опыт",
            raw_text="полный текст",
        )
    )
    assert resume_id
    await conn.close()

    reopened = await init_sqlite(db_path)
    repo = DatabaseRepository(reopened, driver="sqlite")
    stored = await repo.get_resume(resume_id)

    assert stored is not None
    assert stored.skills == ["Python", "Docker"]
    assert stored.certificates == ["ISTQB (2022)"]
    assert "Бакалавр" in stored.education_text
    assert stored.summary == "О себе"
    await reopened.close()


@pytest.mark.asyncio
async def test_missing_resume_columns_are_added_on_startup(tmp_path):
    """Databases created before the education/certificates columns must migrate."""
    db_path = str(tmp_path / "legacy.db")

    async with aiosqlite.connect(db_path) as legacy:
        await legacy.execute(
            """
            CREATE TABLE resumes (
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
                raw_text TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                CONSTRAINT unique_resume_source_url UNIQUE (source_url)
            );
            """
        )
        await legacy.execute(
            "INSERT INTO resumes (source_url, title, raw_text) VALUES (?, ?, ?);",
            ("https://hh.ru/resume/old", "Инженер", "Навыки\nGo\nDocker\n"),
        )
        await legacy.commit()

    conn = await init_sqlite(db_path)  # runs the migration
    async with conn.execute("PRAGMA table_info(resumes);") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    assert {"education_text", "certificates_json"} <= columns

    repo = DatabaseRepository(conn, driver="sqlite")
    assert (await repo.list_resumes())[0].title == "Инженер"  # data survived
    await conn.close()


@pytest.mark.asyncio
async def test_backfill_recovers_sections_from_stored_text(tmp_path):
    from src.db.models import Resume
    from src.resume.service import ResumeService

    conn = await init_sqlite(str(tmp_path / "backfill.db"))
    repo = DatabaseRepository(conn, driver="sqlite")
    await repo.upsert_resume(
        Resume(
            source_url="https://hh.ru/resume/xyz",
            title="Инженер",
            raw_text="Навыки\nGo\nDocker\nОбразование\nУниверситет\n2020 · Бакалавр\nО себе\nЛюблю Go\n",
        )
    )

    updated = await ResumeService(repo, settings=None).backfill_sections()
    assert updated == 1

    stored = (await repo.list_resumes())[0]
    assert stored.skills == ["Go", "Docker"]
    assert "Университет" in stored.education_text
    assert "Люблю Go" in stored.summary

    # Idempotent: a second pass changes nothing.
    assert await ResumeService(repo, settings=None).backfill_sections() == 0
    await conn.close()
