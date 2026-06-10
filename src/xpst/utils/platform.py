"""
Cross-platform utility functions for xPST.

Handles OS-specific differences in paths, signals, and process management.
"""

import os
import sys
from pathlib import Path


def get_config_dir() -> Path:
    """
    Get the platform-appropriate config directory.

    - macOS: ~/.xpst/
    - Linux: ~/.xpst/
    - Windows: %APPDATA%\\xPST\\ or ~/.xpst/

    Returns:
        Path to config directory

    Coverage note (W3-4):
        This helper is the single source of truth for the config directory,
        but a large number of hardcoded ``~/.xpst`` literals across the
        codebase (~89) still bypass it. They were NOT migrated wholesale: a
        blind repo-wide replace is risky because many of those literals are
        user-facing *display* strings (help text, console hints, log lines,
        docstrings) rather than real path construction, and rewriting them
        would change output without changing behavior.

        Routed through this helper (high-value, desktop/app-critical paths):
          - ``cli.py`` ``dashboard`` and ``app`` commands' config_dir default.
          - ``cli.py`` cron/launchd scheduler log paths
            (~/.xpst/logs/*), which must be real expanded paths so cron and
            launchd can write to them.

        Deferred (intentionally left as literals for now):
          - Display/help/console strings that merely *mention* ``~/.xpst`` for
            the user (e.g. "Logs: ~/.xpst/logs/cron.log").
          - Default values inside the config loader / config model, which have
            their own POSIX-vs-Windows handling and are exercised by the
            config test suite.
        These are tracked here so the migration state is explicit rather than
        ambiguous; finishing them is a separate, lower-risk follow-up.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "xPST"
    # macOS and Linux: use home directory
    return Path.home() / ".xpst"


def get_ffmpeg_name() -> str:
    """Get the platform-specific ffmpeg binary name."""
    if sys.platform == "win32":
        return "ffmpeg.exe"
    return "ffmpeg"


def get_ffprobe_name() -> str:
    """Get the platform-specific ffprobe binary name."""
    if sys.platform == "win32":
        return "ffprobe.exe"
    return "ffprobe"


def get_ytdlp_fallback_path() -> Path:
    """
    Get platform-specific yt-dlp fallback path (when not on PATH).

    Returns:
        Path to likely yt-dlp binary location
    """
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Scripts" / "yt-dlp.exe"
    elif sys.platform == "darwin":
        ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        return Path.home() / "Library" / "Python" / ver / "bin" / "yt-dlp"
    else:
        return Path.home() / ".local" / "bin" / "yt-dlp"


def get_browser_list() -> list[str]:
    """Get platform-appropriate browser list for cookie extraction.

    Returns browsers in priority order based on OS market share:
    - macOS: Chrome, Brave, Firefox, Safari
    - Windows: Chrome, Edge, Brave, Firefox
    - Linux: Chrome, Brave, Firefox, Chromium

    Returns:
        List of browser name strings for yt-dlp ``--cookies-from-browser``.
    """

    if sys.platform == "darwin":
        return ["chrome", "brave", "firefox", "safari"]
    elif sys.platform == "win32":
        return ["chrome", "edge", "brave", "firefox"]
    else:
        return ["chrome", "brave", "firefox", "chromium"]


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """Check if running on Linux."""
    return sys.platform == "linux"
