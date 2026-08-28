"""Unit tests for LIVE auth-status reporting (xpst.auth_status).

Covers the `xpst auth status --json` liveness contract:
- live-true and live-false paths with mocked validators (no network)
- the exact regression case: instagram session FILE EXISTS but the
  validator fails → authenticated must be False, not presence-true
- non-interactive safety: validator exceptions/timeouts fail closed
- auth_mode + session_age_days fields
- backward-compatible JSON shape from the CLI
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xpst.auth_status import (
    collect_live_auth_status,
    collect_live_auth_status_async,
)
from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformHealth


@pytest.fixture(autouse=True)
def _suppress_logging():
    """Suppress log output that contaminates CLI output in tests (the rich
    log prefix starts with `[`, which breaks JSON extraction)."""
    import logging

    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def extract_json(output: str):
    """Extract JSON from CLI output that may have log lines prepended."""
    for i, ch in enumerate(output):
        if ch in ("{", "["):
            return json.loads(output[i:])
    return json.loads(output)


class FakeUploader:
    """Uploader stand-in whose check_health() returns a canned result."""

    def __init__(
        self,
        health: PlatformHealth | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._health = health
        self._exc = exc
        self.calls = 0

    async def check_health(self) -> PlatformHealth:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._health is not None
        return self._health


def ok(platform: str, **details) -> PlatformHealth:
    return PlatformHealth(
        platform=platform, authenticated=True, session_valid=True, details=details
    )


def dead(platform: str, error: str) -> PlatformHealth:
    return PlatformHealth(
        platform=platform, authenticated=False, session_valid=False, error=error
    )


def make_config(tmp_path, enabled=("youtube", "x", "instagram", "tiktok")) -> XPSTConfig:
    """Config pointing every credential file into tmp_path/credentials."""
    creds = tmp_path / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.youtube.enabled = "youtube" in enabled
    config.youtube.token_file = str(creds / "youtube_token.json")
    config.x.enabled = "x" in enabled
    config.x.cookies_file = str(creds / "x_cookies.json")
    config.instagram.enabled = "instagram" in enabled
    config.instagram.session_file = str(creds / "instagram_session.json")
    config.tiktok.enabled = "tiktok" in enabled
    config.tiktok.cookies_file = str(creds / "tiktok_cookies.txt")
    return config


def touch(path: str | Path | None, age_days: int = 0) -> None:
    """Create a file (with content) aged `age_days` days."""
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"sessionid": "fake-session-id"}')
    if age_days:
        os.utime(p, (time.time() - age_days * 86400, time.time() - age_days * 86400))


# ── Live-true paths ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_true_youtube_and_x(tmp_path):
    """Healthy validators → authenticated True with live fields set."""
    cfg = make_config(tmp_path)
    cfg.tiktok.client_key = "ck"
    cfg.tiktok.access_token = "at"
    touch(cfg.youtube.token_file)
    touch(cfg.x.cookies_file)
    uploaders = {
        "youtube": FakeUploader(ok("youtube", channel_name="test")),
        "x": FakeUploader(ok("x", username="test")),
        "instagram": FakeUploader(ok("instagram", username="ig")),
        "tiktok": FakeUploader(ok("tiktok", open_id="o1")),
    }

    result = await collect_live_auth_status_async(cfg, uploaders)

    for name in ("youtube", "x", "instagram", "tiktok"):
        entry = result[name]
        assert entry["authenticated"] is True, name
        assert entry["session_valid"] is True, name
        assert entry["live_checked"] is True, name
        assert entry["error"] is None, name
    assert result["youtube"]["auth_mode"] == "oauth"
    assert result["x"]["auth_mode"] == "cookies"
    assert uploaders["youtube"].calls == 1
    # session_age_days computed from credential file mtime (just created → 0)
    assert result["youtube"]["session_age_days"] == 0
    assert result["x"]["session_age_days"] == 0


@pytest.mark.asyncio
async def test_session_age_days_from_file_mtime(tmp_path):
    """session_age_days reflects the credential file's age."""
    cfg = make_config(tmp_path)
    touch(cfg.instagram.session_file, age_days=5)
    uploaders = {
        "instagram": FakeUploader(ok("instagram")),
        "youtube": FakeUploader(dead("youtube", "no creds")),
        "x": FakeUploader(dead("x", "no creds")),
        "tiktok": FakeUploader(dead("tiktok", "no creds")),
    }

    result = await collect_live_auth_status_async(cfg, uploaders)

    assert result["instagram"]["session_age_days"] == 5


