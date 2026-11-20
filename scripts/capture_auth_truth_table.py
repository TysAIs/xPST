#!/usr/bin/env python3
"""Capture the auth/readiness truth table from the Python engine (P1).

This script RUNS the existing Python implementation — it does not restate its
rules — against synthetic, credential-less scratch config directories and
writes what it observed to ``tests/fixtures/auth_truth_table.json``.  The Rust
``xpst-auth`` crate asserts every row of that fixture, so the two engines cannot
drift silently.

Guarantees, enforced by construction:

* ``XPST_CONFIG_DIR`` points at a throwaway directory under the system temp
  dir.  ``~/.xpst`` is never read, written or inferred from.
* No provider network API is touched.  The three functions used here —
  :func:`xpst.provider_truth.build_canonical_status`,
  :func:`xpst.token_state.enrich_live_status` and
  :func:`xpst.auth_path_risk.auth_path_risks` — are the deterministic mapping
  layer, not the live probes.  Rows that need a probe verdict inject a
  SYNTHETIC probe result and are marked ``unverified_no_credentials``: the
  state transition is captured, the observation against a real account is not.
* Credential files are written from fictional identifiers only.

Usage::

    XPST_PYTHON=.venv/bin/python \\
      PYTHONPATH=src .venv/bin/python scripts/capture_auth_truth_table.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "auth_truth_table.json"

#: Fixed clock so every expiry/age in the fixture is deterministic.
NOW = 1_700_000_000.0

FICTIONAL_YOUTUBE_TOKEN = {
    "token": "fictional-access-token",
    "refresh_token": "fictional-refresh-token",
    "token_uri": "https://oauth2.example.invalid/token",
    "client_id": "fictional-client-id.apps.example.invalid",
    "client_secret": "fictional-client-secret",
    "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
    "expiry": "2024-01-13T22:13:20Z",
}

FICTIONAL_YOUTUBE_TOKEN_NO_REFRESH = {
    "token": "fictional-access-token",
    "token_uri": "https://oauth2.example.invalid/token",
    "client_id": "fictional-client-id.apps.example.invalid",
    "expiry": "2024-01-13T22:13:20Z",
}

FICTIONAL_COOKIES = (
    "# Netscape HTTP Cookie File\n.example.invalid\tTRUE\t/\tTRUE\t1893456000\tauth_token\tfictional-auth-token\n"
)

FICTIONAL_INSTAGRAM_SESSION = {
    "sessionid": "fictional-sessionid",
    "ds_user_id": "1000000001",
    "username": "fictional_creator",
}


def _iso(offset_seconds: float) -> str:
    """RFC3339 UTC timestamp ``offset_seconds`` after the fixed clock."""
    return datetime.fromtimestamp(NOW + offset_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _scenario(
    row_id: str,
    platform: str,
    *,
    accounts: dict[str, dict[str, Any]] | None = None,
    files: dict[str, Any] | None = None,
    live: dict[str, Any] | None = None,
    source_live: dict[str, Any] | None = None,
    destination_live: dict[str, Any] | None = None,
    badge_checked_at: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "platform": platform,
        "accounts": accounts or {},
        "files": files or {},
        "live": live,
        "source_live": source_live,
        "destination_live": destination_live,
        "badge_checked_at": badge_checked_at,
        "note": note,
    }


def _live(
    *,
    authenticated: bool,
    error: str | None = None,
    live_checked: bool = True,
    checked_at: float | None = None,
    details: dict[str, Any] | None = None,
    auth_mode: str | None = None,
) -> dict[str, Any]:
    """One uploader ``check_health()`` result, in the shape auth_status emits."""
    entry: dict[str, Any] = {
        "authenticated": authenticated,
        "session_valid": authenticated,
        "live_checked": live_checked,
        "error": error,
        "details": details or {},
    }
    if auth_mode is not None:
        entry["auth_mode"] = auth_mode
    if checked_at is not None:
        entry["checked_at"] = checked_at
    return entry


SCENARIOS: list[dict[str, Any]] = [
    # ── config-only captures: nothing configured, nothing enabled ──────────
    _scenario(
        "default/youtube",
        "youtube",
        note="fresh config: YouTube is enabled but has no token file",
    ),
    _scenario(
        "default/x",
        "x",
        note="fresh config: X defaults to the unofficial cookies path",
    ),
    _scenario(
        "default/instagram",
        "instagram",
        note="fresh config: instagram reports the configured graph_api mode",
    ),
    _scenario(
        "default/tiktok",
        "tiktok",
        note="fresh config: no posting creds, so source_only is the effective mode",
    ),
    _scenario(
        "default/bluesky",
        "bluesky",
        note="bluesky is opt-in and disabled by default",
    ),
    _scenario(
        "default/threads",
        "threads",
        note="threads is disabled by default",
    ),
    _scenario(
        "default/local",
        "local",
        note="local source has no path configured",
    ),
    # ── config-only captures: credential presence changes the verdict ──────
    _scenario(
        "youtube-token-file-present",
        "youtube",
        accounts={"youtube": {"enabled": True, "token_file": "credentials/yt_token.json"}},
        files={"credentials/yt_token.json": FICTIONAL_YOUTUBE_TOKEN},
        note="a stored credential is configured data, not proof of a live session",
    ),
    _scenario(
        "tiktok-source-username",
        "tiktok",
        accounts={"tiktok": {"enabled": True, "username": "fictional_creator"}},
        note="source role configured, destination role not configured",
    ),
    _scenario(
        "tiktok-content-posting-api",
        "tiktok",
        accounts={
            "tiktok": {
                "enabled": True,
                "client_key": "fictional-client-key",
                "client_secret": "fictional-client-secret",
                "access_token": "fictional-access-token",
                "refresh_token": "fictional-refresh-token",
            }
        },
        note="Content Posting API credentials select the posting route",
    ),
    _scenario(
        "tiktok-sandbox",
        "tiktok",
        accounts={
            "tiktok": {
                "enabled": True,
                "sandbox": True,
                "client_key": "fictional-client-key",
                "access_token": "fictional-access-token",
            }
        },
        note="sandbox app cannot publish publicly",
    ),
    _scenario(
        "instagram-session-enabled-no-file",
        "instagram",
        accounts={"instagram": {"enabled": True, "auth_mode": "session"}},
        note="unofficial private-API path raises the high-severity ban risk",
    ),
    _scenario(
        "instagram-graph-no-token",
        "instagram",
        accounts={"instagram": {"enabled": True, "auth_mode": "graph_api"}},
        note="official path selected but no token supplied",
    ),
    _scenario(
        "x-api-v2-bearer",
        "x",
        accounts={"x": {"enabled": True, "auth_mode": "api_v2", "bearer_token": "fictional-bearer"}},
        note="official X API v2 path: no ban-risk advisory",
    ),
    _scenario(
        "x-cookies-missing-file",
        "x",
        accounts={"x": {"enabled": True, "auth_mode": "cookies", "cookies_file": "credentials/x.json"}},
        note="cookies path selected, cookie jar absent",
    ),
    _scenario(
        "facebook-page-id-only",
        "facebook",
        accounts={"facebook": {"enabled": True, "page_id": "100000000000001"}},
        note="half-configured Page is not configured",
    ),
    _scenario(
        "threads-token-no-user",
        "threads",
        accounts={"threads": {"enabled": True, "graph_access_token": "fictional-threads-token"}},
        note="a Threads token without the user id is not configured",
    ),
    _scenario(
        "local-path-present",
        "local",
        accounts={"local": {"path": "media"}},
        files={"media/.keep": ""},
        note="local source path exists",
    ),
    _scenario(
        "all-disabled/tiktok",
        "tiktok",
        accounts={"tiktok": {"enabled": False, "username": "fictional_creator"}},
        note="disabled in config wins over a configured source",
    ),
    # ── probe-injected captures: the state TRANSITIONS for live verdicts ───
    _scenario(
        "youtube-live-ok",
        "youtube",
        accounts={"youtube": {"enabled": True, "token_file": "credentials/yt_token.json"}},
        files={"credentials/yt_token.json": FICTIONAL_YOUTUBE_TOKEN},
        live=_live(authenticated=True, checked_at=NOW),
        note="live probe passed",
    ),
    _scenario(
        "youtube-live-expired-refreshable",
        "youtube",
        accounts={"youtube": {"enabled": True, "token_file": "credentials/yt_token.json"}},
        files={"credentials/yt_token.json": FICTIONAL_YOUTUBE_TOKEN},
        live=_live(
            authenticated=False,
            error="invalid_grant: Token has been expired or revoked",
            checked_at=NOW,
        ),
        note="expired access token with a stored refresh token",
    ),
    _scenario(
        "youtube-live-expired-no-refresh",
        "youtube",
        accounts={"youtube": {"enabled": True, "token_file": "credentials/yt_token.json"}},
        files={"credentials/yt_token.json": FICTIONAL_YOUTUBE_TOKEN_NO_REFRESH},
        live=_live(
            authenticated=False,
            error="invalid_grant: Token has been expired or revoked",
            checked_at=NOW,
        ),
        note="expired access token with no refresh path",
    ),
    _scenario(
        "youtube-live-expiring-no-refresh",
        "youtube",
        accounts={"youtube": {"enabled": True, "token_file": "credentials/yt_token.json"}},
        files={
            "credentials/yt_token.json": {
                **FICTIONAL_YOUTUBE_TOKEN_NO_REFRESH,
                "expiry": _iso(3 * 86400),
            }
        },
        live=_live(authenticated=True, checked_at=NOW),
        note="usable now, human must renew before it dies",
    ),
    _scenario(
        "youtube-live-stale-check",
        "youtube",
        accounts={"youtube": {"enabled": True, "token_file": "credentials/yt_token.json"}},
        files={"credentials/yt_token.json": FICTIONAL_YOUTUBE_TOKEN},
        live=_live(authenticated=True, checked_at=NOW),
        badge_checked_at=NOW - 3600,
        note="a check older than the freshness window may not claim green",
    ),
    _scenario(
        "x-live-invalid-cookies",
        "x",
        accounts={"x": {"enabled": True, "auth_mode": "cookies", "cookies_file": "credentials/x.json"}},
        files={"credentials/x.json": FICTIONAL_COOKIES},
        live=_live(authenticated=False, error="cookies expired: login required", checked_at=NOW),
        note="replayed web session rejected by the provider",
    ),
    _scenario(
        "x-live-unverified-probe",
        "x",
        accounts={"x": {"enabled": True, "auth_mode": "cookies", "cookies_file": "credentials/x.json"}},
        files={"credentials/x.json": FICTIONAL_COOKIES},
        live=_live(
            authenticated=False,
            error="http 429 rate limited",
            checked_at=NOW,
            details={"probe_class": "unverified"},
        ),
        note="probe failed without proving the credential dead",
    ),
    _scenario(
        "instagram-graph-live-ok",
        "instagram",
        accounts={
            "instagram": {
                "enabled": True,
                "auth_mode": "graph_api",
                "graph_access_token": "fictional-graph-token",
            }
        },
        live=_live(authenticated=True, checked_at=NOW),
        note="official Meta Graph API probe passed",
    ),
    _scenario(
        "instagram-session-live-invalid",
        "instagram",
        accounts={
            "instagram": {
                "enabled": True,
                "auth_mode": "session",
                "session_file": "credentials/ig_session.json",
                "username": "fictional_creator",
            }
        },
        files={"credentials/ig_session.json": FICTIONAL_INSTAGRAM_SESSION},
        live=_live(
            authenticated=False,
            error="session expired: login_required",
            checked_at=NOW,
        ),
        note="instagram session invalidated server-side",
    ),
    _scenario(
        "tiktok-live-source-ok",
        "tiktok",
        accounts={"tiktok": {"enabled": True, "username": "fictional_creator"}},
        source_live=_live(authenticated=True, checked_at=NOW, auth_mode="source_only"),
        destination_live=_live(
            authenticated=False,
            error="TikTok Content Posting API is not configured",
            checked_at=NOW,
            auth_mode="source_only",
        ),
        note="download side works, posting side does not exist",
    ),
]


def _facts(cfg: Any, platform: str, live_entry: dict[str, Any] | None) -> dict[str, Any]:
    """Credential-presence facts, read from the loaded config + filesystem.

    Every boolean is the same predicate the Python engine itself uses
    (``Path(...).exists()``), so the fixture records observations rather than
    assumptions.
    """
    from xpst.token_state import token_metadata

    account = getattr(cfg, platform, None)

    def _path_exists(value: Any) -> bool:
        return bool(value and Path(str(value)).expanduser().exists())

    def _flag(name: str) -> bool:
        return bool(getattr(account, name, "") or False)

    meta = token_metadata(cfg, platform)
    token_document: dict[str, Any] = {}
    if platform == "youtube":
        token_file = getattr(account, "token_file", "") or ""
        if _path_exists(token_file):
            try:
                token_document = json.loads(Path(str(token_file)).expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                token_document = {}

    return {
        "platform": platform,
        "enabled": True if platform == "local" else bool(getattr(account, "enabled", False)),
        "auth_mode": str(getattr(account, "auth_mode", "") or ""),
        "publish_mode": str(getattr(account, "publish_mode", "auto") or "auto"),
        "sandbox": bool(getattr(account, "sandbox", False)),
        "token_file_exists": _path_exists(getattr(account, "token_file", "")),
        "token_document_present": bool(token_document),
        "cookies_file_exists": _path_exists(getattr(account, "cookies_file", "")),
        "session_file_exists": _path_exists(getattr(account, "session_file", "")),
        "local_path_exists": _path_exists(getattr(account, "path", "")),
        "browser_jar_exists": _path_exists(
            getattr(account, "cookies_file", "")
            or str(Path(str(cfg.config_dir)).expanduser() / "credentials" / "tiktok_cookies.txt")
        ),
        "cookies_from_browser": bool(getattr(account, "cookies_from_browser", False)),
        "username": _flag("username"),
        "identifier": _flag("identifier"),
        "client_key": _flag("client_key"),
        "access_token": _flag("access_token"),
        "refresh_token": _flag("refresh_token"),
        "api_key": _flag("api_key"),
        "api_secret": _flag("api_secret"),
        "access_token_secret": _flag("access_token_secret"),
        "bearer_token": _flag("bearer_token"),
        "graph_access_token": _flag("graph_access_token"),
        "threads_user_id": _flag("threads_user_id"),
        "page_id": _flag("page_id"),
        "page_access_token": _flag("page_access_token"),
        "credentials_stored": bool((live_entry or {}).get("credentials_stored")),
        "token_configured": bool(meta.configured),
        "has_refresh_token": bool(meta.has_refresh_token),
        "expires_at": meta.expires_at,
    }


def _run_scenario(scenario: dict[str, Any], root: Path) -> dict[str, Any]:
    """Materialise one scratch profile and read the engine's answer from it."""
    from xpst.auth_path_risk import auth_path_risks
    from xpst.config import XPSTConfig
    from xpst.provider_truth import build_canonical_status, posting_truth
    from xpst.token_state import enrich_live_status

    scratch = root / scenario["id"].replace("/", "__")
    scratch.mkdir(parents=True, exist_ok=True)
    for relative, payload in scenario["files"].items():
        target = scratch / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, (dict, list)):
            target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        else:
            target.write_text(str(payload), encoding="utf-8")

    config_document: dict[str, Any] = {}
    if scenario["accounts"]:
        # Files are addressed relative to the scratch profile, exactly as a
        # real install addresses them relative to ~/.xpst.
        accounts: dict[str, Any] = {}
        for name, section in scenario["accounts"].items():
            resolved = {}
            for key, value in section.items():
                if key in {"token_file", "cookies_file", "session_file", "path"} and isinstance(value, str):
                    resolved[key] = str(scratch / value) if value else value
                else:
                    resolved[key] = value
            accounts[name] = resolved
        config_document["accounts"] = accounts
    if config_document:
        import yaml

        (scratch / "config.yaml").write_text(yaml.safe_dump(config_document, sort_keys=True), encoding="utf-8")

    os.environ["XPST_CONFIG_DIR"] = str(scratch)
    cfg = XPSTConfig.load()
    config_dir = Path(str(cfg.config_dir)).expanduser().resolve()
    if config_dir != scratch.resolve() and scratch.resolve() not in config_dir.parents:
        raise SystemExit(f"refusing to run: {config_dir} is not the scratch profile {scratch}")

    platform = scenario["platform"]
    live_entry: dict[str, Any] | None = None
    live_status: dict[str, Any] = {}
    if scenario["live"]:
        live_entry = dict(scenario["live"])
        live_status[platform] = live_entry
    if scenario["source_live"] or scenario["destination_live"]:
        live_status[platform] = live_entry = {
            "live_checked": True,
            "error": None,
            "details": {},
        }
        if scenario["source_live"]:
            live_entry["source_check"] = dict(scenario["source_live"])
        if scenario["destination_live"]:
            live_entry["destination_check"] = dict(scenario["destination_live"])
    canonical = build_canonical_status(cfg, live_status or None)
    entry = canonical[platform]
    posting = posting_truth(entry)

    badge_entry: dict[str, Any] | None = None
    badge_checked_at: float | None = None
    if platform in {
        "youtube",
        "x",
        "instagram",
        "bluesky",
        "tiktok",
        "threads",
        "facebook",
        "messenger",
    }:
        badge_source = dict(entry)
        if scenario["badge_checked_at"] is not None:
            badge_checked_at = scenario["badge_checked_at"]
        elif live_entry is not None and live_entry.get("live_checked"):
            # auth_status.attach_badge_truth stamps the moment the probe ran;
            # this capture's clock is NOW.
            badge_checked_at = NOW
        if badge_checked_at is not None:
            badge_source["checked_at"] = badge_checked_at
        badge_entry = enrich_live_status(cfg, {platform: badge_source}, now=NOW).get(platform)

    risk = next((item for item in auth_path_risks(cfg) if item.platform == platform), None)

    provenance = "config_only"
    if scenario["live"] or scenario["source_live"] or scenario["destination_live"]:
        provenance = "unverified_no_credentials"

    return {
        "id": scenario["id"],
        "platform": platform,
        "provenance": provenance,
        "note": scenario["note"],
        "inputs": {
            "facts": _facts(cfg, platform, live_entry),
            # The engine's top-level entry for the platform: for TikTok's
            # split source/destination probes this is the envelope that the
            # analytics role reads.
            "live": live_entry,
            "source_live": scenario["source_live"],
            "destination_live": scenario["destination_live"],
            "checked_at": badge_checked_at,
        },
        "expected": {
            "provider_state": entry["state"],
            "role_states": {role: status["state"] for role, status in entry["role_status"].items()},
            "auth_mode": entry["auth_mode"],
            "token_state": badge_entry["token_state"] if badge_entry else None,
            "badge": badge_entry["badge"] if badge_entry else None,
            "auto_refresh": badge_entry["auto_refresh"] if badge_entry else None,
            "source_only": posting["source_only"],
            "can_post": posting["can_post"],
            "ban_risk": (
                {
                    "code": risk.ban_risk,
                    "advisory_code": risk.advisory_code,
                    "severity": risk.severity,
                    "enabled": risk.enabled,
                    "acknowledged": risk.acknowledged,
                }
                if risk is not None
                else None
            ),
        },
        "informational": {
            "error": entry.get("error"),
            "badge_reason": badge_entry.get("badge_reason") if badge_entry else None,
        },
    }


