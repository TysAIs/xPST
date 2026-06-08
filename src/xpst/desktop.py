"""
xPST Desktop App Launcher

Provides a native macOS desktop experience using pywebview.
The app appears in the Dock with its own window, traffic light buttons,
and proper lifecycle management (start server on launch, stop on close).

Falls back to opening the system browser if pywebview is not installed.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Default icon path for the macOS dock / window
_ICON_PATH = Path.home() / ".xpst" / "icon.icns"

# Default window dimensions
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 800
_MIN_WIDTH = 960
_MIN_HEIGHT = 600


def _find_free_port() -> int:
    """Find an available localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll until the HTTP server is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_nicegui_server(port: int, config_dir: str) -> threading.Thread:
    """Start the NiceGUI dashboard server in a background daemon thread.

    Returns the daemon thread so the caller can reference it.
    """
    from xpst.dashboard.server import start_dashboard

    def _run() -> None:
        try:
            start_dashboard(port=port, host="127.0.0.1", config_dir=config_dir)
        except Exception:
            logger.exception("NiceGUI server thread exited with error")

    thread = threading.Thread(target=_run, daemon=True, name="nicegui-server")
    thread.start()
    return thread


def launch_desktop_app(
    config_dir: str = "~/.xpst",
    port: int | None = None,
) -> None:
    """Launch the xPST dashboard as a native desktop window.

    1. Starts the NiceGUI server on a background thread.
    2. Opens a pywebview native window pointing at the local server.
    3. When the window is closed the server thread is left to die with the
       process (daemon thread), which is the cleanest shutdown path.

    Args:
        config_dir: Path to the xPST config directory.
        port: Explicit port. If *None* a free port is chosen automatically.
    """
    try:
        import webview  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "pywebview is not installed. Install it with:\n"
            "  pip install 'xpst[desktop]'   # or\n"
            "  pip install pywebview>=4.0"
        )

    import webview

    port = port or _find_free_port()
    url = f"http://127.0.0.1:{port}"

    # ── 1. Start the NiceGUI server in the background ──
    logger.info("Starting xPST dashboard server on %s", url)
    _start_nicegui_server(port, config_dir)

    # ── 2. Wait for the server to be ready ──
    if not _wait_for_server("127.0.0.1", port, timeout=30):
        raise RuntimeError(
            f"xPST dashboard server did not become ready on {url} within 30 s"
        )
    logger.info("Dashboard server is ready at %s", url)

    # ── 3. Resolve icon ──
    icon_path: str | None = None
    if _ICON_PATH.exists():
        icon_path = str(_ICON_PATH)
        logger.info("Using app icon: %s", icon_path)
    else:
        logger.debug("Icon not found at %s (using default)", _ICON_PATH)

    # ── 4. Create and show the native window ──
    window = webview.create_window(
        title="xPST — Cross-Platform Studio",
        url=url,
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        min_size=(_MIN_WIDTH, _MIN_HEIGHT),
        resizable=True,
        hidden=False,
        # macOS-specific: text_select enables standard text selection
        text_select=True,
    )

    def on_closed() -> None:
        """Called when the window is closed (user clicks red close button)."""
        logger.info("Desktop window closed — shutting down")

    window.events.closed += on_closed

    # webview.start() **must** run on the main thread on macOS (Cocoa
    # requirement).  It blocks until all windows are closed.
    logger.info("Opening native window (port %d)…", port)
    webview.start(
        gui="cocoa",       # force the Cocoa backend on macOS
        debug=False,
        icon=icon_path,
    )

    # After the window is closed the process will exit.  The server thread
    # is a daemon and will be terminated automatically.


def launch_browser_fallback(
    config_dir: str = "~/.xpst",
    port: int = 8080,
) -> None:
    """Open the dashboard in the system browser (legacy / fallback path).

    This is used when pywebview is not available.
    """
    import webbrowser

    from xpst.dashboard.server import start_dashboard

    url = f"http://localhost:{port}"

    def _open() -> None:
        time.sleep(2)  # give the server a moment to start
        webbrowser.open(url)

    opener = threading.Thread(target=_open, daemon=True)
    opener.start()

    logger.info("Starting dashboard in browser at %s", url)
    start_dashboard(port=port, host="0.0.0.0", config_dir=config_dir)
