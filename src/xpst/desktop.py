"""
xPST Desktop App Launcher — Cross-Platform

Provides a native desktop experience on macOS, Windows, and Linux.
- macOS: Cocoa window with dock icon + traffic lights
- Windows: native Win32 window with taskbar icon
- Linux: GTK/Qt window with system tray support

Falls back to opening the system browser if pywebview is not installed.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from xpst.utils.platform import get_config_dir

logger = logging.getLogger(__name__)

# ── Platform detection ────────────────────────────────────────────────────
_SYSTEM = platform.system()  # "Darwin", "Windows", "Linux"

# ── Default window dimensions ─────────────────────────────────────────────
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 800
_MIN_WIDTH = 960
_MIN_HEIGHT = 600

# ── Icon paths (OS-specific) ─────────────────────────────────────────────
_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"

def _get_icon_path() -> str | None:
    """Resolve the best icon for the current OS."""
    if _SYSTEM == "Darwin":
        # macOS: prefer .icns, fall back to .png
        for name in ("xpst.icns", "icon.icns", "xpst-full.png"):
            p = _ASSETS_DIR / name
            if p.exists():
                return str(p)
        # Also check the user config directory.
        p = get_config_dir() / "icon.icns"
        if p.exists():
            return str(p)
    elif _SYSTEM == "Windows":
        # Windows: prefer .ico, fall back to .png
        for name in ("xpst.ico", "icon.ico", "xpst-full.png"):
            p = _ASSETS_DIR / name
            if p.exists():
                return str(p)
    else:
        # Linux: prefer .png
        for name in ("xpst-full.png", "icon-256.png", "xpst-128.png"):
            p = _ASSETS_DIR / name
            if p.exists():
                return str(p)
    # Last resort: any PNG in assets
    for p in sorted(_ASSETS_DIR.glob("*.png")):
        return str(p)
    return None


def _get_gui_backend() -> str | None:
    """Select the best pywebview backend for the current OS.

    Returns None to let pywebview auto-detect.
    """
    if _SYSTEM == "Darwin":
        return "cocoa"
    elif _SYSTEM == "Windows":
        return "edgechromium"  # WebView2 (Chromium-based, ships with Win10+)
    elif _SYSTEM == "Linux":
        # Try GTK first (most common), then Qt variants
        try:
            import gi  # noqa: F401
            return "gtk"
        except ImportError:
            for qt_module in ("PySide6", "PyQt6", "PyQt5"):
                try:
                    __import__(qt_module)
                    return "qt"
                except ImportError:
                    continue
            return None  # let pywebview figure it out
    return None


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
    """Start the NiceGUI dashboard server in a background daemon thread."""
    from xpst.dashboard.server import start_dashboard

    def _run() -> None:
        try:
            start_dashboard(port=port, host="127.0.0.1", config_dir=config_dir)
        except Exception as e:
            logger.debug("NiceGUI server thread error: %s", e)
            logger.exception("NiceGUI server thread exited with error")

    thread = threading.Thread(target=_run, daemon=True, name="nicegui-server")
    thread.start()
    return thread


# ── Platform integrations ─────────────────────────────────────────────────

def _install_macos_app(icon_path: str | None) -> None:
    """Create/update a .app bundle in ~/Applications for macOS dock integration."""
    app_dir = Path.home() / "Applications" / "xPST.app"
    contents = app_dir / "Contents"
    macos_dir = contents / "MacOS"
    resources = contents / "Resources"

    # Create structure
    for d in (contents, macos_dir, resources):
        d.mkdir(parents=True, exist_ok=True)

    # Copy icon to Resources
    if icon_path and Path(icon_path).exists():
        dest_icon = resources / "AppIcon.icns"
        if icon_path.endswith(".icns"):
            shutil.copy2(icon_path, dest_icon)
        elif icon_path.endswith(".png"):
            # Convert PNG to ICNS if sips available
            try:
                subprocess.run(
                    ["sips", "-s", "format", "icns", icon_path, "--out", str(dest_icon)],
                    capture_output=True, timeout=10,
                )
            except Exception as e:
                logger.debug("Icon conversion failed, copying as-is: %s", e)
                shutil.copy2(icon_path, resources / "AppIcon.png")

    # Write Info.plist
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>xPST</string>
    <key>CFBundleDisplayName</key><string>xPST</string>
    <key>CFBundleIdentifier</key><string>com.{'xpst'}.app</string>
    <key>CFBundleVersion</key><string>0.1.0</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleExecutable</key><string>xpst</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>10.15</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>"""
    (contents / "Info.plist").write_text(plist)

    # Write launcher script
    launcher = macos_dir / "xpst"
    launcher.write_text(f"""#!/bin/bash
exec {sys.executable} -m xpst app "$@"
""")
    launcher.chmod(0o755)

    logger.info("macOS .app bundle installed at %s", app_dir)


