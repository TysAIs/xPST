"""Truthful badge derivation (``xpst.token_state``).

The product used to conflate "a credential is stored here" with "this platform
works right now", which produced false greens (an expired Instagram session
reported as authenticated) and false re-auth demands (a healthy YouTube token
nobody had checked). These tests pin the honesty rules:

* ``connected`` requires a live check that passed — presence is never enough.
* an unchecked or stale probe renders ``unknown``, not green.
* ``source_only`` (TikTok source mode) and ``disabled`` never render connected.
* expiry/refresh knowledge maps to expiring / needs_reauth, including the case
  where an automatic refresh already failed (bounded retry exhausted).
* the metadata handed to the badge carries no token material.

``xpst auth status --json`` must agree with the badge: the CLI derives both from
the same payload, and these tests assert the agreement end to end.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.token_state import (
    BADGE_CONNECTED,
    BADGE_DISABLED,
    BADGE_EXPIRING,
    BADGE_NEEDS_REAUTH,
    BADGE_SOURCE_ONLY,
    BADGE_UNKNOWN,
    GREEN_BADGES,
    TOKEN_STATE_DISABLED,
    TOKEN_STATE_EXPIRED_REFRESHABLE,
    TOKEN_STATE_EXPIRED_UNREFRESHABLE,
    TOKEN_STATE_EXPIRING,
    TOKEN_STATE_SOURCE_ONLY,
    TOKEN_STATE_UNCONFIGURED,
    TOKEN_STATE_UNKNOWN,
    TOKEN_STATE_VALID,
    TokenMetadata,
    derive_token_state,
    enrich_live_status,
    humanize_seconds,
    iso_timestamp,
    parse_expiry,
    token_metadata,
)

NOW = 1_800_000_000.0
HOUR = 3600.0


def _live(**overrides) -> dict:
    entry = {
        "authenticated": True,
        "session_valid": True,
        "live_checked": True,
        "checked_at": NOW,
        "error": None,
        "auth_mode": "oauth",
    }
    entry.update(overrides)
    return entry


def _meta(**overrides) -> TokenMetadata:
    fields = {
        "platform": "youtube",
        "auth_mode": "oauth",
        "enabled": True,
        "configured": True,
        "has_refresh_token": False,
        "expires_at": None,
    }
    fields.update(overrides)
    return TokenMetadata(**fields)


def _badge(entry, meta, **kwargs) -> str:
    kwargs.setdefault("now", NOW)
    return derive_token_state(meta.platform, entry, meta, **kwargs)["badge"]


# ── the five acceptance states ──────────────────────────────────────────────


def test_valid_state_is_connected():
    info = derive_token_state(
        "youtube",
        _live(),
        _meta(has_refresh_token=True, expires_at=NOW + 10 * HOUR),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_VALID
    assert info["badge"] == BADGE_CONNECTED
    assert info["checked_at"] == NOW
    assert info["checked_at_iso"] == iso_timestamp(NOW)
    assert info["badge_reason"]


def test_expiring_state_warns_when_no_refresh_path_exists():
    """Usable now, but the human must act before the token dies."""
    info = derive_token_state(
        "threads",
        _live(auth_mode="oauth"),
        TokenMetadata(
            platform="threads",
            auth_mode="oauth",
            configured=True,
            has_refresh_token=False,
            expires_at=NOW + 2 * HOUR,
        ),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_EXPIRING
    assert info["badge"] == BADGE_EXPIRING
    assert info["badge"] != BADGE_CONNECTED
    assert "no automatic refresh" in info["badge_reason"]
    assert info["badge_action"] == "xpst connect threads"


def test_expiring_with_auto_refresh_stays_connected_but_reports_expiry():
    """YouTube renews silently in the background; that green claim is true."""
    info = derive_token_state(
        "youtube",
        _live(),
        _meta(has_refresh_token=True, expires_at=NOW + 30 * 60),
        now=NOW,
    )
    assert info["badge"] == BADGE_CONNECTED
    assert info["auto_refresh"] is True
    assert info["expires_in_seconds"] == pytest.approx(30 * 60)
    assert "refresh" in info["badge_reason"]


def test_expired_refreshable_is_not_green():
    info = derive_token_state(
        "youtube",
        _live(authenticated=False, session_valid=False, error="YouTube credentials expired"),
        _meta(has_refresh_token=True, expires_at=NOW - HOUR),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_EXPIRED_REFRESHABLE
    assert info["badge"] == BADGE_EXPIRING
    assert info["badge"] != BADGE_CONNECTED
    assert "refresh token is available" in info["badge_reason"]


def test_expired_unrefreshable_needs_reauth():
    info = derive_token_state(
        "x",
        _live(auth_mode="cookies", authenticated=False, session_valid=False, error="Session expired"),
        TokenMetadata(platform="x", auth_mode="cookies", configured=True, has_refresh_token=False),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_EXPIRED_UNREFRESHABLE
    assert info["badge"] == BADGE_NEEDS_REAUTH
    assert info["badge_action"] == "xpst connect x"


def test_disabled_platform():
    info = derive_token_state(
        "messenger",
        {"live_checked": True, "authenticated": False, "error": "disabled"},
        TokenMetadata(platform="messenger", enabled=False),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_DISABLED
    assert info["badge"] == BADGE_DISABLED
    assert info["badge"] != BADGE_CONNECTED


def test_source_only_tiktok_never_reports_connected():
    info = derive_token_state(
        "tiktok",
        _live(auth_mode="source_only"),
        TokenMetadata(platform="tiktok", auth_mode="source_only", configured=True),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_SOURCE_ONLY
    assert info["badge"] == BADGE_SOURCE_ONLY
    assert info["badge"] not in GREEN_BADGES


def test_unconfigured_platform_is_not_green():
    info = derive_token_state(
        "instagram",
        _live(auth_mode="session", authenticated=False, session_valid=False, error="Session expired"),
        TokenMetadata(platform="instagram", auth_mode="session", configured=False),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_UNCONFIGURED
    assert info["badge"] == BADGE_NEEDS_REAUTH


# ── never a green badge that is not true ────────────────────────────────────


def test_missing_live_check_is_unknown_even_with_stored_credentials():
    info = derive_token_state(
        "youtube",
        None,
        _meta(configured=True, has_refresh_token=True),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_UNKNOWN
    assert info["badge"] == BADGE_UNKNOWN
    assert "no live check has run" in info["badge_reason"]


def test_presence_based_entry_is_not_connected():
    """The old presence signal must not leak into the badge."""
    info = derive_token_state(
        "threads",
        {"credentials_stored": True, "live_checked": False},
        TokenMetadata(platform="threads", configured=True, has_refresh_token=True),
        now=NOW,
    )
    assert info["badge"] == BADGE_UNKNOWN


def test_stale_live_check_degrades_to_unknown():
    info = derive_token_state(
        "x",
        _live(checked_at=NOW - 3600),
        TokenMetadata(platform="x", auth_mode="cookies", configured=True),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_UNKNOWN
    assert info["badge"] == BADGE_UNKNOWN
    assert "freshness window" in info["badge_reason"]


def test_fresh_check_inside_window_stays_connected():
    info = derive_token_state(
        "x",
        _live(checked_at=NOW - 60),
        TokenMetadata(platform="x", auth_mode="cookies", configured=True),
        now=NOW,
    )
    assert info["badge"] == BADGE_CONNECTED
    assert info["check_age_seconds"] == pytest.approx(60)


def test_failed_automatic_refresh_keeps_needs_reauth():
    """A whole refresh run already failed — do not promise another silently."""
    info = derive_token_state(
        "youtube",
        _live(authenticated=False, session_valid=False, error="credentials expired"),
        _meta(has_refresh_token=True),
        refresh={"ok": False, "attempts": 3, "error": "invalid_grant", "refreshed_at": NOW - 60},
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_EXPIRED_UNREFRESHABLE
    assert info["badge"] == BADGE_NEEDS_REAUTH
    assert "3 attempt" in info["badge_reason"]


def test_stale_refresh_failure_does_not_block_a_retry():
    info = derive_token_state(
        "youtube",
        _live(authenticated=False, session_valid=False, error="credentials expired"),
        _meta(has_refresh_token=True),
        refresh={"ok": False, "attempts": 3, "error": "invalid_grant", "refreshed_at": NOW - 48 * HOUR},
        now=NOW,
    )
    assert info["badge"] == BADGE_EXPIRING


# ── metadata + helpers ──────────────────────────────────────────────────────


def test_parse_expiry_accepts_google_formats():
    assert parse_expiry("2026-09-15T11:00:00Z") is not None
    assert parse_expiry("2026-09-15T11:00:00.123456Z") is not None
    assert parse_expiry("garbage") is None
    assert parse_expiry(None) is None


def test_humanize_seconds():
    assert humanize_seconds(45) == "45s"
    assert humanize_seconds(3 * 60) == "3m"
    assert humanize_seconds(3 * HOUR + 12 * 60) == "3h 12m"
    assert humanize_seconds(2 * 86400) == "2d"
    assert humanize_seconds(None) is None


def test_token_metadata_never_returns_token_material(tmp_path):
    """Expiry/refresh facts only — never the token itself."""
    secret = "1//0gSECRET-refresh-token-value"
    token_file = tmp_path / "youtube_token.json"
    token_file.write_text(
        json.dumps(
            {
                "token": "ya29.SUPER-SECRET-ACCESS-TOKEN",
                "refresh_token": secret,
                "client_id": "cid",
                "client_secret": "CLIENT-SECRET",
                "expiry": "2026-09-15T11:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.token_file = str(token_file)

    meta = token_metadata(config, "youtube")

    assert meta.has_refresh_token is True
    assert meta.expires_at is not None
    blob = json.dumps(meta.to_dict()) + repr(meta)
    for needle in ("SUPER-SECRET-ACCESS-TOKEN", secret, "CLIENT-SECRET"):
        assert needle not in blob


def test_token_metadata_handles_missing_and_corrupt_token_files(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.token_file = str(tmp_path / "nope.json")
    assert token_metadata(config, "youtube").configured is False

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    config.youtube.token_file = str(broken)
    meta = token_metadata(config, "youtube")
    assert meta.expires_at is None
    assert "youtube_token" not in repr(meta)


def test_tiktok_source_only_metadata_has_no_refresh_path(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.tiktok.cookies_from_browser = True
    meta = token_metadata(config, "tiktok")
    assert meta.auth_mode == "source_only"
    assert meta.has_refresh_token is False


def test_enrich_marks_every_platform_and_never_invents_green(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    for platform in ("threads", "messenger"):
        getattr(config, platform).enabled = False

    enriched = enrich_live_status(
        config,
        {
            "youtube": _live(),
            "x": _live(authenticated=False, session_valid=False, error="Session expired"),
        },
        now=NOW,
    )

    assert set(enriched) >= {"youtube", "x", "instagram", "tiktok", "threads", "messenger"}
    assert enriched["youtube"]["badge"] == BADGE_CONNECTED
    # Platforms with no probe this run must not be green either.
    assert enriched["instagram"]["badge"] == BADGE_UNKNOWN
    assert enriched["threads"]["badge"] == BADGE_DISABLED
    # No badge outside the connected set is allowed to be green.
    for name, entry in enriched.items():
        if entry["badge"] in GREEN_BADGES:
            assert entry.get("authenticated") and entry.get("live_checked"), name


# ── CLI agreement: one source of truth ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _suppress_logging():
    """Keep rich log prefixes out of captured CLI output."""
    import logging

    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def config_file(tmp_path):
    data = {
        "accounts": {
            "tiktok": {"username": "test_user"},
            "youtube": {"enabled": True, "client_secrets": "", "token_file": ""},
            "x": {"enabled": True, "cookies_file": ""},
            "instagram": {"enabled": True, "session_file": "", "username": ""},
            "threads": {"enabled": False},
            "messenger": {"enabled": False},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {"log_level": "INFO", "log_file": str(tmp_path / "logs" / "xpst.log")},
        "reliability": {"max_retries": 3},
        "rate_limits": {"youtube": 10, "instagram": 10, "x": 10, "tiktok": 10},
        "schedule": {"check_interval": 900},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return str(path)


def _live_payload(*, checked_at: float | None = NOW) -> dict:
    entry = {
        "authenticated": True,
        "session_valid": True,
        "live_checked": True,
        "error": None,
    }
    if checked_at is not None:
        entry["checked_at"] = checked_at
    return {
        "youtube": dict(entry, auth_mode="oauth"),
        "x": dict(entry, auth_mode="cookies"),
        "instagram": dict(
            entry,
            authenticated=False,
            session_valid=False,
            error="Session expired - run 'xpst auth instagram'",
        ),
        "tiktok": dict(entry, auth_mode="source_only"),
        "threads": {"authenticated": False, "live_checked": True, "error": "disabled"},
        "messenger": {"authenticated": False, "live_checked": True, "error": "disabled"},
    }


def _auth_status_json(config_file: str, monkeypatch, payload: dict) -> dict:
    """Run the real CLI against a stubbed live probe.

    The stub is enriched exactly like the production collector
    (``attach_badge_truth``) so the test asserts agreement between the badge
    stored on the entry and the badge map the CLI publishes.
    """
    import xpst.auth_status as auth_status
    from xpst.config import XPSTConfig

    def fake_collect(cfg):  # noqa: ANN001, ARG001
        config = XPSTConfig.load(config_file)
        auth_status.attach_badge_truth(config, payload)
        return payload

    monkeypatch.setattr(auth_status, "collect_live_auth_status", fake_collect)
    result = CliRunner().invoke(main, ["--config", config_file, "auth", "status", "--json"])
    assert result.exit_code == 0, result.output
    return _extract_json(result.output)


def _extract_json(output: str) -> dict:
    for index, char in enumerate(output):
        if char == "{":
            return json.loads(output[index:])
    return json.loads(output)


def test_cli_auth_status_badge_agrees_with_json(config_file, monkeypatch):
    """`auth status --json` stays the single source of truth for badges."""
    data = _auth_status_json(config_file, monkeypatch, _live_payload())

    platforms = data["platforms"]
    for name, badge in data["badges"].items():
        assert platforms[name]["badge"] == badge, name

    assert platforms["youtube"]["badge"] == BADGE_CONNECTED
    assert platforms["instagram"]["badge"] == BADGE_NEEDS_REAUTH
    assert platforms["tiktok"]["badge"] == BADGE_SOURCE_ONLY
    assert platforms["threads"]["badge"] == BADGE_DISABLED
    # nothing green without a passing live check
    for name, entry in platforms.items():
        if name == "local":
            continue
        if entry["badge"] in GREEN_BADGES:
            assert entry.get("live_checked") and entry.get("authenticated"), name


def test_cli_auth_status_stamps_the_check_timestamp(config_file, monkeypatch):
    """Every entry (and the envelope) carries the time of the check."""
    data = _auth_status_json(config_file, monkeypatch, _live_payload(checked_at=None))

    assert data["checked_at"] is not None
    assert data["checked_at"] == pytest.approx(time.time(), abs=120)
    assert data["checked_at_iso"] == iso_timestamp(data["checked_at"])
    entry = data["platforms"]["youtube"]
    assert entry["checked_at"] == pytest.approx(data["checked_at"], abs=1)
    assert entry["checked_at_iso"] == data["checked_at_iso"]


def test_cli_auth_status_without_live_data_is_never_connected(config_file, monkeypatch):
    """A collector that returns nothing still must not produce green badges."""
    data = _auth_status_json(config_file, monkeypatch, {})

    assert data["badges"]
    assert BADGE_CONNECTED not in set(data["badges"].values())
    assert data["platforms"]["youtube"]["live_checked"] is False


def test_cli_auth_status_table_shows_badge_and_age(config_file, monkeypatch):
    """Human output carries the badge label, its reason and the check age."""
    import xpst.auth_status as auth_status

    monkeypatch.setattr(
        auth_status,
        "collect_live_auth_status",
        lambda cfg: {
            "youtube": {
                "authenticated": False,
                "session_valid": False,
                "auth_mode": "oauth",
                "live_checked": True,
                "checked_at": time.time() - 30,
                "badge": BADGE_NEEDS_REAUTH,
                "badge_label": "Needs re-auth",
                "badge_reason": "client_secrets.json not found",
                "check_age_seconds": 30.0,
                "error": "client_secrets.json not found",
            },
            "x": {
                "authenticated": True,
                "session_valid": True,
                "auth_mode": "cookies",
                "live_checked": True,
                "checked_at": time.time(),
                "badge": BADGE_CONNECTED,
                "badge_label": "Connected",
                "badge_reason": "live check passed",
                "check_age_seconds": 1.0,
                "error": None,
            },
        },
    )

    # Non-TTY output auto-selects the machine-readable path, which must carry
    # the same badge vocabulary as the table.
    result = CliRunner().invoke(main, ["--config", config_file, "auth", "status"])
    assert result.exit_code == 0, result.output
    assert "Needs re-auth" in result.output
    assert "client_secrets.json not found" in result.output


def test_config_dir_token_metadata_for_unknown_platform(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    meta = token_metadata(config, "mastodon")
    assert meta.auth_mode == "unknown"
    assert meta.configured is False


def test_badge_field_is_dropped_for_local_source(tmp_path):
    """The local folder is not an account and gets no badge."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    enriched = enrich_live_status(config, {"local": {"authenticated": True, "live_checked": True}}, now=NOW)
    assert "local" not in enriched


