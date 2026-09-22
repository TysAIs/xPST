"""Honest token state: derive a truthful badge from a live auth probe.

``xpst auth status`` historically conflated two different facts:

1. "a credential is stored here" (presence), and
2. "this platform works right now" (liveness).

That conflation is how the product produced both false greens (an expired
Instagram session reporting ``authenticated: true``) and false re-auth
demands (a healthy YouTube token that had simply never been checked).

This module turns a live probe entry (see :mod:`xpst.auth_status`) plus
NON-SECRET token metadata into exactly one of a small set of states, and maps
that state onto the badge rendered by the CLI, the web UI, the desktop app and
MCP:

===============  =================  ==========================================
token_state      badge              meaning
===============  =================  ==========================================
valid            connected          a live check just proved it works
expiring         expiring           still usable, but the user must act soon
expired_refreshable  expiring       access token expired; xPST can refresh it
expired_unrefreshable needs_reauth  user action required (no refresh path)
unconfigured     needs_reauth       no credentials configured yet
source_only      source_only        usable as a SOURCE only, never as an upload
disabled         disabled           switched off in config
unknown          unknown            no live check (or a stale one) — NOT green
===============  =================  ==========================================

Hard rules encoded here:

* A green ``connected`` badge requires a live check that passed.  Presence of
  a credential file is never enough.
* A check older than ``DEFAULT_MAX_CHECK_AGE_SECONDS`` (or a probe that never
  ran) renders ``unknown``, never green — with the timestamp shown so the user
  can see how old the claim is.
* ``source_only`` and ``disabled`` platforms never render as connected.
* Expiry is only known when a token carries it (Google's ``expiry`` field).
  When a refresh path exists, an imminent expiry stays ``connected`` because
  xPST refreshes it silently in the background — the badge only warns when the
  user might actually have to act.

Nothing in this module reads, returns, logs or formats token material; only
expiry timestamps and booleans about a refresh path leaving the token store.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── token states ────────────────────────────────────────────────────────────

TOKEN_STATE_VALID = "valid"
TOKEN_STATE_EXPIRING = "expiring"
TOKEN_STATE_EXPIRED_REFRESHABLE = "expired_refreshable"
TOKEN_STATE_EXPIRED_UNREFRESHABLE = "expired_unrefreshable"
TOKEN_STATE_UNCONFIGURED = "unconfigured"
TOKEN_STATE_SOURCE_ONLY = "source_only"
TOKEN_STATE_DISABLED = "disabled"
TOKEN_STATE_UNKNOWN = "unknown"

# ── badges (what a human sees) ──────────────────────────────────────────────

BADGE_CONNECTED = "connected"
BADGE_EXPIRING = "expiring"
BADGE_NEEDS_REAUTH = "needs_reauth"
BADGE_SOURCE_ONLY = "source_only"
BADGE_DISABLED = "disabled"
BADGE_UNKNOWN = "unknown"

BADGE_LABELS: dict[str, str] = {
    BADGE_CONNECTED: "Connected",
    BADGE_EXPIRING: "Expiring soon",
    BADGE_NEEDS_REAUTH: "Needs re-auth",
    BADGE_SOURCE_ONLY: "Source only",
    BADGE_DISABLED: "Disabled",
    BADGE_UNKNOWN: "Not verified",
}

STATE_TO_BADGE: dict[str, str] = {
    TOKEN_STATE_VALID: BADGE_CONNECTED,
    TOKEN_STATE_EXPIRING: BADGE_EXPIRING,
    TOKEN_STATE_EXPIRED_REFRESHABLE: BADGE_EXPIRING,
    TOKEN_STATE_EXPIRED_UNREFRESHABLE: BADGE_NEEDS_REAUTH,
    TOKEN_STATE_UNCONFIGURED: BADGE_NEEDS_REAUTH,
    TOKEN_STATE_SOURCE_ONLY: BADGE_SOURCE_ONLY,
    TOKEN_STATE_DISABLED: BADGE_DISABLED,
    TOKEN_STATE_UNKNOWN: BADGE_UNKNOWN,
}

#: Only these badges may ever render green in a UI.
GREEN_BADGES: frozenset[str] = frozenset({BADGE_CONNECTED})

#: Every platform the badge surface reports on, in display order.
PLATFORM_ORDER: tuple[str, ...] = (
    "youtube",
    "x",
    "instagram",
    "tiktok",
    "threads",
    "facebook",
    "messenger",
)

#: How long before expiry a token without a refresh path starts warning.
DEFAULT_EXPIRING_WINDOW_SECONDS = 24 * 3600

#: A live check older than this may not claim green any more.
DEFAULT_MAX_CHECK_AGE_SECONDS = 15 * 60

#: How long a recorded failed refresh keeps the badge at ``needs_reauth``.
DEFAULT_REFRESH_FAILURE_TTL_SECONDS = 6 * 3600


def _iso(ts: float | None) -> str | None:
    """Epoch seconds → RFC3339 UTC string (``None`` stays ``None``)."""
    if ts is None:
        return None
    with contextlib.suppress(OverflowError, OSError, ValueError):
        return (
            datetime.fromtimestamp(float(ts), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return None


def iso_timestamp(ts: float | None) -> str | None:
    """Public RFC3339 UTC formatter for badge timestamps."""
    return _iso(ts)


def humanize_seconds(seconds: float | None) -> str | None:
    """Compact duration for badge reasons: ``3h 12m``, ``45s``, ``4d``."""
    if seconds is None:
        return None
    try:
        total = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return None
    if total >= 86400:
        days, rem = divmod(total, 86400)
        hours = rem // 3600
        return f"{days}d" if hours == 0 else f"{days}d {hours}h"
    if total >= 3600:
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def parse_expiry(value: Any) -> float | None:
    """Parse a Google-style RFC3339 expiry string into epoch seconds.

    Returns ``None`` for anything unusable — a malformed expiry must degrade to
    "expiry unknown", never to a crash in a status read.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Already epoch-ish (Google writes ISO strings, but be liberal).
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


