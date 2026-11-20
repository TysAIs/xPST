"""First-class queue pause (defect: a paused queue was a renamed file).

Before this contract, the only way to stop a queue was ``mv schedule.json
schedule.json.paused-<ts>``. Nothing in the product read or managed that name:
``schedule list`` reported an empty queue, any dotfile cleanup could delete the
user's future posts, and renaming the file back released every entry with no
resume step and no warning.

The pause is now a store-backed marker (``schedule.paused.json``) enforced at
the claim boundary, so a paused queue cannot post, survives a restart, and is
reported by the surface a human actually reads.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from xpst import cli as cli_module
from xpst.schedule_manager import ScheduleManager


def _manager(tmp_path: Path) -> ScheduleManager:
    return ScheduleManager(str(tmp_path))


def _add_due(manager: ScheduleManager) -> dict:
    return manager.add(
        video_path="clip.mp4",
        caption="queued",
        scheduled_time=datetime.now() - timedelta(minutes=1),
        platforms=["x"],
    )


def test_pause_blocks_claim_and_resume_releases_it(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    entry = _add_due(manager)

    assert manager.pause(reason="operator pause") is True
    assert manager.is_paused() is True
    assert manager.claim_due() == []
    assert manager.claim(entry["id"]) is False
    assert next(e for e in manager.list() if e["id"] == entry["id"])["status"] == "pending"

    assert manager.resume() is True
    assert manager.is_paused() is False
    assert [e["id"] for e in manager.claim_due()] == [entry["id"]]


def test_pause_is_durable_across_instances(tmp_path: Path) -> None:
    _manager(tmp_path).pause(reason="survives restart")

    restarted = _manager(tmp_path)

    assert restarted.is_paused() is True
    assert restarted.pause_state()["reason"] == "survives restart"


def test_pause_and_resume_are_idempotent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    assert manager.resume() is False  # nothing to release
    assert manager.pause() is True
    assert manager.pause() is False  # already paused


def test_pause_state_is_structured(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    assert manager.pause_state()["paused"] is False
    manager.pause(reason="maintenance")
    state = manager.pause_state()

    assert state["paused"] is True
    assert state["reason"] == "maintenance"
    assert state["paused_at"]


def test_corrupt_pause_marker_fails_closed(tmp_path: Path) -> None:
    """An unreadable marker must not silently release the queue."""
    (Path(tmp_path) / "schedule.paused.json").write_text("{not json", encoding="utf-8")

    assert _manager(tmp_path).is_paused() is True


def test_cli_pause_and_list_report_it(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_dir = home / ".xpst"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XPST_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    runner = CliRunner()
    paused = runner.invoke(cli_module.main, ["schedule", "pause", "--reason", "hold", "--json"])
    assert paused.exit_code == 0, paused.output
    assert json.loads(paused.output.strip().splitlines()[-1])["paused"] is True

    listed = runner.invoke(cli_module.main, ["schedule", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output.strip().splitlines()[-1])
    assert payload["paused"]["paused"] is True
    assert payload["paused"]["reason"] == "hold"

    resumed = runner.invoke(cli_module.main, ["schedule", "resume", "--json"])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output.strip().splitlines()[-1])["paused"] is False


def test_cli_schedule_run_refuses_while_paused(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_dir = home / ".xpst"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XPST_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    manager = ScheduleManager(str(config_dir))
    _add_due(manager)
    manager.pause(reason="hold")

    class _Engine:
        def __init__(self, config):  # noqa: ANN001
            self.posts = 0

        async def post_manual(self, *a, **k):  # noqa: ANN001
            self.posts += 1
            raise AssertionError("a paused queue published")

    monkeypatch.setattr(cli_module, "CrossPostEngine", _Engine)

    result = CliRunner().invoke(cli_module.main, ["schedule", "run", "--json"])

    assert result.exit_code != 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["paused"] is True
    assert next(e for e in manager.list())["status"] == "pending"


def test_mcp_pause_and_resume_round_trip(tmp_path: Path, monkeypatch) -> None:
    """An agent surface must be able to pause a queue, not just the CLI."""
    pytest.importorskip("mcp")

    from xpst.mcp import server as mcp_server
    from xpst.mcp.server import result_is_error

    forced = tmp_path / "store"
    original_init = ScheduleManager.__init__

    def patched_init(self, config_dir="~/.xpst"):  # noqa: ANN001
        original_init(self, config_dir=str(forced))

    monkeypatch.setattr(ScheduleManager, "__init__", patched_init)
    # Mutating MCP tools are secure-by-default; this test drives the real
    # dispatch, so it opts in explicitly.
    monkeypatch.setenv("XPST_MCP_ALLOW_MUTATIONS", "1")

    manager = ScheduleManager(str(forced))
    entry = _add_due(manager)

    paused = asyncio.run(
        mcp_server.handle_call_tool("xpst_schedule_pause", {"reason": "hold"})
    )
    assert not result_is_error(paused), paused
    assert manager.is_paused() is True
    assert manager.claim_due() == []

    listed = asyncio.run(mcp_server.handle_call_tool("xpst_schedule_list", {}))
    assert not result_is_error(listed), listed
    payload = json.loads(listed.content[0].text)
    assert payload["paused"]["paused"] is True
    assert payload["paused"]["reason"] == "hold"

    unconfirmed = asyncio.run(mcp_server.handle_call_tool("xpst_schedule_resume", {}))
    assert not result_is_error(unconfirmed), unconfirmed
    assert manager.is_paused() is False
    assert [e["id"] for e in manager.claim_due()] == [entry["id"]]


def test_pause_is_ungated_and_resume_is_consent_gated() -> None:
    """Stopping posts never needs consent; restarting them does."""
    pytest.importorskip("mcp")

    from xpst.mcp.server import _MUTATING_TOOLS

    assert "xpst_schedule_pause" not in _MUTATING_TOOLS
    assert "xpst_schedule_resume" in _MUTATING_TOOLS