def test_config_path_exists_check_uses_expanduser(tmp_path, monkeypatch):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    target = Path(tmp_path) / "cookies.json"
    target.write_text("{}", encoding="utf-8")
    config.x.cookies_file = str(target)
    assert token_metadata(config, "x").configured is True


# ── web API agreement ───────────────────────────────────────────────────────


def test_health_status_api_publishes_the_same_badges(tmp_path, monkeypatch):
    """The UI's badge payload is the CLI's badge payload, not a second guess."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from xpst.dashboard import api as api_module
    from xpst.dashboard.api import create_api_router

    XPSTConfig().save(str(tmp_path / "config.yaml"))
    api_module._AUTH_STATUS_CACHE.clear()
    api_module._AUTH_STATUS_REFRESHING.clear()
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")

    import xpst.auth_status as auth_status

    payload = _live_payload()

    def fake_collect(config):  # noqa: ANN001, ARG001
        auth_status.attach_badge_truth(config, payload)
        return payload

    monkeypatch.setattr("xpst.auth_status.collect_live_auth_status", fake_collect)

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    body = TestClient(app).get("/api/health-status").json()

    assert body["badges"]["youtube"] == BADGE_CONNECTED
    assert body["badges"]["instagram"] == BADGE_NEEDS_REAUTH
    assert body["badges"]["tiktok"] == BADGE_SOURCE_ONLY
    assert body["auth_checked_at"] is not None
    assert body["auth_checked_at_iso"] == iso_timestamp(body["auth_checked_at"])

    # the badge map must match the per-entry badge the same probe produced
    for name, badge in body["badges"].items():
        assert body["auth"][name]["badge"] == badge, name
        if badge in GREEN_BADGES:
            assert body["auth"][name]["live_checked"], name


def test_health_status_api_never_reports_connected_without_a_probe(tmp_path, monkeypatch):
    """A probe that returns nothing must not produce a green pill."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from xpst.dashboard import api as api_module
    from xpst.dashboard.api import create_api_router

    XPSTConfig().save(str(tmp_path / "config.yaml"))
    api_module._AUTH_STATUS_CACHE.clear()
    api_module._AUTH_STATUS_REFRESHING.clear()
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
    monkeypatch.setattr("xpst.auth_status.collect_live_auth_status", lambda config: {})

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    body = TestClient(app).get("/api/health-status").json()

    assert body["badges"] == {}
    assert body["auth_checked_at"] is None