# ── non-secret token metadata ───────────────────────────────────────────────


@dataclass(frozen=True)
class TokenMetadata:
    """Everything the badge needs about a platform's credentials.

    Deliberately secret-free: an auth mode, whether a refresh path exists, an
    optional access-token expiry, and whether credentials are present at all.
    """

    platform: str
    auth_mode: str = "unknown"
    enabled: bool = True
    configured: bool = False
    has_refresh_token: bool = False
    expires_at: float | None = None
    expiry_source: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "auth_mode": self.auth_mode,
            "enabled": self.enabled,
            "configured": self.configured,
            "has_refresh_token": self.has_refresh_token,
            "expires_at": self.expires_at,
            "expires_at_iso": _iso(self.expires_at),
            "expiry_source": self.expiry_source,
            "notes": list(self.notes),
        }


def _account(config: Any, platform: str) -> Any:
    return getattr(config, platform, None)


def _path_exists(value: Any) -> bool:
    return bool(value and Path(str(value)).expanduser().exists())


def _store_file_for(config: Any, key: str) -> Path | None:
    """Path of an encrypted CredentialStore entry, or ``None``.

    Deliberately a pure path check: constructing a CredentialStore creates the
    credentials directory and (on keyring installs) can hit the OS keychain,
    which must not happen as a side effect of rendering a badge.
    """
    config_dir = str(getattr(config, "config_dir", "") or "")
    if not config_dir:
        return None
    return Path(config_dir).expanduser() / "credentials" / f"{key}.enc"


def _youtube_token_document(config: Any) -> Mapping[str, Any]:
    """Read the stored YouTube token document WITHOUT exposing its contents.

    Returns the parsed mapping (used only for ``expiry`` / ``refresh_token``
    presence) or ``{}``. Encrypted CredentialStore keys are consulted second
    (and only when that entry actually exists) so keyring-backed installs still
    report expiry without paying keychain latency on every status read.
    """
    token_file = getattr(_account(config, "youtube"), "token_file", "") or ""
    if _path_exists(token_file):
        try:
            data = json.loads(Path(str(token_file)).expanduser().read_text(encoding="utf-8"))
            if isinstance(data, Mapping):
                return data
        except (OSError, ValueError):
            pass

    encrypted = _store_file_for(config, "youtube_token")
    if encrypted is None or not (encrypted.exists() or _keyring_opted_in()):
        return {}
    try:
        from xpst.utils.credentials import CredentialStore

        raw = CredentialStore(str(getattr(config, "config_dir", "") or "")).retrieve("youtube_token")
        if raw:
            data = json.loads(raw)
            if isinstance(data, Mapping):
                return data
    except Exception:  # noqa: BLE001 — metadata must never break a status read
        return {}
    return {}


def _keyring_opted_in() -> bool:
    import os

    return os.environ.get("XPST_USE_KEYRING", "").lower() in ("1", "true", "yes")


