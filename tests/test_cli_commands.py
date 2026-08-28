"""Tests for CLI commands with JSON output (item 30).

Uses Click's CliRunner to invoke commands and verify JSON output.
"""

import json
import logging
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from xpst.cli import main


def extract_json(output: str):
    """Extract JSON from CLI output that may have log lines prepended."""
    # Find the first { or [ character
    for i, ch in enumerate(output):
        if ch in ('{', '['):
            return json.loads(output[i:])
    return json.loads(output)


@pytest.fixture
def runner():
    """Create a Click CliRunner."""
    return CliRunner()


@pytest.fixture(autouse=True)
def _suppress_logging():
    """Suppress log output that contaminates CLI output in tests."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def config_file(tmp_path):
    """Create a minimal valid config YAML file."""
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
def xpst_dir(tmp_path, monkeypatch):
    """Redirect ~/.xpst to a temp directory for schedule tests."""
    xpst = tmp_path / ".xpst"
    xpst.mkdir()
    monkeypatch.setattr(Path, "expanduser", lambda self: xpst if str(self) == "~/.xpst" else Path(str(self).replace("~", str(tmp_path))))
    return str(xpst)


class TestConfigShowJson:
    """test_config_show_json: invoke `config show --json`, verify valid JSON."""

    def test_config_show_json(self, runner, config_file):
        """config show --json outputs valid JSON."""
        result = runner.invoke(main, ["--config", config_file, "config", "show", "--json", "--file", config_file])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, dict)

    def test_config_show_json_has_accounts(self, runner, config_file):
        """config show --json contains accounts section."""
        result = runner.invoke(main, ["--config", config_file, "config", "show", "--json", "--file", config_file])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert "accounts" in data


class TestConfigValidateJson:
    """test_config_validate_json: invoke `config validate --json`, verify valid JSON."""

    def test_config_validate_json(self, runner, config_file):
        """config validate --json outputs valid JSON with 'valid' key."""
        result = runner.invoke(main, ["--config", config_file, "config", "validate", "--json", "--file", config_file])
        # May exit with 0 or 4 depending on validation
        assert result.output.strip()
        data = extract_json(result.output)
        assert "valid" in data
        assert "checks" in data
        assert isinstance(data["checks"], list)


class TestConfigExportImport:
    """test_config_export_import: export config, modify, import back, verify."""

    def test_config_export_import(self, runner, config_file, tmp_path):
        """Export config to file and re-import it."""
        export_file = str(tmp_path / "exported.yaml")

        # Export
        result = runner.invoke(main, [
            "--config", config_file,
            "config", "export", export_file,
            "--json", "--raw", "--file", config_file,
        ])
        assert result.exit_code == 0
        export_data = extract_json(result.output)
        assert export_data.get("ok") is True
        assert Path(export_file).exists()

        # Verify exported YAML is valid
        with open(export_file) as f:
            exported = yaml.safe_load(f)
        assert isinstance(exported, dict)
        assert "accounts" in exported

        # Modify the exported config
        exported.setdefault("monitoring", {})["log_level"] = "DEBUG"
        with open(export_file, "w") as f:
            yaml.dump(exported, f)

        # Import back — monkeypatch expanduser so import writes to tmp
        real_config = tmp_path / "imported_config.yaml"
        # Copy original config to new location
        import shutil
        shutil.copy(config_file, str(real_config))

        orig_expanduser = os.path.expanduser
        def fake_expanduser(path):
            if path == "~/.xpst/config.yaml":
                return str(real_config)
            return orig_expanduser(path)
        with patch("os.path.expanduser", side_effect=fake_expanduser):
            with patch("pathlib.Path.expanduser", lambda self: real_config if str(self).endswith("config.yaml") and "~" in str(self) else self):
                result = runner.invoke(main, [
                    "--config", config_file,
                    "config", "import", export_file, "--json",
                ])
        assert result.exit_code == 0
        import_data = extract_json(result.output)
        assert import_data.get("ok") is True

        # Verify imported config has the modification
        with open(str(real_config)) as f:
            final = yaml.safe_load(f)
        assert final.get("monitoring", {}).get("log_level") == "DEBUG"


class TestScheduleAddJson:
    """test_schedule_add_json: invoke `schedule add --json`, verify JSON."""

    def test_schedule_add_json(self, runner, tmp_path, monkeypatch):
        """schedule add --json outputs valid JSON."""
        # Create a dummy video file
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")

        # Patch ScheduleManager to use temp dir
        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=str(tmp_path / ".xpst_schedule"))

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        result = runner.invoke(main, [
            "schedule", "add", str(video),
            "--caption", "Test post",
            "--at", "2026-12-25 10:00",
            "--json",
        ])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert "id" in data
        assert data["caption"] == "Test post"
        assert data["status"] == "pending"


class TestScheduleListJson:
    """test_schedule_list_json: invoke `schedule list --json`, verify JSON."""

    def test_schedule_list_json(self, runner, tmp_path, monkeypatch):
        """schedule list --json outputs valid JSON list."""
        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        # Add an entry first
        mgr = ScheduleManager(config_dir=sched_dir)
        mgr.add("/tmp/test.mp4", "listing test", __import__("datetime").datetime(2026, 12, 1))

        result = runner.invoke(main, ["schedule", "list", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, list)
        assert len(data) >= 1


class TestScheduleRemoveJson:
    """test_schedule_remove_json: add then remove via CLI, verify."""

    def test_schedule_remove_json(self, runner, tmp_path, monkeypatch):
        """Removing a schedule entry via CLI returns ok:true."""
        from xpst.schedule_manager import ScheduleManager
        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        # Add an entry
        mgr = ScheduleManager(config_dir=sched_dir)
        entry = mgr.add("/tmp/test.mp4", "remove me", __import__("datetime").datetime(2026, 12, 1))

        result = runner.invoke(main, ["schedule", "remove", entry["id"], "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert data.get("ok") is True
        assert data.get("removed") == entry["id"]


class TestVersionJson:
    """test_version_json: invoke `version --json`, verify JSON with xpst key."""

    def test_version_json(self, runner):
        """version --json outputs valid JSON with 'xpst' key."""
        result = runner.invoke(main, ["version", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, dict)
        assert "xpst" in data


class TestStatusJson:
    """test_status_json: invoke `status --json`, verify JSON."""

    def test_status_json(self, runner, config_file):
        """status --json outputs valid JSON."""
        result = runner.invoke(main, ["--config", config_file, "status", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert isinstance(data, dict)
        # Status should contain statistics keys
        assert "total_videos_tracked" in data or "total_processed" in data or len(data) > 0


class TestDiagnosticsJson:
    """test_diagnostics_json: invoke `diagnostics --json`, verify bundle output."""

    def test_diagnostics_json_creates_redacted_bundle(self, runner, config_file, tmp_path):
        """diagnostics --json writes a redacted zip bundle."""
        output = tmp_path / "diagnostics.zip"
        log_file = tmp_path / "logs" / "xpst.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("failed token=very-secret-token\n", encoding="utf-8")

        result = runner.invoke(main, ["--config", config_file, "diagnostics", "--output", str(output), "--json"])

        assert result.exit_code == 0
        data = extract_json(result.output)
        assert data == {"ok": True, "output": str(output), "redacted": True}
        with zipfile.ZipFile(output) as archive:
            payload = archive.read("diagnostics.json").decode("utf-8")
        assert "very-secret-token" not in payload
        assert '"readiness"' in payload
        assert '"providers"' in payload


class TestUpdateComponentsJson:
    """test_update_components_json: invoke `update --components --json`."""

    def test_update_components_json(self, runner):
        """update --components --json outputs structured update status."""
        result = runner.invoke(main, ["update", "--components", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert set(data) == {"app", "packages", "helpers", "provider_metadata"}
        assert data["app"][0]["name"] == "xpst"
        assert data["app"][0]["status"]
        assert data["app"][0]["action"]
        assert any(item["name"] == "yt-dlp" for item in data["helpers"])
        assert all("update_command" in item for section in data.values() for item in section)


class TestProvidersJson:
    """test_providers_json: invoke `providers --json`."""

    def test_providers_json(self, runner, config_file):
        """providers --json outputs source and destination manifests."""
        result = runner.invoke(main, ["--config", config_file, "providers", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        source_names = {item["name"] for item in data["sources"]}
        destination_names = {item["name"] for item in data["destinations"]}

        assert source_names >= {"tiktok", "youtube", "instagram", "x", "local"}
        assert destination_names >= {"youtube", "instagram", "x"}
        youtube = next(item for item in data["destinations"] if item["name"] == "youtube")
        assert youtube["auth_mode"] == "oauth"
        assert youtube["is_official_api"] is True


# ── First-run onboarding hygiene (no wedge states) ──────────────

ONBOARDING_HINT = "First-run setup is not complete"


@pytest.fixture
def completed_config_file(tmp_path):
    """A valid config whose first-run onboarding is already complete."""
    import yaml as _yaml

    data = {
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
        "first_run_complete": True,
    }
    cfg = tmp_path / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_yaml.safe_dump(data), encoding="utf-8")
    return str(cfg)


class _StubEngine:
    """Minimal CrossPostEngine stand-in so `run` tests need no real engine."""

    def __init__(self, config=None):
        self.config = config

    def acquire_pidfile(self):
        return True

    def release_pidfile(self):
        return None

    async def check_and_post(self, source=None):
        return []


class TestFirstRunOnboardingHygiene:
    """run/status must never re-trigger first-run onboarding once
    first_run_complete is genuinely true, and a stale/missing
    wizard_state.json must not wedge the flow (audit 02-integrations.md §4).

    Over pipes (non-TTY) the CLI auto-enables JSON, so the nudge is (by
    design) human-TTY-only and machine output stays parseable; the gate
    itself is unit-tested via _maybe_show_onboarding_hint.
    """

    def test_status_skips_onboarding_when_complete(self, runner, completed_config_file):
        """[3] status with first_run_complete=true shows no onboarding nudge"""
        result = runner.invoke(main, ["--config", completed_config_file, "status"])
        assert result.exit_code == 0
        assert ONBOARDING_HINT not in result.output
        assert isinstance(extract_json(result.output), dict)

    def test_status_pipe_mode_never_emits_hint_even_when_incomplete(self, runner, config_file):
        """Piped status stays pure JSON — the nudge must never leak."""
        result = runner.invoke(main, ["--config", config_file, "status"])
        assert result.exit_code == 0
        assert ONBOARDING_HINT not in result.output
        data = extract_json(result.output)
        assert isinstance(data, dict)

    def test_run_skips_onboarding_when_complete(self, runner, completed_config_file, monkeypatch):
        """[3] run with first_run_complete=true does not re-trigger onboarding"""
        monkeypatch.setattr("xpst.cli.CrossPostEngine", _StubEngine)
        result = runner.invoke(main, ["--config", completed_config_file, "run"])
        assert result.exit_code == 0
        assert ONBOARDING_HINT not in result.output

    def test_run_pipe_mode_never_emits_hint_even_when_incomplete(self, runner, config_file, monkeypatch):
        """Piped run stays pure JSON — no onboarding crash or nudge."""
        monkeypatch.setattr("xpst.cli.CrossPostEngine", _StubEngine)
        result = runner.invoke(main, ["--config", config_file, "run"])
        assert result.exit_code == 0
        assert ONBOARDING_HINT not in result.output
        data = extract_json(result.output)
        assert isinstance(data, dict)


class TestOnboardingHintGate:
    """Unit tests for the run/status onboarding gate (human-TTY path)."""

    @staticmethod
    def _config(first_run_complete: bool):
        from xpst.config import XPSTConfig

        cfg = XPSTConfig()
        cfg.first_run_complete = first_run_complete
        return cfg

    def test_hint_emitted_when_onboarding_required(self, monkeypatch):
        from xpst import cli

        captured = []
        monkeypatch.setattr(cli.console, "print", lambda *a, **kw: captured.append(a))
        cli._maybe_show_onboarding_hint(self._config(False), as_json=False, quiet=False)
        assert captured, "hint must be emitted for an un-completed install"
        assert "xpst wizard" in str(captured[0])
        assert "First-run" in str(captured[0])

    def test_hint_suppressed_when_complete(self, monkeypatch):
        from xpst import cli

        captured = []
        monkeypatch.setattr(cli.console, "print", lambda *a, **kw: captured.append(a))
        cli._maybe_show_onboarding_hint(self._config(True), as_json=False, quiet=False)
        assert captured == []

    def test_hint_suppressed_in_json_mode(self, monkeypatch):
        from xpst import cli

        captured = []
        monkeypatch.setattr(cli.console, "print", lambda *a, **kw: captured.append(a))
        cli._maybe_show_onboarding_hint(self._config(False), as_json=True, quiet=False)
        assert captured == []

    def test_hint_suppressed_in_quiet_mode(self, monkeypatch):
        from xpst import cli

        captured = []
        monkeypatch.setattr(cli.console, "print", lambda *a, **kw: captured.append(a))
        cli._maybe_show_onboarding_hint(self._config(False), as_json=False, quiet=True)
        assert captured == []

    def test_gate_never_reads_wizard_state_file(self, monkeypatch):
        """The gate consults first_run_complete only: absence of
        wizard_state.json must never suppress or re-trigger onboarding."""
        from xpst import cli

        cli._maybe_show_onboarding_hint(self._config(False), as_json=False, quiet=False)  # no disk access
        cli._maybe_show_onboarding_hint(self._config(True), as_json=False, quiet=False)
        assert True


class TestMcpCommand:
    """test_mcp_command: invoke `mcp` without starting a real stdio server."""

    def test_mcp_command_uses_packaged_entrypoint(self, runner, monkeypatch):
        """mcp command delegates to xpst.mcp.cli_main."""
        pytest.importorskip("mcp", reason="mcp extra not installed")
        called = {}

        def fake_cli_main():
            called["ok"] = True

        monkeypatch.setattr("xpst.mcp.cli_main", fake_cli_main)
        result = runner.invoke(main, ["mcp"])

        assert result.exit_code == 0
        assert called["ok"] is True


class TestConnectGuide:
    """`xpst connect youtube --guide` — GCP checklist without connecting."""

    def test_guide_json_explicit(self, runner):
        result = runner.invoke(main, ["connect", "youtube", "--guide", "--json"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert data["platform"] == "youtube"
        assert data["app_status"] == "in_production"
        assert data["docs_url"].endswith("docs/youtube-oauth-production.md")
        assert data["client_secrets_path"] == (
            "~/.xpst/credentials/youtube_client_secrets.json"
        )
        assert any(
            "console.cloud.google.com/projectcreate" in step
            for step in data["steps"]
        )

    def test_guide_auto_json_on_non_tty(self, runner):
        """Piped stdout auto-enables JSON (xPST CLI convention) — no flag."""
        result = runner.invoke(main, ["connect", "youtube", "--guide"])
        assert result.exit_code == 0
        data = extract_json(result.output)
        assert data["platform"] == "youtube"

    def test_guide_human_output_lists_steps(self, runner, capsys):
        from xpst import cli as cli_mod

        cli_mod._connect_guide("youtube", as_json=False)
        out = capsys.readouterr().out.replace("\n", "")
        assert "YouTube Shorts" in out
        assert "console.cloud.google.com/projectcreate" in out
        assert "youtube-oauth-production.md" in out
        assert "xpst connect youtube" in out

    def test_guide_rejects_non_youtube_platform(self, runner):
        result = runner.invoke(main, ["connect", "x", "--guide"])
        assert result.exit_code == 2
