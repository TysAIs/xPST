"""Tests for the onboarding UX commands: ``xpst onboard`` and ``xpst doctor``.

Covers the agent-safety contract (no prompts/browsers on pipes), the
``--dry-run`` plan shape, the doctor JSON contract, and the
``describe_remediation`` failure-mode mapping.

Uses Click's CliRunner; no real network calls or browsers are opened —
``xpst.connect.test_connections`` is stubbed at the CLI boundary.
"""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from xpst.cli import main
from xpst.utils.errors import describe_remediation

PLATFORMS = ["youtube", "tiktok", "x", "instagram", "threads", "messenger"]


@pytest.fixture
def runner():
    """Create a Click CliRunner."""
    return CliRunner()


@pytest.fixture
def config_file(tmp_path):
    """Create a minimal valid config YAML file (same shape as test_cli_commands)."""
    import yaml

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
    cfg.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg, "w") as f:
        yaml.dump(config_data, f)
    return str(cfg)


@pytest.fixture
def stub_healthy(monkeypatch):
    """Make every platform's live health check pass without network I/O."""

    async def _fake(config):
        return {p: True for p in PLATFORMS}

    # cli.py imports test_connections lazily inside the commands
    import xpst.connect as connect_mod

    monkeypatch.setattr(connect_mod, "test_connections", _fake)


@pytest.fixture
def stub_broken(monkeypatch):
    """Health check fails for platforms with no stored session file."""

    async def _fake(config):
        return {p: False for p in PLATFORMS}

    import xpst.connect as connect_mod

    monkeypatch.setattr(connect_mod, "test_connections", _fake)


# ──────────────────────────────────────────────
# xpst onboard
# ──────────────────────────────────────────────


class TestOnboardDryRun:
    def test_dry_run_json_plan_shape(self, runner, config_file, stub_healthy):
        result = runner.invoke(main, ["onboard", "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["order"] == PLATFORMS
        for p in PLATFORMS:
            entry = data["platforms"][p]
            assert entry["connected"] is True
            assert entry["title"]
            assert isinstance(entry["steps"], list) and entry["steps"]
            assert entry["next"] is None  # healthy → nothing to do
        assert data["would_connect"] == []

    def test_dry_run_marks_unconnected(self, runner, config_file, stub_broken):
        result = runner.invoke(main, ["onboard", "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["platforms"]["youtube"]["connected"] is False
        assert data["platforms"]["youtube"]["next"] == "xpst connect youtube"
        assert data["would_connect"] == PLATFORMS

    def test_dry_run_human_output_no_side_effects(self, runner, config_file, stub_broken):
        # CLI auto-switches to JSON on non-TTY; either way no side effects.
        result = runner.invoke(main, ["onboard", "--dry-run"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert "youtube" in data["platforms"]


class TestOnboardInteractiveGate:
    def test_non_tty_refuses_to_prompt(self, runner, config_file, stub_broken):
        """Piped stdin must never prompt or open browsers (agent safety)."""
        result = runner.invoke(main, ["onboard", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["error"]["code"] == "INTERACTIVE_REQUIRED"

    def test_non_tty_human_mode_mentions_alternatives(self, runner, config_file, stub_broken):
        result = runner.invoke(main, ["onboard"])
        assert result.exit_code != 0
        assert "--dry-run" in result.output


class TestOnboardAllConnected:
    def test_json_when_everything_connected(self, runner, config_file, stub_healthy):
        result = runner.invoke(main, ["onboard", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["mode"] == "onboard"
        assert data["connected_count"] == len(PLATFORMS)
        assert data["actions"] == []


# ──────────────────────────────────────────────
# xpst doctor
# ──────────────────────────────────────────────


class TestDoctor:
    def test_all_clear_json(self, runner, config_file, stub_healthy, monkeypatch):
        monkeypatch.setenv("XPST_FFMPEG_PATH", "/usr/local/bin/ffmpeg")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/" + name)
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["doctor"] is True
        assert data["all_clear"] is True
        assert data["issues"] == []
        for p in PLATFORMS:
            assert data["platforms"][p]["connected"] is True
        assert {e["name"] for e in data["environment"]} >= {"ffmpeg", "yt-dlp", "config dir"}

    def test_broken_platform_yields_fix_checklist(self, runner, config_file, stub_broken, monkeypatch):
        monkeypatch.setenv("XPST_FFMPEG_PATH", "/usr/local/bin/ffmpeg")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/" + name)
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code != 0  # meaningful failure exit
        data = json.loads(result.output)
        assert data["all_clear"] is False
        assert data["platforms"]["youtube"]["connected"] is False
        assert "xpst connect youtube" in data["platforms"]["youtube"]["fix"]
        issue_platforms = {i["platform"] for i in data["issues"] if i["platform"]}
        assert issue_platforms == set(PLATFORMS)

    def test_missing_ffmpeg_is_reported(self, runner, config_file, stub_healthy, monkeypatch):
        monkeypatch.delenv("XPST_FFMPEG_PATH", raising=False)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        ffmpeg = next(e for e in data["environment"] if e["name"] == "ffmpeg")
        assert ffmpeg["ok"] is False
        assert "ffmpeg" in ffmpeg["fix"].lower()

    def test_platform_filter_limits_report(self, runner, config_file, stub_broken):
        result = runner.invoke(main, ["doctor", "youtube", "--json"])
        data = json.loads(result.output)
        assert set(data["platforms"]) == {"youtube"}

    def test_human_output_includes_checklist(self, runner, config_file, stub_broken, monkeypatch):
        monkeypatch.setenv("XPST_FFMPEG_PATH", "/usr/local/bin/ffmpeg")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/" + name)
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code != 0
        # Non-TTY auto-JSON still carries the checklist fields.
        assert "Fix-it" in result.output or '"issues"' in result.output
        assert "xpst connect youtube" in result.output


# ──────────────────────────────────────────────
# describe_remediation
# ──────────────────────────────────────────────


class TestDescribeRemediation:
    def test_expired_token(self):
        hint = describe_remediation("youtube", "RefreshError: token expired")
        assert hint is not None
        assert "xpst connect youtube" in hint

    def test_missing_scope_platform_specific(self):
        hint = describe_remediation("tiktok", "insufficient permissions: scope not granted")
        assert hint is not None
        assert "video.publish" in hint

    def test_missing_scope_unknown_platform(self):
        hint = describe_remediation("threads", "access denied: missing scope")
        assert hint is not None
        assert "xpst connect" in hint

    def test_quota_exceeded(self):
        hint = describe_remediation("youtube", "youtubeUploadError: quota exceeded")
        assert hint is not None
        assert "xpst quota" in hint
        # Quota must never suggest re-auth
        assert "connect" not in hint

    def test_unknown_error_returns_none(self):
        assert describe_remediation("x", "something entirely different happened") is None

    def test_empty_message_returns_none(self):
        assert describe_remediation("x", "") is None


# ──────────────────────────────────────────────
# Wiring: failed-connect exit path stays intact
# ──────────────────────────────────────────────


class TestConnectRegression:
    def test_connect_still_registered(self, runner):
        result = runner.invoke(main, ["connect", "--help"])
        assert result.exit_code == 0
        assert "Connect social media accounts" in result.output
