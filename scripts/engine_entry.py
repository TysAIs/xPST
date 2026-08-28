#!/usr/bin/env python3
"""PyInstaller entrypoint for the xPST engine sidecar.

Bundled by ``build_engine.spec`` as the ``xpst-engine`` one-file executable
that the Tauri 2 shell spawns.  Kept deliberately thin: it reads the port
from the environment (the shell always passes ``XPST_DASHBOARD_PORT``) and
launches the FastAPI dashboard, bypassing the CLI to avoid pulling in the
whole command surface.
"""

from __future__ import annotations

import os
import threading
import time


def _watch_parent() -> None:
    """Exit when the parent (the shell-spawned bootloader) disappears.

    PyInstaller onefile runs as bootloader-parent -> app-child.  If the
    Tauri shell is killed (even with SIGKILL) it can only reap the direct
    child; this watchdog makes the grandchild exit so the engine can never
    outlive the app as an orphaned uvicorn server.
    """
    parent = os.getppid()
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(0)


def main() -> None:
    threading.Thread(target=_watch_parent, daemon=True).start()

    from xpst.dashboard.server import start_dashboard

    port = int(os.environ.get("XPST_DASHBOARD_PORT", "8080"))
    config_dir = os.environ.get("XPST_CONFIG_DIR", "~/.xpst")
    start_dashboard(port=port, host="127.0.0.1", config_dir=os.path.expanduser(config_dir))


if __name__ == "__main__":
    main()