def _create_linux_desktop_entry(icon_path: str | None) -> None:
    """Create a .desktop file for Linux application menu / taskbar."""
    desktop_dir = Path.home() / ".local" / "share" / "applications"
    desktop_dir.mkdir(parents=True, exist_ok=True)

    # Copy icon to standard location
    icon_dest = ""
    if icon_path and Path(icon_path).exists():
        icon_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps"
        icon_dir.mkdir(parents=True, exist_ok=True)
        icon_dest = str(icon_dir / "xpst.png")
        shutil.copy2(icon_path, icon_dest)

    xpst_bin = shutil.which("xpst") or f"{sys.executable} -m xpst"

    desktop_entry = f"""[Desktop Entry]
Name=xPST
Comment=Cross-Platform Studio — Cross-post short-form video
Exec={xpst_bin} app
Icon={icon_dest or 'xpst'}
Terminal=false
Type=Application
Categories=AudioVideo;Video;
Keywords=video;crosspost;social;shorts;
"""
    desktop_file = desktop_dir / "xpst.desktop"
    desktop_file.write_text(desktop_entry)
    desktop_file.chmod(0o755)

    # Update desktop database
    try:
        subprocess.run(
            ["update-desktop-database", str(desktop_dir)],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        logger.debug("Desktop database update failed (not critical): %s", e)

    logger.info("Linux .desktop entry installed at %s", desktop_file)


def _create_windows_shortcut(icon_path: str | None) -> None:
    """Create a Start Menu shortcut on Windows.

    Gracefully handles missing winshell or win32com — falls back to
    creating a simple .bat launcher if COM automation is unavailable.
    """
    start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "xPST"
    start_menu.mkdir(parents=True, exist_ok=True)

    shortcut_path = start_menu / "xPST.lnk"

    # Try COM-based shortcut first (requires pywin32)
    try:
        from win32com.client import Dispatch  # noqa: F401

        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(shortcut_path))
        shortcut.Targetpath = sys.executable
        shortcut.Arguments = "-m xpst app"
        shortcut.WorkingDirectory = str(Path.home())
        shortcut.Description = "xPST — Cross-Platform Studio"
        if icon_path and Path(icon_path).exists():
            shortcut.IconLocation = icon_path
        shortcut.save()
        logger.info("Windows shortcut created at %s", shortcut_path)
        return
    except ImportError:
        logger.debug("win32com not available, trying bat fallback")
    except Exception as e:
        logger.warning("COM shortcut creation failed: %s", e)

    # Fallback: create a .bat launcher
    try:
        xpst_cmd = shutil.which("xpst") or f'"{sys.executable}" -m xpst'
        bat_path = start_menu / "xPST.bat"
        bat_path.write_text(
            f'@echo off\r\n{xpst_cmd} app %*\r\n',
            encoding="utf-8",
        )
        logger.info("Windows .bat launcher created at %s", bat_path)
    except Exception as e:
        logger.debug("Failed to create .bat fallback: %s", e)


# ── Main launcher ─────────────────────────────────────────────────────────

def launch_desktop_app(
    config_dir: str | None = None,
    port: int | None = None,
) -> None:
    """Launch the xPST dashboard as a native desktop window.

    Works on macOS, Windows, and Linux. Installs platform-specific
    integrations (dock icon, taskbar shortcut, desktop entry) on first run.

    Args:
        config_dir: Path to the xPST config directory.
        port: Explicit port. If *None* a free port is chosen automatically.

    Raises:
        RuntimeError: If pywebview is not installed or server fails to start.
    """
    config_dir = str(Path(config_dir).expanduser() if config_dir is not None else get_config_dir())
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
    icon_path = _get_icon_path()
    if icon_path:
        logger.info("Using app icon: %s", icon_path)
    else:
        logger.debug("No icon found in assets/")

    # ── 4. Install platform integrations ──
    try:
        if _SYSTEM == "Darwin":
            _install_macos_app(icon_path)
        elif _SYSTEM == "Linux":
            _create_linux_desktop_entry(icon_path)
        elif _SYSTEM == "Windows":
            _create_windows_shortcut(icon_path)
    except Exception as e:
        logger.debug("Platform integration error: %s", e)
        logger.warning("Failed to install platform integration", exc_info=True)

    # ── 5. Create and show the native window ──
    gui_backend = _get_gui_backend()
    logger.info("Creating native window (backend=%s, port %d)…", gui_backend or "auto", port)

    window = webview.create_window(
        title="xPST — Cross-Platform Studio",
        url=url,
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        min_size=(_MIN_WIDTH, _MIN_HEIGHT),
        resizable=True,
        hidden=False,
        text_select=True,
    )

    def on_closed() -> None:
        """Called when the window is closed."""
        logger.info("Desktop window closed — shutting down")

    window.events.closed += on_closed

    # webview.start() must run on the main thread (OS requirement on all platforms).
    # It blocks until all windows are closed.
    start_kwargs: dict = {"debug": False}
    if gui_backend:
        start_kwargs["gui"] = gui_backend
    if icon_path and _SYSTEM != "Darwin":
        # macOS uses .app bundle icon; Windows/Linux pass to webview
        start_kwargs["icon"] = icon_path

    webview.start(**start_kwargs)


def launch_browser_fallback(
    config_dir: str | None = None,
    port: int = 8080,
) -> None:
    """Open the dashboard in the system browser (fallback when pywebview is unavailable).

    Works on all platforms via Python's webbrowser module.
    """
    import webbrowser

    from xpst.dashboard.server import start_dashboard

    config_dir = str(Path(config_dir).expanduser() if config_dir is not None else get_config_dir())
    url = f"http://localhost:{port}"

    def _open() -> None:
        time.sleep(2)  # give the server a moment to start
        webbrowser.open(url)

    opener = threading.Thread(target=_open, daemon=True)
    opener.start()

    logger.info("Starting dashboard in browser at %s", url)
    start_dashboard(port=port, host="0.0.0.0", config_dir=config_dir)