def token_metadata(config: Any, platform: str) -> TokenMetadata:
    """Collect the badge-relevant, non-secret metadata for one platform."""
    account = _account(config, platform)
    enabled = bool(getattr(account, "enabled", True)) if account is not None else True
    notes: list[str] = []

    if platform == "youtube":
        document = _youtube_token_document(config)
        expires_at = parse_expiry(document.get("expiry"))
        has_refresh = bool(document.get("refresh_token"))
        configured = bool(document) or _path_exists(getattr(account, "token_file", ""))
        return TokenMetadata(
            platform=platform,
            auth_mode="oauth",
            enabled=enabled,
            configured=configured,
            has_refresh_token=has_refresh,
            expires_at=expires_at,
            expiry_source="token_document" if expires_at else None,
            notes=tuple(notes),
        )

    if platform == "x":
        mode = str(getattr(account, "auth_mode", "cookies") or "cookies")
        configured = (
            bool(getattr(account, "bearer_token", ""))
            if mode == "api_v2"
            else _path_exists(getattr(account, "cookies_file", ""))
        )
        notes.append("cookie/api sessions cannot be refreshed programmatically")
        return TokenMetadata(
            platform=platform,
            auth_mode=mode if mode in ("cookies", "api_v2") else "cookies",
            enabled=enabled,
            configured=configured,
        )

    if platform == "instagram":
        graph_token = bool(getattr(account, "graph_access_token", ""))
        mode = "graph_api" if str(getattr(account, "auth_mode", "")) == "graph_api" and graph_token else "session"
        configured = graph_token if mode == "graph_api" else _path_exists(getattr(account, "session_file", ""))
        notes.append("Instagram long-lived tokens are not auto-refreshable")
        return TokenMetadata(
            platform=platform,
            auth_mode=mode,
            enabled=enabled,
            configured=configured,
        )

    if platform == "tiktok":
        has_posting = bool(getattr(account, "client_key", "")) and bool(
            getattr(account, "access_token", "") or getattr(account, "refresh_token", "")
        )
        if has_posting:
            return TokenMetadata(
                platform=platform,
                auth_mode="content_posting_api",
                enabled=enabled,
                configured=True,
                has_refresh_token=bool(getattr(account, "refresh_token", "")),
            )
        configured = bool(
            getattr(account, "username", "")
            or getattr(account, "cookies_from_browser", False)
            or _path_exists(getattr(account, "cookies_file", ""))
        )
        return TokenMetadata(
            platform=platform,
            auth_mode="source_only",
            enabled=enabled,
            configured=configured,
            notes=("source-only mode: downloads work, posting is not configured",),
        )

    if platform == "threads":
        token = bool(getattr(account, "graph_access_token", ""))
        return TokenMetadata(
            platform=platform,
            auth_mode="oauth",
            enabled=enabled,
            configured=bool(token and getattr(account, "threads_user_id", "")),
            # A long-lived Threads token can be exchanged for a fresh one.
            has_refresh_token=token,
        )

    if platform == "facebook":
        # Page-scoped: the Page token is the credential, and a Page token
        # inherits the user token's lifetime, so there is no refresh path of
        # its own (re-run `xpst auth facebook` when it expires).
        page_id = bool(getattr(account, "page_id", ""))
        token = bool(getattr(account, "page_access_token", ""))
        return TokenMetadata(
            platform=platform,
            auth_mode="oauth",
            enabled=enabled,
            configured=bool(page_id and token),
            notes=("Page-scoped token: no refresh path — re-run `xpst auth facebook` when it expires",),
        )

    if platform == "messenger":
        token = bool(getattr(account, "page_access_token", ""))
        return TokenMetadata(
            platform=platform,
            auth_mode="oauth",
            enabled=enabled,
            configured=token,
            notes=("static Page token: no refresh path",),
        )

    return TokenMetadata(platform=platform, enabled=enabled)


# ── derivation ──────────────────────────────────────────────────────────────


def _check_age_seconds(entry: Mapping[str, Any], now: float) -> float | None:
    checked_at = entry.get("checked_at")
    if isinstance(checked_at, (int, float)):
        return max(0.0, now - float(checked_at))
    return None


