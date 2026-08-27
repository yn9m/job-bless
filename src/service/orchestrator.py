import asyncio
import logging
from datetime import datetime, timezone

from src.config import Config
from src.db.connection import init_sqlite, init_postgres
from src.db.repository import DatabaseRepository
from src.db.models import SearchRun, SearchRunStatus, PageCommitParams, VacancyCard, CollectionSummary
from src.browser.local_process import LocalProcessLauncher
from src.collector.collector import HHVacancyCardCollector

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: Config):
        self.config = config
        self.db_conn = None
        self.repository = None
        self.local_browser_launcher = None
        self.collector = HHVacancyCardCollector()

    async def initialize(self) -> None:
        logger.info("Initializing unified Orchestrator...")
        
        # 1. Init DB connection
        if self.config.db.driver == "sqlite":
            self.db_conn = await init_sqlite(self.config.db.sqlite_path)
            self.repository = DatabaseRepository(self.db_conn, driver="sqlite")
        else:
            self.db_conn = await init_postgres(self.config.db)
            self.repository = DatabaseRepository(self.db_conn, driver="postgres")

        # 2. Start local Chrome process if configured
        if self.config.browser.provider == "local_process":
            logger.info("Ensuring local Chrome process is running...")
            self.local_browser_launcher = LocalProcessLauncher(self.config.browser)
            endpoint, pid = self.local_browser_launcher.start()
            self.config.browser.cdp.endpoint = endpoint

    async def run_job(self, task_id: str = "run_local_1", search_url: str = "") -> CollectionSummary:
        url = search_url or self.config.scroller.search_url
        logger.info(f"Starting HH Job automation task '{task_id}' for URL: {url}")

        # Create SearchRun record in DB
        run = SearchRun(
            id=task_id,
            task_id=task_id,
            search_url=url,
            browser_session_id=self.config.browser.cdp.endpoint,
            transport=self.config.browser.transport,
            status=SearchRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        await self.repository.create_search_run(run)

        final_summary: CollectionSummary | None = None
        try:
            async for item in self.collector.collect(
                browser_config=self.config.browser,
                search_url=url,
                task_id=task_id,
                scroller_config=self.config.scroller,
            ):
                if isinstance(item, VacancyCard):
                    logger.debug(f"Parsed card: {item.title} @ {item.company_name}")
                elif isinstance(item, PageCommitParams):
                    logger.info(f"Committing page #{item.page_number} ({len(item.cards)} cards) to DB...")
                    await self.repository.commit_page_transaction(item)
                elif isinstance(item, CollectionSummary):
                    final_summary = item

            # Update status in DB
            status = SearchRunStatus.COMPLETED if (final_summary and final_summary.final_status == "completed") else SearchRunStatus.FAILED
            reason = final_summary.completion_reason if final_summary else "completed"
            await self.repository.update_search_run_status(task_id, status=status, reason=reason)

            logger.info(f"Job task '{task_id}' finished successfully with status={status.value}.")
            return final_summary or CollectionSummary(task_id=task_id)

        except Exception as e:
            logger.error(f"Job task '{task_id}' failed with error: {e}", exc_info=True)
            await self.repository.update_search_run_status(
                task_id,
                status=SearchRunStatus.FAILED,
                error_code="ERR_JOB_FAILED",
                error_message=str(e)
            )
            raise

    async def close(self) -> None:
        logger.info("Shutting down Orchestrator resources...")
        if self.db_conn:
            try:
                if self.config.db.driver == "sqlite":
                    await self.db_conn.close()
                else:
                    await self.db_conn.close()
            except Exception as e:
                logger.warning(f"Error closing DB connection: {e}")
