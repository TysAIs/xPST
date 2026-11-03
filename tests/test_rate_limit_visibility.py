"""Cooldown state must be visible on every operator surface (health/doctor/MCP).

The 10-01 rate-limit hardening added a durable cooldown store whose
``snapshot()`` calls itself the "health/doctor" view, but nothing rendered it:
an operator could only see an active cooldown by opening
``rate_cooldowns.json`` by hand, and an agent had no way to ask why nothing was
posting.

These tests pin the wiring, and pin that it stays read-only — no sleeps, no
platform probes, no writes, no uploads — so it is safe inside a dry run:

V-1  the report renders an active cooldown (remaining, strikes, reason, until)
V-2  a Meta-family throttle is visible on the siblings too
V-3  a fresh install reads as idle and the report never creates the file
V-4  a corrupt store degrades to idle instead of raising
V-5  ``xpst health --json`` and its human output carry the cooldown state
V-6  ``xpst doctor --json`` carries it as a note, never as a failure
V-7  the MCP tool is registered, read-only, and answers from local state only
V-8  the report is pure: it does not advance the clock and does not write
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

import xpst.cli as cli_module
from xpst.cli import main as cli_main
from xpst.config import XPSTConfig
from xpst.mcp import server as mcp_server
from xpst.rate_limit_status import cooldown_report, format_duration
from xpst.utils.cooldown_store import CooldownStore

if TYPE_CHECKING:
    from pathlib import Path

PLATFORMS = ("youtube", "x", "instagram", "tiktok", "threads", "facebook", "messenger")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def _config(root: Path) -> tuple[XPSTConfig, Path]:
    """A synthetic install with no credentials, no network and no personal data."""
    config = XPSTConfig()
    config.config_dir = str(root)
    config.monitoring.log_file = str(root / "logs" / "xpst.log")
    config.video.download_dir = str(root / "downloads")
    for name in PLATFORMS:
        getattr(config, name).enabled = False
    config_file = root / "config.yaml"
    config.save(str(config_file))
    return config, config_file


def _cool_down(root: Path, platform: str = "instagram", reason: str = "graph code 4") -> FakeClock:
    clock = FakeClock()
    CooldownStore(root, now=clock).record_hit(platform, reason=reason)
    return clock


def _json_from(output: str) -> dict:
    start = output.find("{")
    assert start >= 0, f"no JSON on stdout: {output!r}"
    return json.loads(output[start:])


@pytest.fixture
def fake_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment prerequisites must not depend on the machine running the tests."""
    for env, name in (
        ("XPST_FFMPEG_PATH", "ffmpeg"),
        ("XPST_FFPROBE_PATH", "ffprobe"),
        ("XPST_YTDLP_PATH", "yt-dlp"),
    ):
        fake = tmp_path / name
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv(env, str(fake))


class _Online:
    online = True
    detail = "connected (test stub)"

    def to_dict(self) -> dict:
        return {"online": True, "detail": self.detail}


@pytest.fixture
def offline_probes(monkeypatch: pytest.MonkeyPatch):
    """No live probe, no network: the surfaces under test only render local state."""
    import xpst.auth_status as auth_status_module
    import xpst.connect as connect_module
    import xpst.engine as engine_module
    import xpst.utils.net as net_module

    monkeypatch.setattr(auth_status_module, "collect_live_auth_status", lambda cfg: {})
    monkeypatch.setattr(net_module, "check_network", lambda: _Online())

    async def empty_connections(cfg):
        return {}

    monkeypatch.setattr(connect_module, "test_connections", empty_connections)

    async def local_only_health(self, *, include_platforms: bool = True):
        return {
            "sources": {},
            "platforms": {},
            "circuit_breakers": {},
            "state": {},
            "quotas": {},
        }

    monkeypatch.setattr(engine_module.CrossPostEngine, "check_health", local_only_health)


# ---------------------------------------------------------------------------
# V-1..V-4, V-8: the pure report
# ---------------------------------------------------------------------------


class TestCooldownReport:
    def test_active_cooldown_is_reported_with_detail(self, tmp_path):
        """V-1: an active cooldown is legible without opening the JSON file."""
        clock = _cool_down(tmp_path)
        report = cooldown_report(_config(tmp_path)[0], now=clock)

        assert report["state"] == "cooling"
        assert "instagram" in report["active"]
        record = report["cooldowns"]["instagram"]
        assert record["remaining_s"] == pytest.approx(60)
        assert record["remaining_human"] == "1m 0s"
        assert record["strikes"] == 1
        assert record["last_reason"] == "graph code 4"
        assert record["until_display"]
        assert report["file_present"] is True
        assert report["source"] == str(tmp_path / "rate_cooldowns.json")

    def test_meta_family_siblings_are_visible(self, tmp_path):
        """V-2: a Meta pool throttle cools the siblings; the report says so."""
        clock = _cool_down(tmp_path, "instagram")
        report = cooldown_report(_config(tmp_path)[0], now=clock)

        for sibling in ("facebook", "threads", "messenger"):
            assert sibling in report["active"], sibling

    def test_fresh_install_is_idle_and_creates_nothing(self, tmp_path):
        """V-3: reading health must not write state."""
        config, _ = _config(tmp_path)
        report = cooldown_report(config)

        assert report["state"] == "idle"
        assert report["active"] == []
        assert report["cooldowns"] == {}
        assert report["file_present"] is False
        assert not (tmp_path / "rate_cooldowns.json").exists(), "report wrote the state file"

    def test_corrupt_store_degrades_to_idle(self, tmp_path):
        """V-4: a broken cooldown file must not break a health check."""
        (tmp_path / "rate_cooldowns.json").write_text("{not json", encoding="utf-8")
        report = cooldown_report(_config(tmp_path)[0])

        assert report["state"] == "idle"
        assert report["file_present"] is True

    def test_report_is_pure(self, tmp_path):
        """V-8: no sleeps, no clock advance, no writes."""
        clock = _cool_down(tmp_path)
        before = clock.t
        path = tmp_path / "rate_cooldowns.json"
        mtime = path.stat().st_mtime_ns

        cooldown_report(_config(tmp_path)[0], now=clock)

        assert clock.t == before
        assert path.stat().st_mtime_ns == mtime

    def test_format_duration_is_human(self):
        assert format_duration(0) == "0s"
        assert format_duration(59) == "59s"
        assert format_duration(200) == "3m 20s"
        assert format_duration(3725) == "1h 2m"
        assert format_duration(90061) == "1d 1h"