def derive_token_state(
    platform: str,
    live: Mapping[str, Any] | None,
    meta: TokenMetadata,
    *,
    refresh: Mapping[str, Any] | None = None,
    now: float | None = None,
    expiring_window_seconds: float = DEFAULT_EXPIRING_WINDOW_SECONDS,
    max_check_age_seconds: float = DEFAULT_MAX_CHECK_AGE_SECONDS,
    refresh_failure_ttl_seconds: float = DEFAULT_REFRESH_FAILURE_TTL_SECONDS,
) -> dict[str, Any]:
    """Derive ``token_state`` + badge fields for one platform.

    ``live`` is one entry from :func:`xpst.auth_status.collect_live_auth_status`;
    ``refresh`` is an optional report for this platform from
    :func:`xpst.token_refresh.refresh_due_tokens` (or its persisted record).
    """
    moment = time.time() if now is None else float(now)
    entry: Mapping[str, Any] = live or {}
    auth_mode = str(entry.get("auth_mode") or meta.auth_mode or "unknown")
    enabled = meta.enabled
    if "enabled" in entry and entry.get("enabled") is not None:
        enabled = bool(entry["enabled"])

    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)) and entry.get("live_checked"):
        # A probe handed to us without a timestamp was taken as part of the
        # read that is consuming it; stamp it so the UI can age it out later.
        checked_at = moment
    age = _check_age_seconds({"checked_at": checked_at}, moment)

    refresh_age: float | None = None
    if isinstance(refresh, Mapping) and isinstance(refresh.get("refreshed_at"), (int, float)):
        refresh_age = max(0.0, moment - float(refresh["refreshed_at"]))

    has_refresh = meta.has_refresh_token
    expires_at = meta.expires_at
    expires_in = None if expires_at is None else expires_at - moment
    error_text = str(entry.get("error") or "").strip()
    live_ok = bool(entry.get("authenticated")) and bool(entry.get("session_valid", True))
    live_checked = bool(entry.get("live_checked"))

    configured = meta.configured
    if entry.get("credentials_stored") or entry.get("configured"):
        configured = True

    reason: str
    action: str | None = None
    refresh_failed = bool(
        isinstance(refresh, Mapping)
        and refresh.get("ok") is False
        and (refresh_age is None or refresh_age <= refresh_failure_ttl_seconds)
    )

    if not enabled or error_text.lower() == "disabled":
        state = TOKEN_STATE_DISABLED
        reason = "disabled in config"
    elif auth_mode == "source_only":
        state = TOKEN_STATE_SOURCE_ONLY
        reason = "source only — no posting credentials configured (TikTok dev app pending)"
        if live_checked and not live_ok and error_text:
            reason = f"{reason}; source check failed: {error_text}"
    elif not live_checked:
        state = TOKEN_STATE_UNKNOWN
        reason = "no live check has run — a stored credential is not proof of a working session"
        action = f"xpst auth status --refresh --platform {platform}"
    elif age is not None and age > max_check_age_seconds:
        state = TOKEN_STATE_UNKNOWN
        reason = f"last live check was {humanize_seconds(age)} ago — older than the {humanize_seconds(max_check_age_seconds)} freshness window"
        action = f"xpst auth status --refresh --platform {platform}"
    elif live_ok:
        state = TOKEN_STATE_VALID
        if has_refresh:
            reason = "live check passed; access token refreshes automatically in the background"
            if expires_in is not None and expires_in <= 0:
                reason = "live check passed (the token was refreshed during the check)"
            elif expires_in is not None and expires_in <= expiring_window_seconds:
                reason = (
                    "live check passed; access token refreshes automatically "
                    f"in the background (current one expires in {humanize_seconds(expires_in)})"
                )
        elif expires_at is None:
            reason = "live check passed; token expiry not reported by this provider"
        elif expires_in is not None and expires_in <= expiring_window_seconds:
            state = TOKEN_STATE_EXPIRING
            reason = (
                f"live check passed but the token expires in {humanize_seconds(expires_in)} "
                "and there is no automatic refresh"
            )
            action = f"xpst connect {platform}"
        else:
            reason = "live check passed"
    else:
        # The live check failed. Decide between "xPST can fix this" and
        # "the human must".
        failure = error_text or "live check failed"
        if not configured:
            state = TOKEN_STATE_UNCONFIGURED
            reason = f"no credentials configured — {failure}"
            action = f"xpst connect {platform}"
        elif has_refresh and not refresh_failed:
            state = TOKEN_STATE_EXPIRED_REFRESHABLE
            reason = f"access token expired ({failure}); a refresh token is available and xPST will refresh automatically"
        elif refresh_failed:
            attempts = 0
            with contextlib.suppress(TypeError, ValueError):
                attempts = int(refresh.get("attempts") or 0)  # type: ignore[union-attr]
            detail = ""
            if isinstance(refresh, Mapping) and refresh.get("error"):
                detail = f": {refresh['error']}"
            state = TOKEN_STATE_EXPIRED_UNREFRESHABLE
            reason = (
                f"automatic refresh failed after {attempts} attempt(s){detail}"
                if attempts
                else f"automatic refresh failed{detail}"
            )
            action = f"xpst connect {platform}"
        else:
            state = TOKEN_STATE_EXPIRED_UNREFRESHABLE
            reason = f"{failure}; no refresh path for this auth mode"
            action = f"xpst connect {platform}"

    badge = STATE_TO_BADGE[state]

    return {
        "token_state": state,
        "badge": badge,
        "badge_label": BADGE_LABELS[badge],
        "badge_reason": reason,
        "badge_action": action,
        "checked_at": float(checked_at) if isinstance(checked_at, (int, float)) else None,
        "checked_at_iso": _iso(float(checked_at)) if isinstance(checked_at, (int, float)) else None,
        "check_age_seconds": age,
        "expires_at": expires_at,
        "expires_at_iso": _iso(expires_at),
        "expires_in_seconds": expires_in,
        "auto_refresh": has_refresh,
        "badge_enabled": enabled,
    }


