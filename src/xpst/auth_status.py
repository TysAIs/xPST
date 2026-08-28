"""Live authentication-status collection for ``xpst auth status``.

The status command historically reported credential-file PRESENCE, not
liveness — an expired Instagram session file showed ``authenticated: true``
while the session was actually dead (instagrapi 403 LoginRequired).

This module routes the status command through the SAME live session
validation used by ``xpst health``: the per-platform uploader
``check_health()`` validators (the engine ``check_health`` /
dashboard ``_live_auth`` pattern from PRs #66 and #70):

- youtube: token refresh + channels.list probe
- x:       twikit cookie probe (``client.user()``)
- instagram: sessionid probe via instagrapi (``account_info()``) or a
  Meta Graph API ``/me`` probe when ``auth_mode == "graph_api"`` and a
  graph token is configured
- tiktok:  Content Posting API token probe, or a source check
  (yt-dlp + cookie jar) in ``source_only`` mode

Non-interactive by design: every validator used here fails CLOSED
(returns ``authenticated=False`` with an ``error``) — none of them
prompts, launches a browser, or blocks on stdin. The full engine is NOT
constructed: engine ``__init__`` registers a shutdown handler, runs
crash recovery and hard-requires FFmpeg, none of which a status read
should need. Only the uploaders (and their session manager) are built,
exactly mirroring ``engine._init_platforms``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.utils.logger import get_logger
from xpst.utils.sessions import SessionManager

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)

# Per-platform live-check ceiling. The validators hit live APIs (twikit,
# instagrapi, googleapiclient, graph.facebook.com); without a timeout a
# hung network call would freeze `xpst auth status --json` indefinitely.
LIVE_CHECK_TIMEOUT_SECONDS = 90

_GRAPH_API_ME_URL = "https://graph.facebook.com/v19.0/me"


def _age_days(path: str | Path | None) -> int | None:
    """Session age in whole days from a credential file's mtime.

    Age can never be negative: clock skew or coarse filesystem timestamps
    can put st_mtime marginally ahead of now (same rule as the G53
    session-health check in cli.py).
    """
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        return None
    try:
        return max(0, int((time.time() - p.stat().st_mtime) // 86400))
    except OSError:
        return None


def _credential_file_for(config: XPSTConfig, platform: str) -> str | None:
    """Primary credential file per platform (drives session_age_days)."""
    if platform == "youtube":
        return config.youtube.token_file or None
    if platform == "x":
        return config.x.cookies_file or None
    if platform == "instagram":
        return config.instagram.session_file or None
    if platform == "tiktok":
        return config.tiktok.cookies_file or None
    return None


def _auth_mode_for(config: XPSTConfig, platform: str) -> str:
    """Effective auth mode per platform.

    - youtube: always ``oauth``
    - x: ``cookies`` | ``api_v2`` (from config)
    - instagram: ``graph_api`` only when configured AND a graph token is
      actually present; otherwise the session (instagrapi sessionid)
      path is what will really be used → ``session``
    - tiktok: ``content_posting_api`` when Content Posting API
      credentials exist, else ``source_only`` (yt-dlp downloads)
    """
    if platform == "youtube":
        return "oauth"
    if platform == "x":
        return config.x.auth_mode if config.x.auth_mode in ("cookies", "api_v2") else "cookies"
    if platform == "instagram":
        if config.instagram.auth_mode == "graph_api" and config.instagram.graph_access_token:
            return "graph_api"
        return "session"
    if platform == "tiktok":
        if config.tiktok.client_key and (config.tiktok.access_token or config.tiktok.refresh_token):
            return "content_posting_api"
        return "source_only"
    return "unknown"


async def _graph_api_probe(token: str) -> tuple[bool, str | None, dict[str, Any]]:
    """Probe a Meta Graph API token with GET /me?fields=id (no prompt)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _GRAPH_API_ME_URL,
                params={"fields": "id", "access_token": token},
            )
        if resp.status_code == 200:
            data = resp.json()
            return True, None, {"graph_user_id": str(data.get("id", ""))}
        return False, f"Graph API probe failed: HTTP {resp.status_code}", {}
    except Exception as exc:  # noqa: BLE001 — status must never crash
        return False, f"Graph API probe failed: {str(exc)[:200]}", {}


def _tiktok_source_check(config: XPSTConfig) -> tuple[bool, str | None, dict[str, Any]]:
    """Source-mode TikTok check: yt-dlp present + a usable cookie jar.

    Mirrors sources/tiktok.py check_health() without running yt-dlp
    subprocesses (fast, offline, non-interactive).
    """
    import shutil

    yt_dlp_path = shutil.which("yt-dlp") or shutil.which(str(Path.home() / "bin" / "yt-dlp"))
    cookies_available = False
    if config.tiktok.cookies_from_browser:
        cookies_available = True  # assume the browser session exists
    elif config.tiktok.cookies_file:
        cookies_available = Path(config.tiktok.cookies_file).expanduser().exists()

    ok = bool(yt_dlp_path and cookies_available)
    error = None
    if not yt_dlp_path:
        error = "yt-dlp not found — TikTok source cannot download"
    elif not cookies_available:
        error = "No TikTok cookies available (cookie jar missing / cookies_from_browser unset)"
    details = {
        "yt_dlp_installed": bool(yt_dlp_path),
        "cookies_available": cookies_available,
        "username_configured": bool(config.tiktok.username),
    }
    return ok, error, details