# ── Live-false paths (fail closed) ───────────────────────────────


@pytest.mark.asyncio
async def test_live_false_validator_reports_not_authenticated(tmp_path):
    """A validator returning authenticated=False stays False + error."""
    cfg = make_config(tmp_path)
    cfg.tiktok.client_key = "ck"
    cfg.tiktok.access_token = "at"
    uploaders = {
        "x": FakeUploader(dead("x", "Session expired - run 'xpst auth x'")),
        "youtube": FakeUploader(dead("youtube", "YouTube credentials expired")),
        "instagram": FakeUploader(dead("instagram", "Session expired")),
        "tiktok": FakeUploader(dead("tiktok", "TIKTOK_AUTH_EXPIRED")),
    }

    result = await collect_live_auth_status_async(cfg, uploaders)

    for name in ("youtube", "x", "instagram", "tiktok"):
        entry = result[name]
        assert entry["authenticated"] is False, name
        assert entry["session_valid"] is False, name
        assert entry["live_checked"] is True, name
        assert entry["error"], f"{name} must carry an error detail"


@pytest.mark.asyncio
async def test_regression_instagram_session_file_exists_but_dead(tmp_path):
    """THE regression: instagram session file exists (presence → stored
    creds) but the validator fails (expired sessionid) → authenticated
    must be False, not the presence-based True the old code reported."""
    cfg = make_config(tmp_path)
    touch(cfg.instagram.session_file)  # file PRESENT, like the real box
    uploaders = {
        "youtube": FakeUploader(ok("youtube")),
        "x": FakeUploader(ok("x")),
        "instagram": FakeUploader(
            dead("instagram", "Session expired - run 'xpst auth instagram'")
        ),
        "tiktok": FakeUploader(ok("tiktok")),
    }

    result = await collect_live_auth_status_async(cfg, uploaders)

    ig = result["instagram"]
    assert ig["authenticated"] is False
    assert ig["session_valid"] is False
    assert ig["live_checked"] is True
    assert "Session expired" in ig["error"]
    # the file is there — age is computable even though auth is dead
    assert ig["session_age_days"] == 0
    # while youtube/x stay true on the same run
    assert result["youtube"]["authenticated"] is True
    assert result["x"]["authenticated"] is True


@pytest.mark.asyncio
async def test_validator_exception_fails_closed(tmp_path):
    """A crashing validator never raises out of the collector."""
    cfg = make_config(tmp_path)
    uploaders = {
        "youtube": FakeUploader(exc=RuntimeError("network unreachable")),
        "x": FakeUploader(ok("x")),
        "instagram": FakeUploader(ok("instagram")),
        "tiktok": FakeUploader(ok("tiktok")),
    }

    result = await collect_live_auth_status_async(cfg, uploaders)

    entry = result["youtube"]
    assert entry["authenticated"] is False
    assert "Live check failed" in entry["error"]
    assert "network unreachable" in entry["error"]
    # other platforms unaffected
    assert result["x"]["authenticated"] is True


@pytest.mark.asyncio
async def test_disabled_platform_reports_disabled(tmp_path):
    """A known-but-disabled platform reports authenticated=False + 'disabled'."""
    cfg = make_config(tmp_path, enabled=("youtube", "x", "instagram"))  # tiktok off

    result = await collect_live_auth_status_async(cfg, uploaders={})

    assert result["tiktok"]["authenticated"] is False
    assert result["tiktok"]["error"] == "disabled"
    assert result["tiktok"]["live_checked"] is True
    assert result["youtube"]["error"] == "disabled"


