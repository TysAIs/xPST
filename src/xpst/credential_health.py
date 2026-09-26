"""Proactive credential-health watchdog: warn BEFORE a credential dies.

Why this module exists
----------------------
``xpst.token_state`` marks a credential ``expiring`` only inside a 24-hour
window. For a credential xPST renews by itself (a refresh token exists) that is
correct — the background refresh handles it and a day's notice is plenty. For a
credential ONLY THE HUMAN can renew — an Instagram session, a Meta long-lived
token with no refresh path, a TikTok refresh token — 24 hours is too late: the
first symptom the user notices is a post that failed. Tyler's exact complaint.

So the watchdog applies a *reauth horizon* (7 days by default) to credentials a
human must act on, and leaves the 24-hour window alone for the ones xPST can
renew unattended.

Honesty rules (same non-negotiables as the rest of the auth surface)
-------------------------------------------------------------------
* **Never invent a deadline.** If the provider reports no expiry (instagrapi
  sessions do not), the credential is reported as ``unverifiable``: the report
  says it cannot warn ahead of time and points at a live check, instead of
  guessing a date.
* **Never call a credential fine because a file exists.** Every judgement is
  derived from a live-probe badge produced by :mod:`xpst.token_state`.
* **``needs_attention`` only contains things a human can act on**, ordered by
  urgency; everything else is explicitly bucketed so a surface cannot silently
  drop it.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from xpst.token_state import (
    PLATFORM_ORDER,
    TOKEN_STATE_DISABLED,
    TOKEN_STATE_EXPIRED_UNREFRESHABLE,
    TOKEN_STATE_EXPIRING,
    TOKEN_STATE_SOURCE_ONLY,
    TOKEN_STATE_UNCONFIGURED,
    TOKEN_STATE_UNKNOWN,
    TOKEN_STATE_VALID,
)

__all__ = [
    "ATTENTION_HORIZON_DAYS",
    "ATTENTION_STATES",
    "build_credential_health",
    "render_health_lines",
]

#: Default lead time for credentials only a human can renew.
ATTENTION_HORIZON_DAYS = 7

#: Live-probe states that mean "a human has to do something".
ATTENTION_STATES: frozenset[str] = frozenset(
    {
        TOKEN_STATE_EXPIRED_UNREFRESHABLE,
        TOKEN_STATE_UNCONFIGURED,
    }
)

#: States that are not an account to maintain and must never be nagged about.
NOT_A_CREDENTIAL_STATES: frozenset[str] = frozenset(
    {
        TOKEN_STATE_DISABLED,
        TOKEN_STATE_SOURCE_ONLY,
    }
)


def _humanize_seconds(total: float) -> str:
    """Compact human duration ('5d 3h', '42m', '2h')."""
    seconds = max(0, int(total))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d" if hours == 0 else f"{days}d {hours}h"
    if hours:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    return f"{minutes}m"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_credential_health(
    canonical: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    reauth_horizon_days: int = ATTENTION_HORIZON_DAYS,
) -> dict[str, Any]:
    """Turn canonical auth-status entries into a forward-looking report.

    ``canonical`` is the mapping returned by
    :func:`xpst.auth_status.collect_live_auth_status` (platform → entry); entries
    missing badge fields are still reported, never assumed healthy.
    """
    moment = time.time() if now is None else float(now)
    horizon_seconds = float(reauth_horizon_days) * 86400
    source: Mapping[str, Any] = canonical or {}

    entries: list[dict[str, Any]] = []
    for platform, raw in source.items():
        entry: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        state = str(entry.get("token_state") or "")
        badge = str(entry.get("badge") or "")
        expires_at = _as_float(entry.get("expires_at"))
        expires_in = _as_float(entry.get("expires_in_seconds"))
        if expires_in is None and expires_at is not None:
            expires_in = expires_at - moment
        auto_refresh = bool(entry.get("auto_refresh"))

        item: dict[str, Any] = {
            "platform": platform,
            "state": state or "unknown",
            "badge": badge,
            "badge_label": entry.get("badge_label"),
            "auto_refresh": auto_refresh,
            "expires_at": expires_at,
            "expires_at_iso": _iso(expires_at),
            "expires_in_seconds": expires_in,
            "days_left": None if expires_in is None else round(expires_in / 86400, 2),
            "session_age_days": entry.get("session_age_days"),
            "action": entry.get("badge_action"),
            "reason": entry.get("badge_reason"),
            "bucket": "ok",
            "attention": None,
        }

        if state in NOT_A_CREDENTIAL_STATES:
            item["bucket"] = "not_applicable"
        elif platform not in PLATFORM_ORDER:
            # The local file source (and any future catalog entry) is not an
            # account: there is nothing to renew, so it must not appear in a
            # list of credentials a human has to maintain.
            item["bucket"] = "not_applicable"
            item["attention"] = "not an account surface (source/catalog only)"
        elif not state or state == TOKEN_STATE_UNKNOWN:
            # Nothing was proven: no badge, a stale probe, or an inconclusive
            # transport failure. Not "fine", and not a deadline either.
            item["bucket"] = "unverifiable"
            item["attention"] = (
                "not verified by a live check — xPST cannot warn ahead of time until one runs"
            )
        elif state in ATTENTION_STATES:
            item["bucket"] = "needs_attention"
            item["attention"] = "expired or unconfigured — reconnect now"
        elif state == TOKEN_STATE_EXPIRING and not auto_refresh:
            # Live probe passed, but this credential dies and only the human can
            # replace it: this is the case the watchdog exists for.
            if expires_in is not None and expires_in <= horizon_seconds:
                item["bucket"] = "needs_attention"
                item["attention"] = (
                    "expires in "
                    f"{_humanize_seconds(expires_in)} and there is no automatic refresh"
                )
            else:
                item["bucket"] = "ok"
        elif state in (TOKEN_STATE_EXPIRING, TOKEN_STATE_VALID):
            if expires_in is None:
                # A passing probe with no reported expiry: honest to say we
                # cannot see the deadline, and to say what would show it.
                item["bucket"] = "unverifiable"
                item["attention"] = (
                    "provider reports no expiry — xPST cannot predict this one; "
                    "`xpst health` proves it is alive right now"
                )
            else:
                item["bucket"] = "ok"
                if auto_refresh and expires_in <= horizon_seconds:
                    item["attention"] = (
                        f"access token expires in {_humanize_seconds(expires_in)} — "
                        "xPST refreshes it automatically"
                    )
        else:
            item["bucket"] = "unverifiable"
            item["attention"] = f"unrecognised token state {state!r}"

        entries.append(item)

    needs_attention = [e for e in entries if e["bucket"] == "needs_attention"]
    unverifiable = [e for e in entries if e["bucket"] == "unverifiable"]

    # Order by urgency: known deadlines first (soonest first), then the ones
    # already broken, then platforms without a readable deadline.
    def _sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
        days = item.get("days_left")
        return (0, float(days), item["platform"]) if days is not None else (1, 0.0, item["platform"])

    needs_attention.sort(key=_sort_key)
    unverifiable.sort(key=lambda i: i["platform"])

    parts: list[str] = []
    if needs_attention:
        described = ", ".join(
            f"{e['platform']} ({e['attention']})" if e["attention"] else e["platform"]
            for e in needs_attention
        )
        parts.append(f"{len(needs_attention)} credential(s) need attention: {described}")
    else:
        parts.append("no credential needs attention")
    if unverifiable:
        parts.append(
            f"{len(unverifiable)} provider(s) cannot be predicted ahead of time: "
            + ", ".join(e["platform"] for e in unverifiable)
        )

    return {
        "generated_at": moment,
        "generated_at_iso": _iso(moment),
        "reauth_horizon_days": int(reauth_horizon_days),
        "needs_attention": needs_attention,
        "unverifiable": unverifiable,
        "not_applicable": [e for e in entries if e["bucket"] == "not_applicable"],
        "ok": [e for e in entries if e["bucket"] == "ok"],
        "entries": entries,
        "ok_count": sum(1 for e in entries if e["bucket"] == "ok"),
        "attention_count": len(needs_attention),
        "summary_line": "; ".join(parts),
    }


def render_health_lines(health: Mapping[str, Any]) -> list[str]:
    """Plain-language lines for the CLI (no colour, no markup)."""
    if not health:
        return []
    lines: list[str] = []
    needs = health.get("needs_attention") or []
    horizon = health.get("reauth_horizon_days")
    if needs:
        lines.append(f"Needs attention (within {horizon} days):")
        for item in needs:
            action = item.get("action") or f"xpst connect {item['platform']}"
            lines.append(f"  - {item['platform']}: {item.get('attention')} -> {action}")
    else:
        lines.append(f"No credential needs attention within {horizon} days.")
    unverifiable = health.get("unverifiable") or []
    if unverifiable:
        names = ", ".join(str(i.get("platform")) for i in unverifiable)
        lines.append(f"Cannot warn ahead of time (no reported expiry): {names}")
    return lines