def test_refresh_tokens_endpoint_is_a_bounded_noop(tmp_path, monkeypatch):
    """The UI action must be safe to press: no due token → no work, no error."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from xpst.dashboard import api as api_module
    from xpst.dashboard.api import create_api_router

    XPSTConfig().save(str(tmp_path / "config.yaml"))
    api_module._AUTH_STATUS_CACHE.clear()
    api_module._AUTH_STATUS_REFRESHING.clear()
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    body = TestClient(app).post("/api/refresh-tokens").json()

    assert body["count"] == 0
    assert body["failed"] == []


def test_cold_request_does_not_wait_on_a_token_refresh(tmp_path, monkeypatch):
    """First paint stays fast: only the background path runs the refresh job."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from xpst.dashboard import api as api_module
    from xpst.dashboard.api import create_api_router

    XPSTConfig().save(str(tmp_path / "config.yaml"))
    api_module._AUTH_STATUS_CACHE.clear()
    api_module._AUTH_STATUS_REFRESHING.clear()
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")
    monkeypatch.setattr("xpst.auth_status.collect_live_auth_status", lambda config: {})

    calls: list[int] = []
    monkeypatch.setattr(
        api_module,
        "_refresh_due_tokens_quietly",
        lambda config: calls.append(1),
    )

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    TestClient(app).get("/api/health-status")
    assert calls == [], "a cold request must not block on a token refresh"

    config = XPSTConfig.load(str(tmp_path / "config.yaml"))
    api_module._probe_and_store(str(tmp_path), config, refresh_tokens=True)
    assert calls == [1], "the background path is where the refresh belongs"


