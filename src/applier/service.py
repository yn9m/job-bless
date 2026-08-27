import asyncio
import os
import logging

from src.config import Config
from src.db.connection import init_sqlite, init_postgres
from src.db.repository import DatabaseRepository
from src.browser.local_process import LocalProcessLauncher
from src.browser.connector import BrowserConnector
from src.applier.auto_applier import HHAutoApplier
from src.db.models import ApplicationStatus
from src.llm.factory import create_llm_client_from

logger = logging.getLogger(__name__)


class ApplicationService:
    def __init__(self, config: Config):
        self.config = config
        self.db_conn = None
        self.repository = None
        self.local_browser_launcher = None
        self.llm_client = None
        self.resume_text = ""
        self.applier = None

    async def initialize(self) -> None:
        logger.info("Initializing ApplicationService...")
        if self.config.db.driver == "sqlite":
            self.db_conn = await init_sqlite(self.config.db.sqlite_path)
            self.repository = DatabaseRepository(self.db_conn, driver="sqlite")
        else:
            self.db_conn = await init_postgres(self.config.db)
            self.repository = DatabaseRepository(self.db_conn, driver="postgres")

        # Load Resume Text
        resume_path = self.config.applier.resume_path
        if os.path.exists(resume_path):
            try:
                with open(resume_path, "r", encoding="utf-8") as f:
                    self.resume_text = f.read()
                logger.info(f"Loaded candidate resume from '{resume_path}' ({len(self.resume_text)} chars).")
            except Exception as e:
                logger.error(f"Failed to read resume file at {resume_path}: {e}")
        else:
            logger.warning(f"Resume file not found at '{resume_path}'. LLM match scoring will be skipped.")

        # Initialize LLM Client if enabled
        if self.config.llm.enabled:
            try:
                self.llm_client = create_llm_client_from(self.config)
                logger.info("LLM Client initialized successfully for smart applier.")
            except Exception as e:
                logger.error(f"Failed to initialize LLM Client: {e}")

        self.applier = HHAutoApplier(
            llm_client=self.llm_client,
            resume_text=self.resume_text,
            min_score=self.config.applier.min_llm_score,
        )

        if self.config.browser.provider == "local_process":
            logger.info("Ensuring local Chrome process is running for auto-applier...")
            self.local_browser_launcher = LocalProcessLauncher(self.config.browser)
            endpoint, pid = self.local_browser_launcher.start()
            self.config.browser.cdp.endpoint = endpoint

    async def run_apply_batch(self, limit: int = 10) -> dict:
        unapplied = await self.repository.get_unapplied_vacancies(limit=limit)
        logger.info(f"Fetched {len(unapplied)} unapplied vacancies from DB to process.")

        if not unapplied:
            logger.info("No unapplied vacancies found in database.")
            return {"total": 0, "applied": 0, "skipped_questions": 0, "skipped_low_score": 0, "already_applied": 0, "failed": 0}

        stats = {"total": len(unapplied), "applied": 0, "skipped_questions": 0, "skipped_low_score": 0, "already_applied": 0, "failed": 0}

        connector = BrowserConnector(self.config.browser)
        async with connector.connect() as page:
            for item in unapplied:
                v_id = item["id"]
                ext_id = item["external_id"]
                url = item["url"]
                title = item["title"]

                logger.info(f"Processing vacancy [{ext_id}] '{title}' ({url})...")
                result = await self.applier.apply_to_vacancy(page, vacancy_url=url, external_id=ext_id, vacancy_id=v_id)

                await self.repository.record_application(result)

                if result.status == ApplicationStatus.APPLIED:
                    stats["applied"] += 1
                elif result.status == ApplicationStatus.SKIPPED_QUESTIONS:
                    stats["skipped_questions"] += 1
                elif result.status == ApplicationStatus.SKIPPED_LOW_SCORE:
                    stats["skipped_low_score"] += 1
                elif result.status == ApplicationStatus.ALREADY_APPLIED:
                    stats["already_applied"] += 1
                else:
                    stats["failed"] += 1

                await asyncio.sleep(2.0)  # Pause between applications

        logger.info(f"Application batch completed: {stats}")
        return stats

    async def close(self) -> None:
        if self.llm_client:
            try:
                await self.llm_client.aclose()
            except Exception as e:
                logger.warning(f"Error closing LLM client: {e}")

        if self.db_conn:
            try:
                await self.db_conn.close()
            except Exception as e:
                logger.warning(f"Error closing DB: {e}")
