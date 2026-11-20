"""Credential-health watchdog: warns before a credential dies, never invents a date."""

from __future__ import annotations

import time

from xpst.credential_health import (
    ATTENTION_HORIZON_DAYS,
    build_credential_health,
    render_health_lines,
)
from xpst.token_state import (
    BADGE_CONNECTED,
    BADGE_EXPIRING,
    BADGE_NEEDS_REAUTH,
    BADGE_SOURCE_ONLY,
    TOKEN_STATE_DISABLED,
    TOKEN_STATE_EXPIRED_UNREFRESHABLE,
    TOKEN_STATE_EXPIRING,
    TOKEN_STATE_SOURCE_ONLY,
    TOKEN_STATE_UNKNOWN,
    TOKEN_STATE_VALID,
)

NOW = 1_700_000_000.0
DAY = 86400.0


def _entry(**kw) -> dict:
    base = {
        "token_state": TOKEN_STATE_VALID,
        "badge": BADGE_CONNECTED,
        "badge_reason": "live check passed",
        "badge_action": None,
        "auto_refresh": False,
        "expires_at": None,
        "expires_in_seconds": None,
    }
    base.update(kw)
    return base


def _by_platform(health: dict, platform: str) -> dict:
    return next(e for e in health["entries"] if e["platform"] == platform)


# ── the case the watchdog exists for ────────────────────────────────────────


def test_human_only_credential_warns_days_ahead_not_hours():
    """A no-refresh token 5 days out is 'needs attention', with a real deadline."""
    health = build_credential_health(
        {
            "threads": _entry(
                token_state=TOKEN_STATE_EXPIRING,
                badge=BADGE_EXPIRING,
                badge_action="xpst connect threads",
                auto_refresh=False,
                expires_at=NOW + 5 * DAY,
                expires_in_seconds=5 * DAY,
            )
        },
        now=NOW,
    )
    item = health["needs_attention"][0]
    assert item["platform"] == "threads"
    assert item["days_left"] == 5.0
    assert item["action"] == "xpst connect threads"
    assert "no automatic refresh" in item["attention"]
    assert health["attention_count"] == 1


def test_auto_refreshable_credential_is_not_nagged():
    """YouTube renews itself: an impending expiry is information, not a task."""
    health = build_credential_health(
        {
            "youtube": _entry(
                auto_refresh=True,
                expires_at=NOW + 5 * DAY,
                expires_in_seconds=5 * DAY,
            )
        },
        now=NOW,
    )
    assert health["needs_attention"] == []
    item = _by_platform(health, "youtube")
    assert item["bucket"] == "ok"
    assert "refreshes it automatically" in item["attention"]


def test_expired_unrefreshable_needs_attention_now():
    health = build_credential_health(
        {
            "instagram": _entry(
                token_state=TOKEN_STATE_EXPIRED_UNREFRESHABLE,
                badge=BADGE_NEEDS_REAUTH,
                badge_action="xpst connect instagram",
            )
        },
        now=NOW,
    )
    assert health["needs_attention"][0]["platform"] == "instagram"
    assert "reconnect now" in health["needs_attention"][0]["attention"]
    assert "instagram" in health["summary_line"]


# ── honesty rules ───────────────────────────────────────────────────────────


def test_no_reported_expiry_is_unverifiable_never_a_guess():
    """instagrapi sessions report no expiry: say so, do not invent a date."""
    health = build_credential_health({"instagram": _entry(auto_refresh=False)}, now=NOW)
    item = _by_platform(health, "instagram")
    assert item["bucket"] == "unverifiable"
    assert item["days_left"] is None
    assert item["expires_at"] is None
    assert health["needs_attention"] == []
    assert "cannot be predicted ahead of time" in health["summary_line"] or (
        "Cannot" in " ".join(render_health_lines(health))
    )


def test_unproven_credential_is_unverifiable_not_healthy():
    """No live check has run: never green, and not a fabricated deadline either."""
    health = build_credential_health(
        {"x": _entry(token_state=TOKEN_STATE_UNKNOWN, badge="unknown")}, now=NOW
    )
    item = _by_platform(health, "x")
    assert item["bucket"] == "unverifiable"
    assert health["ok_count"] == 0
    assert health["attention_count"] == 0


def test_disabled_and_source_only_are_never_nagged():
    health = build_credential_health(
        {
            "threads": _entry(token_state=TOKEN_STATE_DISABLED, badge="disabled"),
            "tiktok": _entry(token_state=TOKEN_STATE_SOURCE_ONLY, badge=BADGE_SOURCE_ONLY),
        },
        now=NOW,
    )
    assert health["needs_attention"] == []
    assert health["unverifiable"] == []
    assert {e["platform"] for e in health["not_applicable"]} == {"threads", "tiktok"}