def refresh_record_is_failure(record: Mapping[str, Any] | None, *, now: float | None = None) -> bool:
    """Whether a persisted refresh record should keep the badge red."""
    if not isinstance(record, Mapping) or record.get("ok") is not False:
        return False
    moment = time.time() if now is None else float(now)
    refreshed_at = record.get("refreshed_at")
    if not isinstance(refreshed_at, (int, float)):
        return True
    return (moment - float(refreshed_at)) <= DEFAULT_REFRESH_FAILURE_TTL_SECONDS


def enrich_live_status(
    config: Any,
    live: Mapping[str, Any] | None,
    *,
    refresh_report: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Attach badge truth to every platform in a live-status mapping.

    Platforms missing from ``live`` (no probe has ever run for them) still get
    an entry, and it is never green: the badge is ``unknown`` (or ``disabled``
    / ``source_only``), which is the whole point — a platform xPST cannot
    currently prove must not render as connected.
    """
    moment = time.time() if now is None else float(now)
    source: Mapping[str, Any] = live or {}
    report: Mapping[str, Any] = refresh_report or {}
    # Only the social platforms in PLATFORM_ORDER get a badge: the local file
    # source is not an account and must not appear in an account badge list.
    platforms = list(PLATFORM_ORDER)

    enriched: dict[str, dict[str, Any]] = {}
    for platform in platforms:
        raw = source.get(platform)
        entry = dict(raw) if isinstance(raw, Mapping) else {}
        meta = token_metadata(config, platform)
        badge_info = derive_token_state(
            platform,
            entry,
            meta,
            refresh=report.get(platform) if isinstance(report.get(platform), Mapping) else None,
            now=moment,
        )
        entry.update(badge_info)
        entry["token_metadata"] = meta.to_dict()
        entry.setdefault("live_checked", False)
        enriched[platform] = entry
    return enriched


__all__ = [
    "BADGE_CONNECTED",
    "BADGE_DISABLED",
    "BADGE_EXPIRING",
    "BADGE_LABELS",
    "BADGE_NEEDS_REAUTH",
    "BADGE_SOURCE_ONLY",
    "BADGE_UNKNOWN",
    "DEFAULT_EXPIRING_WINDOW_SECONDS",
    "DEFAULT_MAX_CHECK_AGE_SECONDS",
    "GREEN_BADGES",
    "PLATFORM_ORDER",
    "STATE_TO_BADGE",
    "TOKEN_STATE_DISABLED",
    "TOKEN_STATE_EXPIRED_REFRESHABLE",
    "TOKEN_STATE_EXPIRED_UNREFRESHABLE",
    "TOKEN_STATE_EXPIRING",
    "TOKEN_STATE_SOURCE_ONLY",
    "TOKEN_STATE_UNCONFIGURED",
    "TOKEN_STATE_UNKNOWN",
    "TOKEN_STATE_VALID",
    "TokenMetadata",
    "derive_token_state",
    "enrich_live_status",
    "humanize_seconds",
    "iso_timestamp",
    "parse_expiry",
    "refresh_record_is_failure",
    "token_metadata",
]
