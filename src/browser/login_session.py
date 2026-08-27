import os
import sys
import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright

from src.config import Config
from src.browser.local_process import LocalProcessLauncher
from src.browser.connector import BrowserConnector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("login_session")

STORAGE_STATE_PATH = "./data/storage_state.json"


async def run_manual_login(config_path: str | None = None) -> str:
    config = Config.load(config_path)
    config.browser.headless = False  # Always force visible window for login

    logger.info("============================================================")
    logger.info("HH.ru Interactive Manual Login Initializer")
    logger.info("============================================================")

    # 1. Start Chrome process
    launcher = LocalProcessLauncher(config.browser)
    endpoint, pid = launcher.start()
    config.browser.cdp.endpoint = endpoint

    async with async_playwright() as pw:
        logger.info(f"Connecting to Chrome via CDP: {endpoint}")
        browser = await pw.chromium.connect_over_cdp(endpoint, timeout=config.browser.cdp.timeout_ms)
        
        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = await context.new_page()

        login_url = "https://hh.ru/account/login"
        logger.info(f"Opening login page: {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded")

        print("\n" + "=" * 64)
        print("ACTION REQUIRED:")
        print("1. A physical Chrome window has been opened at https://hh.ru/account/login")
        print("2. Please enter your phone/email, password, or SMS code to log in.")
        print("3. Once you are successfully logged in on HH.ru, come back here and PRESS ENTER.")
        print("=" * 64 + "\n")

        # Wait for user input in console
        await asyncio.get_event_loop().run_in_executor(None, input, "Press ENTER after completing login in Chrome...")

        # Export storage state (cookies + localStorage) as a backup
        storage_path = Path(STORAGE_STATE_PATH).resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(storage_path))

        logger.info("============================================================")
        logger.info(f"SUCCESS: Session state saved to '{storage_path}'")
        logger.info(f"Chrome profile saved to '{Path('./data/browser-profile').resolve()}'")
        logger.info("Future automation runs will automatically use this logged-in account.")
        logger.info("============================================================")

        return str(storage_path)


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.local.yaml"
    asyncio.run(run_manual_login(cfg_path))
