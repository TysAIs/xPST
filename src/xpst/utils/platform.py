"""
Cross-platform utility functions for xPST.

Handles OS-specific differences in paths, signals, and process management.
"""

import os
import shutil
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

    ``XPST_CONFIG_DIR`` is honored first, making this helper the single source
    of truth for user data and configuration paths.
    """
    if override := os.environ.get("XPST_CONFIG_DIR"):
        return Path(override).expanduser()
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


def ensure_ffmpeg() -> str | None:
    """Return the ffmpeg executable path when it is available on PATH."""
    return shutil.which(get_ffmpeg_name()) or shutil.which("ffmpeg")


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
