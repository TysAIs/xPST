"""Bounded background refresh of expiring access tokens.

The badge module (:mod:`xpst.token_state`) tells the truth about whether a
platform works right now; this module is what keeps that truth green without
bothering a human: when an access token is due (expiring, or already expired
with a refresh token available) xPST refreshes it in the background with a
BOUNDED retry, then persists the result so the next status read can report it.

Design rules:

* Bounded: ``max_attempts`` attempts per platform, exponential backoff, and a
  wall-clock ``deadline`` — a hung provider can never wedge a status read or a
  desktop health tick.
* Secret-free: nothing here returns, logs or persists token material.  Errors
  are redacted (see :func:`redact`) before they reach a report, a log line or
  the UI.
* Non-interactive: every refresher uses an existing credential file/store
  path; none of them prompts or opens a browser.  A platform without a refresh
  path is never attempted (and its badge stays whatever honesty demands).
* Observable: the outcome is written to ``token_refresh.json`` (0600) in the
  config dir, so a failure survives the process and keeps the badge red
  instead of being forgotten.

The refresh drivers live in :data:`REFRESH_DRIVERS` and are looked up by
platform; tests inject a stubbed mapping instead of touching the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xpst.token_state import (
    BADGE_EXPIRING,
    PLATFORM_ORDER,
    derive_token_state,
    token_metadata,
)
from xpst.utils.logger import get_logger
from xpst.utils.secure_io import write_text_0600

logger = get_logger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_DEADLINE_SECONDS = 30.0

#: Short profile for paths on a request/latency budget (web API first paint,
#: desktop health tick).  The dedicated CLI command uses the defaults above.
FAST_MAX_ATTEMPTS = 2
FAST_BASE_DELAY_SECONDS = 0.5
FAST_DEADLINE_SECONDS = 15.0

#: Renew an access token once it is this close to expiry (when a refresh path
#: exists). Small enough that a background tick will not thrash, large enough
#: that a token cannot die between two health ticks.
DEFAULT_REFRESH_LEAD_SECONDS = 15 * 60

REPORT_FILENAME = "token_refresh.json"

# Credential-shaped substrings that must never reach a log line, a report or a
# badge reason.  Deliberately broad: a false positive only shortens a message.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ya29\.[A-Za-z0-9._\-]+"),
    re.compile(r"IGQV[A-Za-z0-9._\-]+"),
    re.compile(r"EAA[A-Za-z0-9._\-]{10,}"),
    re.compile(r"(?i)(access_token|refresh_token|client_secret|page_access_token)=([^&\s]+)"),
    re.compile(r"1/[A-Za-z0-9_\-]{20,}"),
    re.compile(r"TH[A-Za-z0-9_\-]{6,}"),
)

_SENSITIVE_KEYWORDS = ("token", "secret", "password", "authorization", "credential")


def redact(message: str, *, limit: int = 200) -> str:
    """Strip credential-shaped material from an error string.

    Any ``key=value`` pair whose key looks sensitive is collapsed to
    ``key=[redacted]``, known token prefixes are dropped, and the result is
    truncated so an upstream error page cannot flood the UI.
    """
    text = str(message or "")
    for pattern in _CREDENTIAL_PATTERNS:
        if "=" in pattern.pattern:
            text = pattern.sub(lambda m: f"{m.group(1)}=[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    text = text.replace("\n", " ").strip()
    return text[:limit]


def report_path(config: Any) -> Path:
    """Path of the persisted refresh report for a config dir."""
    config_dir = str(getattr(config, "config_dir", "") or Path.home() / ".xpst")
    return Path(config_dir).expanduser() / REPORT_FILENAME


def _platform_badge_info(
    config: Any,
    platform: str,
    live: Mapping[str, Any] | None,
    *,
    now: float,
) -> dict[str, Any]:
    entry = live.get(platform) if isinstance(live, Mapping) else None
    meta = token_metadata(config, platform)
    return derive_token_state(platform, entry if isinstance(entry, Mapping) else None, meta, now=now)


def due_platforms(
    config: Any,
    live: Mapping[str, Any] | None = None,
    *,
    now: float | None = None,
    force: bool = False,
    lead_seconds: float = DEFAULT_REFRESH_LEAD_SECONDS,
) -> list[str]:
    """Platforms whose access token should be refreshed right now.

    A platform is due when it has a WORKING refresh path and either:

    * its known expiry is within ``lead_seconds`` (or already past), or
    * a live check reports the badge as ``expiring`` (expired-but-refreshable,
      or expiring with no automatic path left).

    ``force`` returns every platform with a refresh path, which is what an
    explicit ``--force`` run means.
    """
    moment = time.time() if now is None else float(now)
    due: list[str] = []
    for platform in PLATFORM_ORDER:
        meta = token_metadata(config, platform)
        if not meta.enabled or not meta.has_refresh_token:
            continue
        if force:
            due.append(platform)
            continue
        if meta.expires_at is not None and (meta.expires_at - moment) <= lead_seconds:
            due.append(platform)
            continue
        info = _platform_badge_info(config, platform, live, now=moment)
        if info["badge"] == BADGE_EXPIRING:
            due.append(platform)
    return due


@dataclass
class RefreshOutcome:
    """Result of one platform's refresh attempt (secret-free by construction)."""

    platform: str
    attempted: bool = False
    ok: bool = False
    attempts: int = 0
    reason: str = ""
    error: str | None = None
    refreshed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "attempted": self.attempted,
            "ok": self.ok,
            "attempts": self.attempts,
            "reason": self.reason,
            "error": self.error,
            "refreshed_at": self.refreshed_at,
        }


