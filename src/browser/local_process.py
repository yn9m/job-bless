import os
import sys
import shutil
import logging
import subprocess
from typing import Optional, List, Tuple
from pathlib import Path

from src.config import BrowserConfig

logger = logging.getLogger(__name__)


class LocalProcessLauncher:
    """
    Manages local browser process creation (e.g. Google Chrome / Chromium)
    with remote debugging enabled.
    """

    def __init__(self, config: BrowserConfig):
        self.config = config
        self._process: Optional[subprocess.Popen] = None

    def resolve_executable_path(self) -> str:
        cmd_name = self.config.local_process.command
        if shutil.which(cmd_name):
            return cmd_name

        if sys.platform == "win32":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
            for candidate in candidates:
                if os.path.exists(candidate):
                    logger.info(f"Auto-resolved browser executable path: '{candidate}'")
                    return candidate

        return cmd_name

    def prepare_args(self, executable: str) -> List[str]:
        args = list(self.config.local_process.args)
        prepared_args = []
        has_headless = False

        for arg in args:
            if arg.startswith("--headless"):
                has_headless = True
                if not self.config.headless:
                    continue  # Strip headless if Headless == False
            if arg.startswith("--user-data-dir="):
                val = arg.split("=", 1)[1]
                abs_val = str(Path(val).resolve())
                os.makedirs(abs_val, exist_ok=True)
                prepared_args.append(f"--user-data-dir={abs_val}")
                continue
            prepared_args.append(arg)

        if self.config.headless and not has_headless:
            prepared_args.append("--headless=new")

        return prepared_args

    def start(self) -> Tuple[str, Optional[int]]:
        if self._process and self._process.poll() is None:
            logger.info("Browser process is already running.")
            return self.config.cdp.endpoint, self._process.pid

        executable = self.resolve_executable_path()
        prepared_args = self.prepare_args(executable)

        logger.info(f"Launching Chrome process: '{executable}' (headless={self.config.headless})")
        logger.info(f"Arguments: {prepared_args}")

        try:
            self._process = subprocess.Popen([executable] + prepared_args)
            logger.info(f"Chrome process launched successfully (PID={self._process.pid}).")
            return self.config.cdp.endpoint, self._process.pid
        except Exception as e:
            logger.error(f"Failed to launch Chrome process: {e}")
            raise RuntimeError(f"Failed to launch Chrome process: {e}") from e

    def stop(self) -> None:
        if not self._process or self._process.poll() is not None:
            return

        pid = self._process.pid
        logger.info(f"Stopping Chrome process tree (PID={pid})...")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                self._process.terminate()
                self._process.wait(timeout=2)
        except Exception as e:
            logger.warning(f"Error terminating Chrome process PID {pid}: {e}")
        finally:
            self._process = None