def test_missing_and_malformed_entries_do_not_crash_or_go_green():
    """A known platform with junk state is unverifiable; unknown names are not accounts."""
    health = build_credential_health(
        {
            "x": _entry(token_state="something_new", badge=""),
            "threads": {},
            "local": _entry(token_state=TOKEN_STATE_UNKNOWN, badge=""),
        },
        now=NOW,
    )
    assert health["ok_count"] == 0
    assert {e["platform"] for e in health["unverifiable"]} == {"x", "threads"}
    assert {e["platform"] for e in health["not_applicable"]} == {"local"}
    assert health["needs_attention"] == []


def test_non_account_surfaces_are_not_reported_as_credentials():
    """The local file source has nothing to renew: never a maintenance item."""
    health = build_credential_health(
        {
            "local": _entry(token_state=TOKEN_STATE_UNKNOWN),
            "youtube": _entry(
                auto_refresh=True,
                expires_at=NOW + 30 * DAY,
                expires_in_seconds=30 * DAY,
            ),
        },
        now=NOW,
    )
    assert [e["platform"] for e in health["not_applicable"]] == ["local"]
    assert health["unverifiable"] == []
    assert health["ok_count"] == 1


def test_none_input_is_an_empty_honest_report():
    health = build_credential_health(None, now=NOW)
    assert health["entries"] == []
    assert health["attention_count"] == 0
    assert health["reauth_horizon_days"] == ATTENTION_HORIZON_DAYS
    assert "no credential needs attention" in health["summary_line"]


# ── ordering and serialisation ──────────────────────────────────────────────


def test_attention_is_ordered_by_urgency_then_platform():
    health = build_credential_health(
        {
            "x": _entry(
                token_state=TOKEN_STATE_EXPIRING,
                badge=BADGE_EXPIRING,
                expires_at=NOW + 6 * DAY,
                expires_in_seconds=6 * DAY,
            ),
            "threads": _entry(
                token_state=TOKEN_STATE_EXPIRING,
                badge=BADGE_EXPIRING,
                expires_at=NOW + 2 * DAY,
                expires_in_seconds=2 * DAY,
            ),
            "instagram": _entry(
                token_state=TOKEN_STATE_EXPIRED_UNREFRESHABLE,
                badge=BADGE_NEEDS_REAUTH,
            ),
        },
        now=NOW,
    )
    order = [e["platform"] for e in health["needs_attention"]]
    assert order == ["threads", "x", "instagram"]  # soonest deadline, then no-deadline


def test_expires_in_is_derived_from_expires_at_when_missing():
    health = build_credential_health(
        {
            "threads": _entry(
                token_state=TOKEN_STATE_EXPIRING,
                badge=BADGE_EXPIRING,
                expires_at=NOW + 3 * DAY + 6 * 3600,
                expires_in_seconds=None,
            )
        },
        now=NOW,
    )
    item = _by_platform(health, "threads")
    assert item["days_left"] == 3.25
    assert item["expires_at_iso"].endswith("Z")


def test_report_is_json_serialisable_and_carries_no_callables():
    import json

    health = build_credential_health({"youtube": _entry(auto_refresh=True)}, now=NOW)
    blob = json.dumps(health)
    assert "credential" in blob
    assert health["generated_at_iso"] is not None


def test_render_lines_show_action_for_attention_and_names_the_blind_spots():
    health = build_credential_health(
        {
            "threads": _entry(
                token_state=TOKEN_STATE_EXPIRING,
                badge=BADGE_EXPIRING,
                badge_action="xpst connect threads",
                expires_at=NOW + 1 * DAY,
                expires_in_seconds=1 * DAY,
            ),
            "instagram": _entry(),
        },
        now=NOW,
    )
    lines = "\n".join(render_health_lines(health))
    assert "Needs attention (within 7 days)" in lines
    assert "xpst connect threads" in lines
    assert "instagram" in lines


def test_render_lines_are_empty_for_an_empty_report():
    assert render_health_lines({}) == []


def test_clock_skew_never_produces_a_negative_horizon():
    health = build_credential_health(
        {
            "threads": _entry(
                token_state=TOKEN_STATE_EXPIRING,
                badge=BADGE_EXPIRING,
                expires_at=NOW - 3600,
                expires_in_seconds=-3600,
            )
        },
        now=NOW,
    )
    item = _by_platform(health, "threads")
    assert item["days_left"] < 0
    assert "no automatic refresh" in item["attention"]


def test_watchdog_adds_no_elapsed_time_beyond_the_call():
    """Pure function: the report must not sleep or probe."""
    started = time.time()
    build_credential_health({"youtube": _entry()}, now=time.time())
    assert time.time() - started < 1.0