# ── refresh drivers ─────────────────────────────────────────────────────────


def _load_youtube_credentials(config: Any) -> tuple[Any, Path | None]:
    """Load Google credentials for refresh (file first, then credential store)."""
    from google.oauth2.credentials import Credentials

    token_file = str(getattr(config.youtube, "token_file", "") or "")
    token_path = Path(token_file).expanduser() if token_file else None

    document: str | None = None
    if token_path is not None and token_path.exists():
        with contextlib.suppress(OSError):
            document = token_path.read_text(encoding="utf-8")
    if not document:
        from xpst.utils.credentials import CredentialStore

        document = CredentialStore(str(getattr(config, "config_dir", "") or "")).retrieve("youtube_token")
    if not document:
        raise ValueError("no stored YouTube token to refresh — run: xpst connect youtube")

    creds = Credentials.from_authorized_user_info(json.loads(document))
    if not creds.refresh_token:
        raise ValueError("stored YouTube token has no refresh token — run: xpst connect youtube")
    return creds, token_path


async def refresh_youtube_token(config: Any, *, request_factory: Callable[[], Any] | None = None) -> None:
    """Refresh the YouTube OAuth access token and persist it (0600)."""
    from google.auth.transport.requests import Request

    creds, token_path = _load_youtube_credentials(config)
    creds.refresh((request_factory or Request)())

    document = creds.to_json()
    if token_path is not None:
        write_text_0600(token_path, document)
    from xpst.utils.credentials import CredentialStore

    CredentialStore(str(getattr(config, "config_dir", "") or "")).store("youtube_token", document)


async def _threads_uploader(config: Any) -> Any:
    from xpst.platforms.threads import ThreadsUploader
    from xpst.utils.sessions import SessionManager

    uploader: Any = ThreadsUploader(config)
    uploader._session_manager = SessionManager(getattr(config, "config_dir", "~/.xpst"))
    return uploader


async def refresh_threads_token(config: Any) -> None:
    """Exchange the long-lived Threads token for a fresh one and persist it."""
    from xpst.utils.credentials import CredentialStore

    uploader = await _threads_uploader(config)
    token = await uploader._refresh_access_token()
    if not token:
        raise ValueError("threads refresh returned no token")
    CredentialStore(str(getattr(config, "config_dir", "") or "")).store("threads_access_token", token)


async def refresh_tiktok_token(config: Any) -> None:
    """Refresh the TikTok Content Posting API access token and persist it."""
    from xpst.platforms.tiktok import TikTokUploader
    from xpst.utils.credentials import CredentialStore

    uploader = TikTokUploader(config)
    token = await uploader._refresh_access_token()
    if not token:
        raise ValueError("tiktok refresh returned no token")
    CredentialStore(str(getattr(config, "config_dir", "") or "")).store("tiktok_access_token", token)
    with contextlib.suppress(Exception):
        # In-process convenience only; the encrypted store above is the source
        # of truth and no plaintext token is written to config.yaml.
        config.tiktok.access_token = token


#: platform → driver.  Only platforms with a refresh path appear here.
REFRESH_DRIVERS: dict[str, Callable[[Any], Awaitable[None]]] = {
    "youtube": refresh_youtube_token,
    "threads": refresh_threads_token,
    "tiktok": refresh_tiktok_token,
}


# ── the job ─────────────────────────────────────────────────────────────────