def _build_uploaders(config: XPSTConfig) -> dict[str, Any]:
    """Instantiate enabled uploaders exactly like engine._init_platforms.

    Import failures (missing optional dependencies such as instagrapi) are
    swallowed — that platform then reports ``disabled`` instead of
    crashing the whole status command in dep-less environments.
    """
    uploaders: dict[str, Any] = {}

    if config.youtube.enabled:
        try:
            from xpst.platforms.youtube import YouTubeUploader

            uploaders["youtube"] = YouTubeUploader(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube uploader unavailable: %s", exc)

    if config.x.enabled:
        try:
            from xpst.platforms.x import XUploader

            uploaders["x"] = XUploader(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("X uploader unavailable: %s", exc)

    if config.instagram.enabled:
        try:
            from xpst.platforms.instagram import InstagramUploader

            uploaders["instagram"] = InstagramUploader(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Instagram uploader unavailable: %s", exc)

    if config.tiktok.enabled:
        try:
            from xpst.platforms.tiktok import TikTokUploader

            uploaders["tiktok"] = TikTokUploader(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TikTok uploader unavailable: %s", exc)

    # Same session-manager wiring the engine does (secure auth path).
    session_manager = SessionManager(config.config_dir)
    for uploader in uploaders.values():
        uploader._session_manager = session_manager

    return uploaders


async def _check_via_uploader(uploader: Any) -> dict[str, Any]:
    """Run one uploader's check_health() with a hard timeout."""
    try:
        health = await asyncio.wait_for(
            uploader.check_health(), timeout=LIVE_CHECK_TIMEOUT_SECONDS
        )
        return {
            "authenticated": bool(health.authenticated),
            "session_valid": bool(health.session_valid),
            "error": health.error,
            "details": dict(health.details or {}),
        }
    except asyncio.TimeoutError:
        return {
            "authenticated": False,
            "session_valid": False,
            "error": f"Live check timed out after {LIVE_CHECK_TIMEOUT_SECONDS}s",
            "details": {},
        }
    except Exception as exc:  # noqa: BLE001 — fail closed, never crash status
        return {
            "authenticated": False,
            "session_valid": False,
            "error": f"Live check failed: {str(exc)[:200]}",
            "details": {},
        }


async def collect_live_auth_status_async(
    config: XPSTConfig,
    uploaders: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Live per-platform auth status (async core).

    Args:
        config: Loaded xPST configuration.
        uploaders: Optional pre-built uploader dict (tests inject fakes
            here to mock the validators).

    Returns:
        ``{platform: {authenticated, session_valid, auth_mode,
        session_age_days, live_checked, error, details}}`` for
        youtube/x/instagram/tiktok. Validation failures degrade to
        ``authenticated=False`` + ``error`` — never an exception and
        never a prompt.
    """
    if uploaders is None:
        uploaders = _build_uploaders(config)

    result: dict[str, dict[str, Any]] = {}
    for name in ("youtube", "x", "instagram", "tiktok"):
        entry: dict[str, Any] = {
            "authenticated": False,
            "session_valid": False,
            "auth_mode": _auth_mode_for(config, name),
            "session_age_days": _age_days(_credential_file_for(config, name)),
            "live_checked": True,
            "error": None,
            "details": {},
        }

        uploader = uploaders.get(name)
        if name == "tiktok" and config.tiktok.enabled and entry["auth_mode"] == "source_only":
            # Source-mode TikTok needs no uploader/credentials — the check
            # is yt-dlp + cookie jar availability (offline, non-interactive).
            ok, error, details = _tiktok_source_check(config)
            entry.update(authenticated=ok, session_valid=ok, error=error, details=details)
        elif uploader is None:
            # Matches engine.check_health's wording for known-but-off platforms.
            entry["error"] = "disabled"
        elif name == "instagram" and entry["auth_mode"] == "graph_api":
            ok, error, details = await _graph_api_probe(config.instagram.graph_access_token)
            entry.update(authenticated=ok, session_valid=ok, error=error, details=details)
        else:
            entry.update(await _check_via_uploader(uploader))

        result[name] = entry

    return result


def collect_live_auth_status(
    config: XPSTConfig,
    uploaders: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Sync wrapper: live per-platform auth status for the CLI.

    Total failure of the collection itself (e.g. no event loop possible)
    degrades to all-false entries with an error — the command still
    returns honest, backward-compatible JSON instead of crashing.
    """
    platforms = ("youtube", "x", "instagram", "tiktok")
    try:
        return asyncio.run(collect_live_auth_status_async(config, uploaders))
    except Exception as exc:  # noqa: BLE001 — status must never crash
        logger.warning("Live auth status collection failed: %s", exc)
        return {
            name: {
                "authenticated": False,
                "session_valid": False,
                "auth_mode": "unknown",
                "session_age_days": None,
                "live_checked": True,
                "error": f"Live check failed: {str(exc)[:200]}",
                "details": {},
            }
            for name in platforms
        }
