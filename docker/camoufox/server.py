import os
import sys
from camoufox.server import launch_server

host = os.getenv("HOST", "0.0.0.0")
port = int(os.getenv("PORT", 3000))
# Запускаем в headful режиме (headless=False), чтобы браузер отрисовывался в Xvfb и отображался в VNC
headless_env = os.getenv("HEADLESS", "false").lower()
headless = headless_env == "true"
ws_path = os.getenv("WS_PATH", "camoufox")

print(f"Launching Camoufox server on {host}:{port} (headless={headless}, ws_path={ws_path})...", flush=True)

try:
    launch_server(
        host=host,
        port=port,
        ws_path=ws_path,
        headless=headless,
    )
except Exception as e:
    print(f"Error launching Camoufox server: {e}", file=sys.stderr, flush=True)
    sys.exit(1)