@pytest.mark.asyncio
async def test_import_failure_uploader_missing_fails_closed(tmp_path):
    """A platform whose uploader failed to import (dep-less env) → disabled."""
    cfg = make_config(tmp_path, enabled=())

    result = await collect_live_auth_status_async(cfg, uploaders={})

    for name in ("youtube", "x", "instagram", "tiktok"):
        assert result[name]["authenticated"] is False
        assert result[name]["error"] == "disabled"


# ── Auth modes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_mode_x_api_v2(tmp_path):
    cfg = make_config(tmp_path)
    cfg.x.auth_mode = "api_v2"

    result = await collect_live_auth_status_async(
        cfg, uploaders={"x": FakeUploader(ok("x"))}
    )

    assert result["x"]["auth_mode"] == "api_v2"


@pytest.mark.asyncio
async def test_auth_mode_instagram_graph_api_uses_graph_probe(tmp_path, monkeypatch):
    """graph_api mode with a token → Graph API /me probe, not instagrapi."""
    import xpst.auth_status as auth_status

    cfg = make_config(tmp_path)
    cfg.instagram.auth_mode = "graph_api"
    cfg.instagram.graph_access_token = "IGQVTEST"
    calls = []

    async def fake_probe(token):
        calls.append(token)
        return True, None, {"graph_user_id": "17841400000000000"}

    monkeypatch.setattr(auth_status, "_graph_api_probe", fake_probe)

    result = await collect_live_auth_status_async(
        cfg,
        uploaders={
            "instagram": FakeUploader(dead("instagram", "should NOT be called"))
        },
    )

    assert result["instagram"]["auth_mode"] == "graph_api"
    assert result["instagram"]["authenticated"] is True
    assert calls == ["IGQVTEST"]


@pytest.mark.asyncio
async def test_auth_mode_instagraph_without_token_falls_back_to_session(tmp_path):
    """graph_api configured but no token → session probe is what runs."""
    cfg = make_config(tmp_path)
    cfg.instagram.auth_mode = "graph_api"
    cfg.instagram.graph_access_token = ""

    result = await collect_live_auth_status_async(
        cfg, uploaders={"instagram": FakeUploader(ok("instagram", username="ig"))}
    )

    assert result["instagram"]["auth_mode"] == "session"
    assert result["instagram"]["authenticated"] is True


@pytest.mark.asyncio
async def test_auth_mode_tiktok_content_posting_api_uses_uploader(tmp_path):
    cfg = make_config(tmp_path)
    cfg.tiktok.client_key = "ck"
    cfg.tiktok.access_token = "at"

    result = await collect_live_auth_status_async(
        cfg, uploaders={"tiktok": FakeUploader(ok("tiktok", open_id="o1"))}
    )

    assert result["tiktok"]["auth_mode"] == "content_posting_api"
    assert result["tiktok"]["authenticated"] is True


@pytest.mark.asyncio
async def test_auth_mode_tiktok_source_only_checks_source(tmp_path, monkeypatch):
    """source_only mode → yt-dlp + cookie-jar check, no uploader call."""
    import shutil

    cfg = make_config(tmp_path)
    touch(cfg.tiktok.cookies_file)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/yt-dlp" if name == "yt-dlp" else None
    )
    uploaders = {"tiktok": FakeUploader(dead("tiktok", "must NOT be called"))}

    result = await collect_live_auth_status_async(cfg, uploaders)

    assert result["tiktok"]["auth_mode"] == "source_only"
    assert result["tiktok"]["authenticated"] is True
    assert uploaders["tiktok"].calls == 0