# ---------------------------------------------------------------------------
# V-5..V-6: CLI surfaces
# ---------------------------------------------------------------------------


class TestCliSurfaces:
    def test_health_json_reports_the_cooldown(self, tmp_path, fake_binaries, offline_probes):
        """V-5: `xpst health --json` is machine-readable about a cooldown."""
        _cool_down(tmp_path)
        _, config_file = _config(tmp_path)

        result = CliRunner().invoke(cli_main, ["--config", str(config_file), "health", "--json"])

        assert result.exit_code == 0, result.output
        payload = _json_from(result.output)
        cooldowns = payload["rate_limit_cooldowns"]
        assert cooldowns["state"] == "cooling"
        assert "instagram" in cooldowns["active"]

    def test_health_human_output_names_the_cooldown(
        self, tmp_path, fake_binaries, offline_probes, monkeypatch: pytest.MonkeyPatch
    ):
        _cool_down(tmp_path, reason="graph code 4")
        _, config_file = _config(tmp_path)
        # CliRunner's stdout is a pipe, which would auto-select --json; the
        # human branch is what this test is about.
        monkeypatch.setattr(cli_module, "_stdout_is_tty", lambda: True)

        result = CliRunner().invoke(cli_main, ["--config", str(config_file), "health"])

        assert result.exit_code == 0, result.output
        assert "Rate-limit Cooldowns" in result.output
        assert "Instagram" in result.output
        assert "graph code 4" in result.output

    def test_doctor_reports_cooldown_without_failing(self, tmp_path, fake_binaries, offline_probes):
        """V-6: a cooldown is a note, not a failure — doctor must not exit non-zero."""
        _cool_down(tmp_path)
        _, config_file = _config(tmp_path)

        result = CliRunner().invoke(cli_main, ["--config", str(config_file), "doctor", "--json"])

        payload = _json_from(result.output)
        assert result.exit_code == 0, result.output
        assert payload["all_clear"] is True
        assert payload["issues"] == []
        assert payload["rate_limit_cooldowns"]["state"] == "cooling"
        cooldown_notes = [n for n in payload["notes"] if "cooldown" in n["problem"].lower()]
        assert len(cooldown_notes) == 1
        assert "instagram" in cooldown_notes[0]["problem"].lower()

    def test_doctor_idle_reports_no_cooldown_note(self, tmp_path, fake_binaries, offline_probes):
        _, config_file = _config(tmp_path)

        result = CliRunner().invoke(cli_main, ["--config", str(config_file), "doctor", "--json"])

        payload = _json_from(result.output)
        assert payload["rate_limit_cooldowns"]["state"] == "idle"
        assert payload["notes"] == []


# ---------------------------------------------------------------------------
# V-7: MCP tool
# ---------------------------------------------------------------------------


class TestMcpTool:
    def test_tool_is_registered_and_read_only(self):
        names = [tool.name for tool in mcp_server.TOOLS]
        assert "xpst_rate_limit_status" in names
        assert "xpst_rate_limit_status" not in mcp_server._MUTATING_TOOLS

    def test_tool_answers_from_local_state(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        """V-7: the MCP answer is the same report, with no engine/probe required."""
        _cool_down(tmp_path)
        config, _ = _config(tmp_path)
        monkeypatch.setattr(mcp_server, "_server", mcp_server.XPSTMCPServer(config))
        monkeypatch.delenv("XPST_MCP_READONLY", raising=False)

        result = asyncio.run(mcp_server.handle_call_tool("xpst_rate_limit_status", {}))

        assert not result.isError
        payload = json.loads(result.content[0].text)
        assert payload["state"] == "cooling"
        assert "instagram" in payload["active"]

    def test_readonly_mode_does_not_block_it(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        _cool_down(tmp_path)
        config, _ = _config(tmp_path)
        monkeypatch.setattr(mcp_server, "_server", mcp_server.XPSTMCPServer(config))
        monkeypatch.setenv("XPST_MCP_READONLY", "1")

        result = asyncio.run(mcp_server.handle_call_tool("xpst_rate_limit_status", {}))

        assert not result.isError
        assert json.loads(result.content[0].text)["state"] == "cooling"
