import os
import sys
import asyncio
import logging
import threading
import time

from src.config import Config
from src.service.orchestrator import Orchestrator
from src.applier.service import ApplicationService

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Configure standard logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("main")

# How long to wait for a clean interpreter exit before forcing it.
EXIT_WATCHDOG_SECONDS = 3.0


async def main() -> None:
    mode = "run"
    config_path = None

    if len(sys.argv) > 1:
        if sys.argv[1] in ("run", "apply", "login", "web"):
            mode = sys.argv[1]
            if len(sys.argv) > 2:
                config_path = sys.argv[2]
        elif sys.argv[1].endswith(".yaml") or sys.argv[1].endswith(".yml"):
            config_path = sys.argv[1]

    config = Config.load(config_path)

    logger.info("==========================================")
    logger.info("Starting Unified Python Career Agent")
    logger.info(f"Mode: {mode}")
    logger.info(f"DB Driver: {config.db.driver} ({config.db.sqlite_path if config.db.driver == 'sqlite' else config.db.host})")
    logger.info(f"Browser Headless: {config.browser.headless}")
    logger.info("==========================================")

    if mode == "web":
        # The web UI owns the whole runtime: pages, background jobs, scheduler.
        from src.web.app import serve

        logger.info(f"Web UI: http://{config.web.host}:{config.web.port}")
        await serve(config)
    elif mode == "apply":
        logger.info("Running Auto-Applier task (applying to vacancies without employer questions)...")
        app_service = ApplicationService(config)
        try:
            await app_service.initialize()
            stats = await app_service.run_apply_batch(limit=20)
            logger.info(f"Application run completed: {stats}")
        finally:
            await app_service.close()
    else:
        logger.info("Vacancy collection module is currently disabled on startup.")
        logger.info("All DB and Collector code remains intact and ready for re-activation.")


def run() -> None:
    """Entry point with a quiet Ctrl+C.

    Ctrl+C is a normal way to stop the server, so it must not print a traceback,
    and the shell prompt must come back even if a leftover Playwright driver
    keeps non-daemon threads alive after the loop is closed.
    """
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Остановлено пользователем.")
    finally:
        force_exit_after(EXIT_WATCHDOG_SECONDS)


def force_exit_after(seconds: float) -> None:
    """Last-resort watchdog: leaves no chance for a hung thread to stall exit."""
    def watchdog() -> None:
        time.sleep(seconds)
        logging.shutdown()  # atexit handlers do not run after os._exit
        os._exit(0)

    thread = threading.Thread(target=watchdog, daemon=True, name="exit-watchdog")
    thread.start()


if __name__ == "__main__":
    run()