@pytest.mark.asyncio
async def test_tiktok_source_only_fails_without_cookies(tmp_path, monkeypatch):
    """source_only with no yt-dlp → honest failure with error detail."""
    import shutil

    cfg = make_config(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = await collect_live_auth_status_async(cfg, uploaders={})

    assert result["tiktok"]["authenticated"] is False
    assert "yt-dlp" in result["tiktok"]["error"]


# ── Sync wrapper robustness ──────────────────────────────────────


def test_sync_wrapper_survives_total_failure(tmp_path, monkeypatch):
    """If the collection itself explodes, the CLI still gets honest JSON."""
    import xpst.auth_status as auth_status

    cfg = make_config(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("no event loop possible")

    # Patch the async core (not asyncio.run) so no coroutine is created
    # and left un-awaited.
    monkeypatch.setattr(auth_status, "collect_live_auth_status_async", boom)

    result = collect_live_auth_status(cfg)

    for name in ("youtube", "x", "instagram", "tiktok"):
        entry = result[name]
        assert entry["authenticated"] is False
        assert entry["live_checked"] is True
        assert "Live check failed" in entry["error"]


# ── CLI contract ─────────────────────────────────────────────────


@pytest.fixture
def config_file(tmp_path):
    """Minimal valid config YAML (same shape as test_cli_commands)."""
    config_data = {
        "accounts": {
            "tiktok": {"username": "test_user"},
            "youtube": {"enabled": True, "client_secrets": "", "token_file": ""},
            "x": {"enabled": True, "cookies_file": ""},
            "instagram": {"enabled": True, "session_file": "", "username": ""},
        },
        "video": {"download_dir": str(tmp_path / "downloads")},
        "monitoring": {
            "log_level": "INFO",
            "log_file": str(tmp_path / "logs" / "xpst.log"),
        },
        "reliability": {"max_retries": 3},
        "rate_limits": {"youtube": 10, "instagram": 10, "x": 10, "tiktok": 10},
        "schedule": {"check_interval": 900},
    }
    cfg = tmp_path / "config.yaml"
    with open(cfg, "w") as f:
        yaml.dump(config_data, f)
    return str(cfg)


def test_cli_auth_status_json_live_contract(config_file, monkeypatch):
    """`auth status --json` merges live fields and keeps old ones."""
    import xpst.auth_status as auth_status

    live = {
        "youtube": {
            "authenticated": True,
            "session_valid": True,
            "auth_mode": "oauth",
            "session_age_days": 2,
            "live_checked": True,
            "error": None,
            "details": {"channel_name": "ch"},
        },
        "x": {
            "authenticated": True,
            "session_valid": True,
            "auth_mode": "cookies",
            "session_age_days": 1,
            "live_checked": True,
            "error": None,
            "details": {},
        },
        "instagram": {
            "authenticated": False,
            "session_valid": False,
            "auth_mode": "session",
            "session_age_days": 9,
            "live_checked": True,
            "error": "Session expired - run 'xpst auth instagram'",
            "details": {},
        },
        "tiktok": {
            "authenticated": True,
            "session_valid": True,
            "auth_mode": "source_only",
            "session_age_days": None,
            "live_checked": True,
            "error": None,
            "details": {},
        },
    }
    monkeypatch.setattr(auth_status, "collect_live_auth_status", lambda cfg: live)

    result = CliRunner().invoke(main, ["--config", config_file, "auth", "status", "--json"])
    assert result.exit_code == 0, result.output
    data = extract_json(result.output)

    platforms = data["platforms"]
    # old fields survive (backward compat)
    for name in ("youtube", "x", "instagram", "threads", "messenger"):
        assert "authenticated" in platforms[name], name
        assert "quota_remaining" in platforms[name], name
    # live truth overrides presence
    assert platforms["instagram"]["authenticated"] is False
    assert platforms["instagram"]["live_checked"] is True
    assert "Session expired" in platforms["instagram"]["error"]
    assert platforms["instagram"]["auth_mode"] == "session"
    assert platforms["instagram"]["session_age_days"] == 9
    assert platforms["youtube"]["authenticated"] is True
    assert platforms["x"]["live_checked"] is True
    assert platforms["tiktok"]["auth_mode"] == "source_only"
    # platforms outside the live scope keep presence-based behaviour
    assert platforms["threads"]["live_checked"] is False
    assert platforms["messenger"]["live_checked"] is False


def test_cli_auth_status_json_regression_dead_instagram(config_file, monkeypatch, tmp_path):
    """End-to-end regression: instagram creds STORED but live check fails →
    authenticated:false in the CLI JSON, youtube/x stay true."""
    import xpst.auth_status as auth_status

    def fake_collect(cfg):
        return collect_live_auth_status(
            cfg,
            uploaders={
                "youtube": FakeUploader(ok("youtube")),
                "x": FakeUploader(ok("x")),
                "instagram": FakeUploader(
                    dead("instagram", "Session expired - run 'xpst auth instagram'")
                ),
                "tiktok": FakeUploader(ok("tiktok")),
            },
        )

    monkeypatch.setattr(auth_status, "collect_live_auth_status", fake_collect)

    result = CliRunner().invoke(main, ["--config", config_file, "auth", "status", "--json"])
    assert result.exit_code == 0, result.output
    data = extract_json(result.output)
    ig = data["platforms"]["instagram"]
    assert ig["authenticated"] is False
    assert ig["live_checked"] is True
    assert ig["error"]
    assert data["platforms"]["youtube"]["authenticated"] is True
    assert data["platforms"]["x"]["authenticated"] is True


def test_cli_auth_status_json_never_prompts_when_validator_hangs(config_file, monkeypatch):
    """Non-interactive safety: a validator raising SystemExit/stdin reads can't
    hang status — the collector fails closed instead."""
    import xpst.auth_status as auth_status

    class PromptingUploader:
        async def check_health(self):
            raise EOFError("stdin read attempted — must not happen")

    def fake_collect(cfg):
        return collect_live_auth_status(
            cfg,
            uploaders={
                "youtube": PromptingUploader(),
                "x": FakeUploader(ok("x")),
                "instagram": FakeUploader(ok("instagram")),
                "tiktok": FakeUploader(ok("tiktok")),
            },
        )

    monkeypatch.setattr(auth_status, "collect_live_auth_status", fake_collect)

    result = CliRunner().invoke(main, ["--config", config_file, "auth", "status", "--json"])
    assert result.exit_code == 0, result.output
    data = extract_json(result.output)
    assert data["platforms"]["youtube"]["authenticated"] is False
    assert data["platforms"]["youtube"]["error"]
    # machine output only — no interactive prompt text on stdout
    assert "Authenticate" not in result.output


def test_cli_auth_status_table_shows_live_error(config_file, monkeypatch):
    """Human table output surfaces the live-check failure too."""
    import xpst.auth_status as auth_status

    def fake_collect(cfg):
        return {
            "youtube": {
                "authenticated": False,
                "session_valid": False,
                "auth_mode": "oauth",
                "session_age_days": None,
                "live_checked": True,
                "error": "client_secrets.json not found",
                "details": {},
            },
            "x": {
                "authenticated": True,
                "session_valid": True,
                "auth_mode": "cookies",
                "session_age_days": None,
                "live_checked": True,
                "error": None,
                "details": {},
            },
            "instagram": {
                "authenticated": False,
                "session_valid": False,
                "auth_mode": "session",
                "session_age_days": None,
                "live_checked": True,
                "error": "Session expired - run 'xpst auth instagram'",
                "details": {},
            },
            "tiktok": {
                "authenticated": False,
                "session_valid": False,
                "auth_mode": "source_only",
                "session_age_days": None,
                "live_checked": True,
                "error": "disabled",
                "details": {},
            },
        }

    monkeypatch.setattr(auth_status, "collect_live_auth_status", fake_collect)

    result = CliRunner().invoke(main, ["--config", config_file, "auth", "status"])
    assert result.exit_code == 0, result.output
    assert "client_secrets.json not found" in result.output
    assert "Session expired" in result.output