def _provider_roster(scratch: Path) -> dict[str, Any]:
    """Provider identity + role roster, so the Rust side never hardcodes it."""
    from xpst.config import XPSTConfig
    from xpst.provider_truth import SUPPORTED_PROVIDERS

    os.environ["XPST_CONFIG_DIR"] = str(scratch / "roster")
    XPSTConfig.load()
    return {
        item.name: {
            "display_name": item.display_name,
            "roles": [role.value for role in item.roles],
            "auth_mode": item.auth_mode,
        }
        for item in SUPPORTED_PROVIDERS
    }


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    root = Path(tempfile.mkdtemp(prefix="xpst-auth-truth-"))
    try:
        roster = _provider_roster(root)
        rows = [_run_scenario(scenario, root) for scenario in SCENARIOS]
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

    fixture = {
        "schema_version": 1,
        "generated_by": "scripts/capture_auth_truth_table.py",
        "engine": "python:src/xpst (provider_truth, token_state, auth_path_risk)",
        "now_epoch": NOW,
        "capture_rules": {
            "network": "none — no provider API is called",
            "config_dir": "throwaway scratch directory per row (XPST_CONFIG_DIR); ~/.xpst is never touched",
            "clock": NOW,
            "provenance": {
                "config_only": "observed by loading a scratch config profile with no credentials",
                "unverified_no_credentials": (
                    "state transition produced by the real mapping functions from a SYNTHETIC "
                    "probe result; not observed against a live provider account"
                ),
            },
        },
        "providers": roster,
        "rows": rows,
    }
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE_PATH.relative_to(REPO_ROOT)} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
