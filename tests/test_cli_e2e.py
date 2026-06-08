"""
End-to-end CLI tests for xPST using Click's CliRunner.

Verifies that every major CLI command produces valid JSON output
when invoked with ``--json``, and that ``--help`` works.

Tests:
- version --json → JSON with 'xpst' key
- config show --json → valid JSON dict
- config validate --json → JSON with 'valid' key
- schedule list --json → JSON array
- plugins list --json → valid JSON
- --help → exit code 0, contains 'xPST'
"""

import json
import logging
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from unittest.mock import patch

from xpst.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_json(output: str):
    """Extract JSON from CLI output that may have log lines prepended."""
    for i, ch in enumerate(output):
        if ch in ('{', '['):
            return json.loads(output[i:])
    return json.loads(output)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 1. version --json
# ---------------------------------------------------------------------------

class TestVersionJson:
    """xpst version --json outputs JSON with 'xpst' key."""

    def test_xpst_version_json(self, runner):
        """version --json produces valid JSON containing 'xpst'."""
        result = runner.invoke(main, ["version", "--json"])
        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert isinstance(data, dict)
        assert "xpst" in data


# ---------------------------------------------------------------------------
# 2. config show --json
# ---------------------------------------------------------------------------

class TestConfigShowJson:
    """xpst config show --json outputs valid JSON."""

    def test_xpst_config_show_json(self, runner, config_file):
        """config show --json produces a valid JSON dict."""
        result = runner.invoke(main, [
            "--config", config_file,
            "config", "show", "--json", "--file", config_file,
        ])
        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# 3. config validate --json
# ---------------------------------------------------------------------------

class TestConfigValidateJson:
    """xpst config validate --json outputs JSON with 'valid' key."""

    def test_xpst_config_validate_json(self, runner, config_file):
        """config validate --json produces valid JSON with 'valid' field."""
        result = runner.invoke(main, [
            "--config", config_file,
            "config", "validate", "--json", "--file", config_file,
        ])
        assert result.output.strip(), "No output produced"
        data = extract_json(result.output)
        assert isinstance(data, dict)
        assert "valid" in data


# ---------------------------------------------------------------------------
# 4. schedule list --json
# ---------------------------------------------------------------------------

class TestScheduleListJson:
    """xpst schedule list --json outputs a JSON array."""

    def test_xpst_schedule_list_json(self, runner, tmp_path, monkeypatch):
        """schedule list --json produces a valid JSON array."""
        from xpst.schedule_manager import ScheduleManager

        orig_init = ScheduleManager.__init__
        sched_dir = str(tmp_path / ".xpst_schedule")

        def patched_init(self, config_dir="~/.xpst"):
            orig_init(self, config_dir=sched_dir)

        monkeypatch.setattr(ScheduleManager, "__init__", patched_init)

        result = runner.invoke(main, ["schedule", "list", "--json"])
        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# 5. plugins list --json
# ---------------------------------------------------------------------------

class TestPluginsListJson:
    """xpst plugins list --json outputs valid JSON."""

    def test_xpst_plugins_list_json(self, runner):
        """plugins list --json produces valid JSON."""
        result = runner.invoke(main, ["plugins", "list", "--json"])
        assert result.exit_code == 0, result.output
        data = extract_json(result.output)
        assert isinstance(data, dict)
        # Should have 'plugins' key (list) and 'count' key
        assert "plugins" in data
        assert "count" in data


# ---------------------------------------------------------------------------
# 6. --help
# ---------------------------------------------------------------------------

class TestHelp:
    """xpst --help works and mentions xPST."""

    def test_xpst_help(self, runner):
        """--help exits 0 and contains 'xPST' in output."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "xPST" in result.output