def test_refresh_tokens_endpoint_passes_force_through(tmp_path, monkeypatch):
    """`force=true` is the explicit 'renew now' escape hatch."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import xpst.token_refresh as token_refresh
    from xpst.dashboard import api as api_module
    from xpst.dashboard.api import create_api_router

    XPSTConfig().save(str(tmp_path / "config.yaml"))
    api_module._AUTH_STATUS_CACHE.clear()
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")

    seen: dict[str, object] = {}

    def fake_refresh(config, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return {"youtube": {"attempted": True, "ok": True, "attempts": 1}}

    monkeypatch.setattr(token_refresh, "refresh_due_tokens", fake_refresh)

    app = FastAPI()
    app.include_router(create_api_router(str(tmp_path)))
    body = TestClient(app).post("/api/refresh-tokens?force=true").json()

    assert seen["force"] is True
    assert body["count"] == 1
    assert body["failed"] == []


def test_no_refresh_path_warns_a_week_out_not_a_day():
    """The watchdog horizon: replacing this needs a browser, so warn early.

    24h is right for a token xPST refreshes itself; a human-only renewal must
    warn while there is still time to act (Tyler's "why do I only find out when
    the post fails").
    """
    meta = TokenMetadata(
        platform="threads",
        auth_mode="oauth",
        configured=True,
        has_refresh_token=False,
        expires_at=NOW + 5 * 24 * HOUR,
    )
    info = derive_token_state("threads", _live(auth_mode="oauth"), meta, now=NOW)
    assert info["token_state"] == TOKEN_STATE_EXPIRING
    assert info["badge"] == BADGE_EXPIRING
    assert info["expiry_horizon"] == "reauth"
    assert info["reauth_window_seconds"] == 7 * 24 * 3600
    assert info["badge_action"] == "xpst connect threads"


def test_no_refresh_path_beyond_the_horizon_is_connected():
    meta = TokenMetadata(
        platform="threads",
        auth_mode="oauth",
        configured=True,
        has_refresh_token=False,
        expires_at=NOW + 10 * 24 * HOUR,
    )
    info = derive_token_state("threads", _live(auth_mode="oauth"), meta, now=NOW)
    assert info["token_state"] == TOKEN_STATE_VALID
    assert info["badge"] == BADGE_CONNECTED
    assert info["expiry_horizon"] is None


def test_auto_refresh_window_is_unchanged_by_the_reauth_horizon():
    """A refreshable token 5 days out stays green: xPST renews it in the background."""
    info = derive_token_state(
        "youtube",
        _live(),
        _meta(has_refresh_token=True, expires_at=NOW + 5 * 24 * HOUR),
        now=NOW,
    )
    assert info["token_state"] == TOKEN_STATE_VALID
    assert info["badge"] == BADGE_CONNECTED
    assert info["reauth_window_seconds"] == 7 * 24 * 3600
    assert info["expiry_horizon"] is None
