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
        # Some test runners and portable environments expose USERPROFILE but
        # not APPDATA. Prefer it over a POSIX-style .xpst fallback.
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            return Path(user_profile) / "AppData" / "Roaming" / "xPST"
    # macOS and Linux: use home directory
    return Path.home() / ".xpst"


def get_ffmpeg_name() -> str:
    """Get the platform-specific ffmpeg binary name."""
    if sys.platform == "win32":
        return "ffmpeg.exe"
    return "ffmpeg"


def stdin_is_interactive() -> bool:
    """True only when stdin is a real interactive console.

    ``sys.stdin.isatty()`` alone is not sufficient on Windows: NUL and
    DEVNULL report ``isatty() == True`` there (``GetFileType`` returns
    ``FILE_TYPE_CHAR``), so agent automation with closed stdin would slip
    past interactive gates and crash on the first prompt (EOFError churn).
    ``GetConsoleMode`` succeeds only for a real console handle, which
    cleanly separates a terminal from NUL/pipes on win32. On POSIX,
    ``isatty()`` is already authoritative.
    """
    try:
        if not sys.stdin or sys.stdin.closed or not sys.stdin.isatty():
            return False
    except (OSError, ValueError, AttributeError):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            handle = msvcrt.get_osfhandle(0)
            mode = wintypes.DWORD()
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        except Exception:
            # Anything odd (closed fd, exotic stdin) → treat as non-interactive
            return False
    return True


def get_ffprobe_name() -> str:
    """Get the platform-specific ffprobe binary name."""
    if sys.platform == "win32":
        return "ffprobe.exe"
    return "ffprobe"


def get_media_bin_dir() -> Path:
    """Directory holding fetch-on-first-use media binaries (ffmpeg/ffprobe).

    ``XPST_MEDIA_BIN_DIR`` overrides it; the default is ``<config dir>/bin``
    (``~/.xpst/bin``). This is where :mod:`xpst.media.binaries` installs a
    verified static build when the machine has no ffmpeg of its own.
    """
    override = os.environ.get("XPST_MEDIA_BIN_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    # A relocated config dir (XPST_CONFIG_DIR, used by CI/smoke harnesses and
    # by anyone running a second isolated profile) owns its own bin dir.
    config_override = os.environ.get("XPST_CONFIG_DIR", "").strip()
    if config_override:
        return Path(config_override).expanduser() / "bin"
    return get_config_dir() / "bin"


def system_media_dirs() -> list[Path]:
    """Directories probed for a system install when PATH lookup fails.

    GUI-launched macOS apps get a minimal PATH that misses both
    ``/opt/homebrew/bin`` and the user's own ``~/bin``, so these are probed
    directly. Kept as a named helper so the resolution order is testable
    without depending on what happens to be installed on the test host.
    """
    home = Path.home()
    dirs = [
        home / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        home / ".local" / "bin",
    ]
    if sys.platform != "darwin":
        dirs.append(Path("/usr/bin"))
    return dirs


def _executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def resolve_ffmpeg_path() -> str | None:
    """
    Resolve the ffmpeg binary path.

    Resolution order (first hit wins):
      1. ``XPST_FFMPEG_PATH`` when set and pointing at an existing file;
      2. a system install — ``shutil.which`` then :func:`system_media_dirs`;
      3. a previously fetched copy in :func:`get_media_bin_dir`
         (``~/.xpst/bin``), downloaded on first use and checksum-verified.

    Returns None when no ffmpeg binary can be found.
    """
    env_path = os.environ.get("XPST_FFMPEG_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    found = shutil.which(get_ffmpeg_name())
    if found:
        return found
    for base in system_media_dirs():
        cand = base / get_ffmpeg_name()
        if _executable_file(cand):
            return str(cand)
    # Last: the copy xPST fetched itself on a machine with no system ffmpeg.
    fetched = get_media_bin_dir() / get_ffmpeg_name()
    if _executable_file(fetched):
        return str(fetched)
    return None


def resolve_ffprobe_path() -> str | None:
    """Resolve the ffprobe binary path.

    Same order as :func:`resolve_ffmpeg_path`: the ``XPST_FFPROBE_PATH``
    override first, then a system install (PATH plus :func:`system_media_dirs`),
    then the fetched copy in :func:`get_media_bin_dir`.
    """
    env_path = os.environ.get("XPST_FFPROBE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    found = shutil.which(get_ffprobe_name())
    if found:
        return found
    for base in system_media_dirs():
        cand = base / get_ffprobe_name()
        if _executable_file(cand):
            return str(cand)
    fetched = get_media_bin_dir() / get_ffprobe_name()
    if _executable_file(fetched):
        return str(fetched)
    return None


def resolve_ytdlp_path() -> Path | None:
    """
    Resolve the yt-dlp CLI binary path.

    Order: ``XPST_YTDLP_PATH`` override (set by the Tauri shell to the
    bundled resource zipapp), then ``shutil.which``, then the
    platform-specific fallback probed by :func:`get_ytdlp_fallback_path`.
    Returns None when no yt-dlp binary can be found.
    """
    env_path = os.environ.get("XPST_YTDLP_PATH")
    if env_path and os.path.exists(env_path):
        return Path(env_path)
    found = shutil.which("yt-dlp")
    if found:
        return Path(found)
    fallback = get_ytdlp_fallback_path()
    try:
        if fallback.is_file():
            return fallback
    except OSError:
        pass
    return None


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
