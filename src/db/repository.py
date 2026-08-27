import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union
import aiosqlite
import asyncpg

from src.db.models import (
    SearchRun,
    SearchRunStatus,
    PageCommitParams,
    VacancyCard,
    VacancyApplication,
    ApplicationStatus,
    Resume,
    VacancyScore,
    TaskRun,
    TaskKind,
    TaskStatus,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatabaseRepository:
    def __init__(self, connection: Union[aiosqlite.Connection, asyncpg.Pool], driver: str = "sqlite"):
        self.connection = connection
        self.driver = driver.lower()

    # ------------------------------------------------------------------
    # Driver-neutral query helpers.
    # Queries are written with `?` placeholders and rewritten to `$n` for
    # PostgreSQL, so new code does not need a branch per driver.
    # ------------------------------------------------------------------

    def _sql(self, query: str) -> str:
        if self.driver == "sqlite":
            return query
        counter = iter(range(1, 1000))
        return re.sub(r"\?", lambda _: f"${next(counter)}", query)

    async def _execute(self, query: str, params: Sequence[Any] = ()) -> None:
        if self.driver == "sqlite":
            await self.connection.execute(query, tuple(params))
            await self.connection.commit()
            return
        async with self.connection.acquire() as conn:
            await conn.execute(self._sql(query), *params)

    async def _fetch_all(self, query: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        if self.driver == "sqlite":
            async with self.connection.execute(query, tuple(params)) as cursor:
                columns = [c[0] for c in cursor.description]
                rows = await cursor.fetchall()
                return [dict(zip(columns, row)) for row in rows]
        async with self.connection.acquire() as conn:
            rows = await conn.fetch(self._sql(query), *params)
            return [dict(row) for row in rows]

    async def _fetch_one(self, query: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = await self._fetch_all(query, params)
        return rows[0] if rows else None

    async def _fetch_val(self, query: str, params: Sequence[Any] = ()) -> Any:
        row = await self._fetch_one(query, params)
        return next(iter(row.values())) if row else None

    async def _write_returning(self, query: str, params: Sequence[Any] = ()) -> Any:
        """INSERT/UPDATE ... RETURNING: like _fetch_val, but commits.

        Reading through the fetch path leaves the transaction open on SQLite, so
        the write is lost unless some later statement happens to commit it.
        """
        value = await self._fetch_val(query, params)
        if self.driver == "sqlite":
            await self.connection.commit()
        return value

    async def create_search_run(self, run: SearchRun) -> None:
        started_str = run.started_at.isoformat() if isinstance(run.started_at, datetime) else str(run.started_at)
        
        if self.driver == "sqlite":
            query = """
                INSERT INTO search_runs (
                    id, task_id, search_url, browser_session_id, transport, status,
                    started_at, collector_version, resume_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status,
                    started_at = excluded.started_at;
            """
            await self.connection.execute(
                query,
                (run.id, run.task_id, run.search_url, run.browser_session_id, run.transport,
                 str(run.status.value), started_str, run.collector_version, run.resume_id)
            )
            await self.connection.commit()
        else:
            query = """
                INSERT INTO search_runs (
                    id, task_id, search_url, browser_session_id, transport, status,
                    started_at, collector_version, resume_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    started_at = EXCLUDED.started_at;
            """
            async with self.connection.acquire() as conn:
                await conn.execute(
                    query,
                    run.id, run.task_id, run.search_url, run.browser_session_id, run.transport,
                    str(run.status.value), run.started_at, run.collector_version, run.resume_id
                )

    async def update_search_run_status(
        self,
        run_id: str,
        status: SearchRunStatus,
        reason: str = "",
        error_code: str = "",
        error_message: str = ""
    ) -> None:
        completed_at = None
        if status in (SearchRunStatus.COMPLETED, SearchRunStatus.FAILED, SearchRunStatus.CANCELLED, SearchRunStatus.MANUAL_ACTION_REQUIRED):
            completed_at = datetime.now(timezone.utc)

        if self.driver == "sqlite":
            completed_str = completed_at.isoformat() if completed_at else None
            query = """
                UPDATE search_runs SET
                    status = ?,
                    completion_reason = CASE WHEN ? != '' THEN ? ELSE completion_reason END,
                    error_code = CASE WHEN ? != '' THEN ? ELSE error_code END,
                    error_message = CASE WHEN ? != '' THEN ? ELSE error_message END,
                    completed_at = COALESCE(?, completed_at)
                WHERE id = ?;
            """
            await self.connection.execute(
                query,
                (str(status.value), reason, reason, error_code, error_code, error_message, error_message, completed_str, run_id)
            )
            await self.connection.commit()
        else:
            query = """
                UPDATE search_runs SET
                    status = $1,
                    completion_reason = COALESCE(NULLIF($2, ''), completion_reason),
                    error_code = COALESCE(NULLIF($3, ''), error_code),
                    error_message = COALESCE(NULLIF($4, ''), error_message),
                    completed_at = COALESCE($5, completed_at)
                WHERE id = $6;
            """
            async with self.connection.acquire() as conn:
                await conn.execute(query, str(status.value), reason, error_code, error_message, completed_at, run_id)

    async def commit_page_transaction(self, params: PageCommitParams) -> None:
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self.driver == "sqlite":
            await self._commit_page_sqlite(params, now_str)
        else:
            await self._commit_page_postgres(params, now_dt)

    async def _commit_page_sqlite(self, params: PageCommitParams, now_str: str) -> None:
        # 1. Insert search_page
        page_query = """
            INSERT INTO search_pages (
                search_run_id, page_key, page_number, current_url, canonical_url,
                status, cards_count, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (search_run_id, page_key) DO UPDATE SET
                status = excluded.status,
                cards_count = excluded.cards_count,
                completed_at = excluded.completed_at
            RETURNING id;
        """
        async with self.connection.execute(
            page_query,
            (params.search_run_id, params.page_key, params.page_number, params.current_url, params.canonical_url, "completed", len(params.cards), now_str, now_str)
        ) as cursor:
            row = await cursor.fetchone()
            page_id = row[0]

        # 2. Upsert vacancies & insert discoveries
        upsert_vacancy = """
            INSERT INTO vacancies (
                source, external_id, canonical_url, title, company_name, company_id, company_url,
                salary_text, salary_from, salary_to, currency, gross, city, work_format, schedule,
                experience, employment_type, first_discovered_at, last_discovered_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, external_id) DO UPDATE SET
                last_discovered_at = excluded.last_discovered_at,
                canonical_url = CASE WHEN excluded.canonical_url != '' THEN excluded.canonical_url ELSE vacancies.canonical_url END,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE vacancies.title END,
                company_name = CASE WHEN excluded.company_name != '' THEN excluded.company_name ELSE vacancies.company_name END,
                salary_text = CASE WHEN excluded.salary_text != '' THEN excluded.salary_text ELSE vacancies.salary_text END,
                salary_from = COALESCE(excluded.salary_from, vacancies.salary_from),
                salary_to = COALESCE(excluded.salary_to, vacancies.salary_to),
                raw_json = excluded.raw_json
            RETURNING id;
        """

        insert_discovery = """
            INSERT INTO vacancy_discoveries (
                search_run_id, search_page_id, vacancy_id, page_number, position_on_page, discovered_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (search_run_id, vacancy_id) DO NOTHING;
        """

        for i, card in enumerate(params.cards):
            raw_json = json.dumps(card.__dict__, default=str, ensure_ascii=False)
            gross_int = 1 if card.gross else 0
            
            async with self.connection.execute(
                upsert_vacancy,
                (
                    card.source, card.external_id, card.url, card.title, card.company_name, card.company_id, card.company_url,
                    card.salary_text, card.salary_from, card.salary_to, card.currency, gross_int, card.city, card.work_format, card.schedule,
                    card.experience, card.employment_type, now_str, now_str, raw_json
                )
            ) as cur_v:
                v_row = await cur_v.fetchone()
                vacancy_id = v_row[0]

            position = i + 1
            await self.connection.execute(
                insert_discovery,
                (params.search_run_id, page_id, vacancy_id, params.page_number, position, now_str, raw_json)
            )

        # 3. Update search_run stats
        update_run_stats = """
            UPDATE search_runs SET
                total_pages_processed = total_pages_processed + 1,
                total_cards_found = total_cards_found + ?,
                last_processed_url = ?
            WHERE id = ?;
        """
        await self.connection.execute(update_run_stats, (len(params.cards), params.current_url, params.search_run_id))
        await self.connection.commit()

        logger.info(f"Committed search_page #{params.page_number} ({len(params.cards)} cards) to SQLite.")

    async def _commit_page_postgres(self, params: PageCommitParams, now_dt: datetime) -> None:
        async with self.connection.acquire() as conn:
            async with conn.transaction():
                # 1. Insert search_pages
                page_query = """
                    INSERT INTO search_pages (
                        search_run_id, page_key, page_number, current_url, canonical_url,
                        status, cards_count, started_at, completed_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (search_run_id, page_key) DO UPDATE SET
                        status = EXCLUDED.status,
                        cards_count = EXCLUDED.cards_count,
                        completed_at = EXCLUDED.completed_at
                    RETURNING id;
                """
                page_id = await conn.fetchval(
                    page_query,
                    params.search_run_id, params.page_key, params.page_number, params.current_url, params.canonical_url, "completed", len(params.cards), now_dt, now_dt
                )

                # 2. Upsert vacancies & insert discoveries
                upsert_vacancy = """
                    INSERT INTO vacancies (
                        source, external_id, canonical_url, title, company_name, company_id, company_url,
                        salary_text, salary_from, salary_to, currency, gross, city, work_format, schedule,
                        experience, employment_type, first_discovered_at, last_discovered_at, raw_json
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $18, $19)
                    ON CONFLICT (source, external_id) DO UPDATE SET
                        last_discovered_at = EXCLUDED.last_discovered_at,
                        canonical_url = COALESCE(NULLIF(EXCLUDED.canonical_url, ''), vacancies.canonical_url),
                        title = COALESCE(NULLIF(EXCLUDED.title, ''), vacancies.title),
                        company_name = COALESCE(NULLIF(EXCLUDED.company_name, ''), vacancies.company_name),
                        salary_text = COALESCE(NULLIF(EXCLUDED.salary_text, ''), vacancies.salary_text),
                        salary_from = COALESCE(EXCLUDED.salary_from, vacancies.salary_from),
                        salary_to = COALESCE(EXCLUDED.salary_to, vacancies.salary_to),
                        raw_json = EXCLUDED.raw_json
                    RETURNING id;
                """

                insert_discovery = """
                    INSERT INTO vacancy_discoveries (
                        search_run_id, search_page_id, vacancy_id, page_number, position_on_page, discovered_at, raw_json
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (search_run_id, vacancy_id) DO NOTHING;
                """

                for i, card in enumerate(params.cards):
                    raw_json = json.dumps(card.__dict__, default=str, ensure_ascii=False)
                    vacancy_id = await conn.fetchval(
                        upsert_vacancy,
                        card.source, card.external_id, card.url, card.title, card.company_name, card.company_id, card.company_url,
                        card.salary_text, card.salary_from, card.salary_to, card.currency, card.gross, card.city, card.work_format, card.schedule,
                        card.experience, card.employment_type, now_dt, raw_json
                    )

                    position = i + 1
                    await conn.execute(
                        insert_discovery,
                        params.search_run_id, page_id, vacancy_id, params.page_number, position, now_dt, raw_json
                    )

                # 3. Update search_run stats
                update_run_stats = """
                    UPDATE search_runs SET
                        total_pages_processed = total_pages_processed + 1,
                        total_cards_found = total_cards_found + $1,
                        last_processed_url = $2
                    WHERE id = $3;
                """
                await conn.execute(update_run_stats, len(params.cards), params.current_url, params.search_run_id)

                logger.info(f"Committed search_page #{params.page_number} ({len(params.cards)} cards) to PostgreSQL.")

    async def record_application(self, app: "VacancyApplication") -> None:
        applied_str = app.applied_at.isoformat() if isinstance(app.applied_at, datetime) else str(app.applied_at)
        
        if self.driver == "sqlite":
            query = """
                INSERT INTO vacancy_applications (
                    vacancy_id, external_id, vacancy_url, status, applied_at, response_text,
                    error_message, cover_letter
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (external_id) DO UPDATE SET
                    status = excluded.status,
                    applied_at = excluded.applied_at,
                    response_text = excluded.response_text,
                    error_message = excluded.error_message,
                    cover_letter = excluded.cover_letter;
            """
            await self.connection.execute(
                query,
                (app.vacancy_id, app.external_id, app.vacancy_url, str(app.status.value), applied_str,
                 app.response_text, app.error_message, app.cover_letter)
            )
            await self.connection.commit()
        else:
            query = """
                INSERT INTO vacancy_applications (
                    vacancy_id, external_id, vacancy_url, status, applied_at, response_text,
                    error_message, cover_letter
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (external_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    applied_at = EXCLUDED.applied_at,
                    response_text = EXCLUDED.response_text,
                    error_message = EXCLUDED.error_message,
                    cover_letter = EXCLUDED.cover_letter;
            """
            async with self.connection.acquire() as conn:
                await conn.execute(
                    query,
                    app.vacancy_id, app.external_id, app.vacancy_url, str(app.status.value),
                    app.applied_at, app.response_text, app.error_message, app.cover_letter
                )

    async def get_unapplied_vacancies(self, limit: int = 50) -> list[dict]:
        if self.driver == "sqlite":
            query = """
                SELECT v.id, v.external_id, v.canonical_url, v.title, v.company_name
                FROM vacancies v
                LEFT JOIN vacancy_applications va ON v.external_id = va.external_id
                WHERE va.id IS NULL
                LIMIT ?;
            """
            async with self.connection.execute(query, (limit,)) as cur:
                rows = await cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "external_id": r[1],
                        "url": r[2],
                        "title": r[3],
                        "company_name": r[4],
                    }
                    for r in rows
                ]
        else:
            query = """
                SELECT v.id, v.external_id, v.canonical_url, v.title, v.company_name
                FROM vacancies v
                LEFT JOIN vacancy_applications va ON v.external_id = va.external_id
                WHERE va.id IS NULL
                LIMIT $1;
            """
            async with self.connection.acquire() as conn:
                rows = await conn.fetch(query, limit)
                return [
                    {
                        "id": r["id"],
                        "external_id": r["external_id"],
                        "url": r["canonical_url"],
                        "title": r["title"],
                        "company_name": r["company_name"],
                    }
                    for r in rows
                ]

    # ------------------------------------------------------------------
    # Settings edited from the web UI
    # ------------------------------------------------------------------

    async def get_all_settings(self) -> Dict[str, str]:
        rows = await self._fetch_all("SELECT key, value FROM app_settings;")
        return {row["key"]: row["value"] for row in rows}

    async def save_settings(self, values: Dict[str, str]) -> None:
        query = """
            INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """
        now = _now()
        for key, value in values.items():
            await self._execute(query, (key, value, now))

    # ------------------------------------------------------------------
    # Resumes
    # ------------------------------------------------------------------

    async def upsert_resume(self, resume: Resume) -> int:
        query = """
            INSERT INTO resumes (
                source_url, external_id, title, full_name, city, experience_text, salary_text,
                skills_json, summary, education_text, certificates_json, raw_text,
                is_active, search_query, context_text, profile_summary, profile_model,
                profile_hash, profile_updated_at, imported_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_url) DO UPDATE SET
                external_id = excluded.external_id,
                title = excluded.title,
                full_name = excluded.full_name,
                city = excluded.city,
                experience_text = excluded.experience_text,
                salary_text = excluded.salary_text,
                skills_json = excluded.skills_json,
                summary = excluded.summary,
                education_text = excluded.education_text,
                certificates_json = excluded.certificates_json,
                raw_text = excluded.raw_text,
                search_query = excluded.search_query,
                context_text = excluded.context_text,
                profile_summary = excluded.profile_summary,
                profile_model = excluded.profile_model,
                profile_hash = excluded.profile_hash,
                profile_updated_at = excluded.profile_updated_at,
                updated_at = excluded.updated_at
            RETURNING id;
        """
        now = _now()
        return await self._write_returning(
            query,
            (
                resume.source_url, resume.external_id, resume.title, resume.full_name, resume.city,
                resume.experience_text, resume.salary_text, json.dumps(resume.skills, ensure_ascii=False),
                resume.summary, resume.education_text,
                json.dumps(resume.certificates, ensure_ascii=False),
                resume.raw_text, 1 if resume.is_active else 0,
                resume.search_query, resume.context_text, resume.profile_summary,
                resume.profile_model, resume.profile_hash,
                resume.profile_updated_at.isoformat() if resume.profile_updated_at else "",
                now, now,
            ),
        )

    async def list_resumes(self) -> List[Resume]:
        rows = await self._fetch_all("SELECT * FROM resumes ORDER BY is_active DESC, updated_at DESC;")
        return [_row_to_resume(row) for row in rows]

    async def get_resume(self, resume_id: int) -> Optional[Resume]:
        row = await self._fetch_one("SELECT * FROM resumes WHERE id = ?;", (resume_id,))
        return _row_to_resume(row) if row else None

    async def get_active_resume(self) -> Optional[Resume]:
        row = await self._fetch_one("SELECT * FROM resumes WHERE is_active = 1 ORDER BY updated_at DESC;")
        return _row_to_resume(row) if row else None

    async def set_active_resume(self, resume_id: int) -> None:
        await self._execute("UPDATE resumes SET is_active = 0 WHERE is_active = 1;")
        await self._execute("UPDATE resumes SET is_active = 1 WHERE id = ?;", (resume_id,))

    async def delete_resume(self, resume_id: int) -> None:
        await self._execute("DELETE FROM resumes WHERE id = ?;", (resume_id,))

    async def update_resume_fields(self, resume_id: int, search_query: str, context_text: str) -> None:
        """The two fields the user edits by hand on the resume card."""
        await self._execute(
            "UPDATE resumes SET search_query = ?, context_text = ?, updated_at = ? WHERE id = ?;",
            (search_query, context_text, _now(), resume_id),
        )

    async def save_resume_profile(
        self, resume_id: int, summary: str, model: str, content_hash: str
    ) -> None:
        await self._execute(
            """
            UPDATE resumes
            SET profile_summary = ?, profile_model = ?, profile_hash = ?,
                profile_updated_at = ?, updated_at = ?
            WHERE id = ?;
            """,
            (summary, model, content_hash, _now(), _now(), resume_id),
        )

    # ------------------------------------------------------------------
    # Match scores
    # ------------------------------------------------------------------

    async def upsert_score(self, score: VacancyScore) -> None:
        query = """
            INSERT INTO vacancy_scores (
                vacancy_id, resume_id, score, verdict, matched_skills_json, missing_skills_json,
                model, error_message, scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (vacancy_id, resume_id) DO UPDATE SET
                score = excluded.score,
                verdict = excluded.verdict,
                matched_skills_json = excluded.matched_skills_json,
                missing_skills_json = excluded.missing_skills_json,
                model = excluded.model,
                error_message = excluded.error_message,
                scored_at = excluded.scored_at;
        """
        await self._execute(
            query,
            (
                score.vacancy_id, score.resume_id, score.score, score.verdict,
                json.dumps(score.matched_skills, ensure_ascii=False),
                json.dumps(score.missing_skills, ensure_ascii=False),
                score.model, score.error_message, _now(),
            ),
        )

    async def get_unscored_vacancies(self, resume_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        """Vacancies that have no successful score against this resume yet."""
        query = """
            SELECT v.id, v.external_id, v.canonical_url, v.title, v.company_name, v.salary_text,
                   v.city, v.work_format, v.experience, v.schedule, v.employment_type, v.raw_json
            FROM vacancies v
            LEFT JOIN vacancy_scores s ON s.vacancy_id = v.id AND s.resume_id = ?
            WHERE s.id IS NULL OR s.error_message != ''
            ORDER BY v.last_discovered_at DESC
            LIMIT ?;
        """
        return await self._fetch_all(query, (resume_id, limit))

    def _vacancy_filters(
        self,
        *,
        min_score: Optional[int],
        max_score: Optional[int],
        only_unapplied: bool,
        only_scored: bool,
        search: str,
        found_for_resume: Optional[int] = None,
    ) -> tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if found_for_resume:
            clauses.append(
                "EXISTS (SELECT 1 FROM vacancy_discoveries d "
                "JOIN search_runs r ON r.id = d.search_run_id "
                "WHERE d.vacancy_id = v.id AND r.resume_id = ?)"
            )
            params.append(found_for_resume)
        if min_score is not None:
            clauses.append("s.score >= ?")
            params.append(min_score)
        if max_score is not None:
            clauses.append("s.score <= ?")
            params.append(max_score)
        if only_scored:
            clauses.append("s.id IS NOT NULL")
        if only_unapplied:
            clauses.append("a.id IS NULL")
        if search:
            clauses.append("(LOWER(v.title) LIKE ? OR LOWER(v.company_name) LIKE ?)")
            pattern = f"%{search.lower()}%"
            params.extend([pattern, pattern])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    async def list_vacancies(
        self,
        *,
        resume_id: Optional[int] = None,
        min_score: Optional[int] = None,
        max_score: Optional[int] = None,
        only_unapplied: bool = False,
        only_scored: bool = False,
        search: str = "",
        found_for_resume: Optional[int] = None,
        order: str = "score",
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        order_sql = {
            "score": "s.score DESC NULLS LAST, v.last_discovered_at DESC",
            "recent": "v.last_discovered_at DESC",
            "salary": "v.salary_from DESC NULLS LAST",
        }.get(order, "s.score DESC NULLS LAST, v.last_discovered_at DESC")
        if self.driver == "sqlite":
            # SQLite sorts NULLs first on DESC; emulate NULLS LAST explicitly.
            order_sql = order_sql.replace("s.score DESC NULLS LAST", "s.score IS NULL, s.score DESC")
            order_sql = order_sql.replace("v.salary_from DESC NULLS LAST", "v.salary_from IS NULL, v.salary_from DESC")

        where, params = self._vacancy_filters(
            min_score=min_score, max_score=max_score, only_unapplied=only_unapplied,
            only_scored=only_scored, search=search, found_for_resume=found_for_resume,
        )
        query = f"""
            SELECT v.id, v.external_id, v.canonical_url, v.title, v.company_name, v.salary_text,
                   v.city, v.work_format, v.experience, v.last_discovered_at,
                   s.score, s.verdict, s.matched_skills_json, s.missing_skills_json, s.scored_at,
                   a.status AS application_status, a.applied_at, a.cover_letter
            FROM vacancies v
            LEFT JOIN vacancy_scores s ON s.vacancy_id = v.id AND s.resume_id = ?
            LEFT JOIN vacancy_applications a ON a.external_id = v.external_id
            {where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?;
        """
        rows = await self._fetch_all(query, [resume_id, *params, limit, offset])
        for row in rows:
            row["matched_skills"] = _load_json_list(row.pop("matched_skills_json", None))
            row["missing_skills"] = _load_json_list(row.pop("missing_skills_json", None))
        return rows

    async def count_vacancies(
        self,
        *,
        resume_id: Optional[int] = None,
        min_score: Optional[int] = None,
        max_score: Optional[int] = None,
        only_unapplied: bool = False,
        only_scored: bool = False,
        search: str = "",
        found_for_resume: Optional[int] = None,
    ) -> int:
        where, params = self._vacancy_filters(
            min_score=min_score, max_score=max_score, only_unapplied=only_unapplied,
            only_scored=only_scored, search=search, found_for_resume=found_for_resume,
        )
        query = f"""
            SELECT COUNT(*) AS total
            FROM vacancies v
            LEFT JOIN vacancy_scores s ON s.vacancy_id = v.id AND s.resume_id = ?
            LEFT JOIN vacancy_applications a ON a.external_id = v.external_id
            {where};
        """
        return int(await self._fetch_val(query, [resume_id, *params]) or 0)

    async def get_vacancies_to_apply(
        self, resume_id: int, min_score: int, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Unapplied vacancies scored at or above the threshold, best first."""
        query = """
            SELECT v.id, v.external_id, v.canonical_url AS url, v.title, v.company_name, s.score
            FROM vacancies v
            JOIN vacancy_scores s ON s.vacancy_id = v.id AND s.resume_id = ?
            LEFT JOIN vacancy_applications a ON a.external_id = v.external_id
            WHERE a.id IS NULL AND s.score >= ? AND s.error_message = ''
            ORDER BY s.score DESC
            LIMIT ?;
        """
        return await self._fetch_all(query, (resume_id, min_score, limit))

    async def get_vacancies_by_ids(self, vacancy_ids: Sequence[int]) -> List[Dict[str, Any]]:
        if not vacancy_ids:
            return []
        placeholders = ", ".join("?" for _ in vacancy_ids)
        query = f"""
            SELECT v.id, v.external_id, v.canonical_url AS url, v.title, v.company_name
            FROM vacancies v
            LEFT JOIN vacancy_applications a ON a.external_id = v.external_id
            WHERE v.id IN ({placeholders}) AND a.id IS NULL;
        """
        return await self._fetch_all(query, list(vacancy_ids))

    # ------------------------------------------------------------------
    # Applications & dashboard
    # ------------------------------------------------------------------

    async def list_applications(
        self, *, status: str = "", limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        where = " WHERE a.status = ?" if status else ""
        params: List[Any] = [status] if status else []
        query = f"""
            SELECT a.id, a.external_id, a.vacancy_url, a.status, a.applied_at, a.response_text,
                   a.error_message, a.cover_letter, v.title, v.company_name, v.salary_text,
                   v.city, s.score, s.verdict
            FROM vacancy_applications a
            LEFT JOIN vacancies v ON v.external_id = a.external_id
            LEFT JOIN vacancy_scores s ON s.vacancy_id = v.id
            {where}
            ORDER BY a.applied_at DESC
            LIMIT ? OFFSET ?;
        """
        return await self._fetch_all(query, [*params, limit, offset])

    async def count_applications(self, status: str = "") -> int:
        where = " WHERE status = ?" if status else ""
        params = [status] if status else []
        return int(await self._fetch_val(f"SELECT COUNT(*) FROM vacancy_applications{where};", params) or 0)

    async def get_dashboard_stats(self, resume_id: Optional[int] = None) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "vacancies_total": int(await self._fetch_val("SELECT COUNT(*) FROM vacancies;") or 0),
            "scored_total": int(
                await self._fetch_val("SELECT COUNT(*) FROM vacancy_scores WHERE resume_id = ?;", (resume_id,)) or 0
            ) if resume_id else 0,
            "applications_total": await self.count_applications(),
            "by_status": {},
            "last_run": None,
        }
        for row in await self._fetch_all(
            "SELECT status, COUNT(*) AS total FROM vacancy_applications GROUP BY status;"
        ):
            stats["by_status"][row["status"]] = row["total"]

        last_run = await self._fetch_one(
            "SELECT id, kind, status, started_at, finished_at FROM task_runs ORDER BY started_at DESC LIMIT 1;"
        )
        stats["last_run"] = last_run
        return stats

    # ------------------------------------------------------------------
    # Background task history
    # ------------------------------------------------------------------

    async def create_task_run(self, run: TaskRun) -> None:
        query = """
            INSERT INTO task_runs (id, kind, status, trigger, params_json, result_json, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET status = excluded.status;
        """
        await self._execute(
            query,
            (
                run.id, run.kind.value, run.status.value, run.trigger,
                json.dumps(run.params, ensure_ascii=False, default=str), "{}",
                run.started_at.isoformat(),
            ),
        )

    async def finish_task_run(
        self, run_id: str, status: TaskStatus, result: Optional[Dict[str, Any]] = None, error_message: str = ""
    ) -> None:
        query = """
            UPDATE task_runs
            SET status = ?, result_json = ?, error_message = ?, finished_at = ?
            WHERE id = ?;
        """
        await self._execute(
            query,
            (
                status.value, json.dumps(result or {}, ensure_ascii=False, default=str),
                error_message, _now(), run_id,
            ),
        )

    async def list_task_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = await self._fetch_all(
            "SELECT * FROM task_runs ORDER BY started_at DESC LIMIT ?;", (limit,)
        )
        for row in rows:
            row["params"] = _load_json_dict(row.pop("params_json", None))
            row["result"] = _load_json_dict(row.pop("result_json", None))
        return rows

    async def fail_stale_task_runs(self) -> None:
        """A run left as `running` means the process died — mark it on startup."""
        await self._execute(
            "UPDATE task_runs SET status = ?, error_message = ?, finished_at = ? WHERE status = ?;",
            (TaskStatus.FAILED.value, "process restarted while the task was running", _now(), TaskStatus.RUNNING.value),
        )


def _load_json_list(raw: Any) -> List[Any]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _load_json_dict(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_ts(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _row_to_resume(row: Dict[str, Any]) -> Resume:
    return Resume(
        id=row.get("id"),
        imported_at=_parse_ts(row.get("imported_at")),
        updated_at=_parse_ts(row.get("updated_at")),
        source_url=row.get("source_url", ""),
        external_id=row.get("external_id", ""),
        title=row.get("title", ""),
        full_name=row.get("full_name", ""),
        city=row.get("city", ""),
        experience_text=row.get("experience_text", ""),
        salary_text=row.get("salary_text", ""),
        skills=_load_json_list(row.get("skills_json")),
        summary=row.get("summary", ""),
        education_text=row.get("education_text", "") or "",
        certificates=_load_json_list(row.get("certificates_json")),
        raw_text=row.get("raw_text", ""),
        is_active=bool(row.get("is_active", 0)),
        search_query=row.get("search_query", "") or "",
        context_text=row.get("context_text", "") or "",
        profile_summary=row.get("profile_summary", "") or "",
        profile_model=row.get("profile_model", "") or "",
        profile_hash=row.get("profile_hash", "") or "",
        profile_updated_at=_parse_ts(row["profile_updated_at"]) if row.get("profile_updated_at") else None,
    )