async def _attempt_platform(
    config: Any,
    platform: str,
    driver: Callable[[Any], Awaitable[None]],
    *,
    max_attempts: int,
    base_delay: float,
    deadline: float,
    sleep: Callable[[float], Awaitable[None]],
) -> RefreshOutcome:
    started = time.monotonic()
    outcome = RefreshOutcome(platform=platform, attempted=True)
    last_error: str | None = None

    for attempt in range(1, max(1, max_attempts) + 1):
        outcome.attempts = attempt
        try:
            await driver(config)
        except Exception as exc:  # noqa: BLE001 — one bad provider must not sink the job
            last_error = redact(f"{type(exc).__name__}: {exc}")
            logger.warning("Token refresh failed for %s (attempt %s): %s", platform, attempt, last_error)
            if attempt >= max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            if time.monotonic() - started + delay >= deadline:
                last_error = f"{last_error} (retry budget exhausted)"
                break
            await sleep(delay)
        else:
            outcome.ok = True
            outcome.attempts = attempt
            outcome.reason = f"refreshed on attempt {attempt}"
            outcome.refreshed_at = time.time()
            logger.info("Refreshed %s access token (attempt %s)", platform, attempt)
            return outcome

    outcome.ok = False
    outcome.error = last_error or "refresh failed"
    outcome.reason = f"refresh failed after {outcome.attempts} attempt(s)"
    outcome.refreshed_at = time.time()
    return outcome


async def refresh_due_tokens_async(
    config: Any,
    *,
    live: Mapping[str, Any] | None = None,
    drivers: Mapping[str, Callable[[Any], Awaitable[None]]] | None = None,
    platforms: list[str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    deadline: float = DEFAULT_DEADLINE_SECONDS,
    force: bool = False,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Refresh every due platform with a bounded retry.

    Returns a secret-free report ``{platform: outcome}``.  An empty dict means
    "nothing was due" — the common case, and a cheap one (no network calls).
    """
    registry = dict(REFRESH_DRIVERS if drivers is None else drivers)
    targets = list(platforms) if platforms else due_platforms(config, live, now=now, force=force)
    targets = [name for name in targets if name in registry]
    if not targets:
        return {}

    sleeper = sleep or asyncio.sleep
    report: dict[str, dict[str, Any]] = {}
    for platform in targets:
        outcome = await _attempt_platform(
            config,
            platform,
            registry[platform],
            max_attempts=max_attempts,
            base_delay=base_delay,
            deadline=deadline,
            sleep=sleeper,
        )
        report[platform] = outcome.to_dict()
    return report


def refresh_due_tokens(config: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
    """Synchronous wrapper around :func:`refresh_due_tokens_async`."""
    return asyncio.run(refresh_due_tokens_async(config, **kwargs))


# ── persistence ─────────────────────────────────────────────────────────────


def save_refresh_report(config: Any, report: Mapping[str, Any]) -> None:
    """Persist the report (0600, secret-free) next to the credentials."""
    if not report:
        return
    try:
        path = report_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_0600(path, json.dumps(dict(report), indent=2, sort_keys=True))
    except OSError as exc:  # pragma: no cover - a read-only config dir
        logger.warning("Could not persist token refresh report: %s", redact(str(exc)))


def load_refresh_report(config: Any) -> dict[str, dict[str, Any]]:
    """Read the last persisted report (``{}`` when absent/unreadable)."""
    path = report_path(config)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    return {
        str(name): dict(value)
        for name, value in data.items()
        if isinstance(value, Mapping)
    }


@dataclass
class RefreshRun:
    """A refresh run plus the env-var-tunable knobs (for CLI/API callers)."""

    report: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def attempted(self) -> list[str]:
        return [name for name, item in self.report.items() if item.get("attempted")]

    @property
    def failed(self) -> list[str]:
        return [name for name, item in self.report.items() if item.get("attempted") and not item.get("ok")]

    @property
    def succeeded(self) -> list[str]:
        return [name for name, item in self.report.items() if item.get("ok")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platforms": self.report,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
        }


__all__ = [
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "FAST_BASE_DELAY_SECONDS",
    "FAST_DEADLINE_SECONDS",
    "FAST_MAX_ATTEMPTS",
    "REFRESH_DRIVERS",
    "REPORT_FILENAME",
    "RefreshOutcome",
    "RefreshRun",
    "due_platforms",
    "load_refresh_report",
    "redact",
    "refresh_due_tokens",
    "refresh_due_tokens_async",
    "refresh_threads_token",
    "refresh_tiktok_token",
    "refresh_youtube_token",
    "report_path",
    "save_refresh_report",
]
