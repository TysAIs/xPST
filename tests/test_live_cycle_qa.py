"""QA-wave live-cycle tests (HANDOFF-2026-08-29).

Covers the features re-landed after the qa/dashboard-webview-adversarial
clone was deleted:

- analytics drill-down (AnalyticsStore.get_video_metrics(_map) +
  backend.getVideoMetrics)
- live-cycle harness contract (scripts/live_cycle_test.py)
- delete idempotency ('already deleted' short-circuit in engine.delete_post)
- disconnect (connect.disconnect_platform + CLI + MCP xpst_disconnect)
- MCP fixes (xpst_delete unknown-video → success=false; analytics handlers
  resolved from config.config_dir)
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from xpst.analytics_store import AnalyticsStore
from xpst.config import XPSTConfig
from xpst.connect import disconnect_platform
from xpst.engine import DeleteOutcome
from xpst.mcp import server as mcp_server
from xpst.state import StateManager
from xpst.utils.credentials import CredentialStore

# Reuse the harness module from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
harness = importlib.import_module("live_cycle_test")


# ── Analytics drill-down: store level ────────────────────────────────────────


def test_analytics_store_get_video_metrics_roundtrip(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([
        {"platform": "youtube", "post_id": "p1", "views": 100, "likes": 10},
        {"platform": "x", "post_id": "p1", "views": 50, "likes": 5},
        {"platform": "x", "post_id": "p1", "views": 60, "likes": 6},  # newer
    ])

    rows = store.get_video_metrics(["p1"])
    by_platform = {r["platform"]: r for r in rows}
    assert set(by_platform) == {"youtube", "x"}
    # only the LATEST snapshot per (platform, post_id)
    assert by_platform["x"]["views"] == 60
    assert by_platform["youtube"]["views"] == 100

    filtered = store.get_video_metrics(["p1"], platform="x")
    assert len(filtered) == 1 and filtered[0]["views"] == 60

    assert store.get_video_metrics([]) == []
    assert store.get_video_metrics(["missing-id"]) == []


def test_analytics_store_get_video_metrics_map(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([
        {"platform": "youtube", "post_id": "p1", "views": 100},
        {"platform": "x", "post_id": "p2", "views": 7},
    ])
    mapping = store.get_video_metrics_map(["p1", "p2"])
    assert set(mapping) == {"p1", "p2"}
    assert len(mapping["p1"]) == 1 and mapping["p1"][0]["views"] == 100


# ── Analytics drill-down: desktop backend ────────────────────────────────────

pytest.importorskip("PySide6", reason="desktop extra not installed")

from xpst.desktop_app.backend import AppController  # noqa: E402


def test_backend_get_video_metrics_drilldown(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)

    store = AnalyticsStore(Path(tmp_path) / "analytics.db")
    store.record_snapshots([
        {"platform": "youtube", "post_id": "yt-1", "views": 120, "likes": 12},
    ])

    video = {
        "posted_to": {
            "youtube": {"id": "yt-1", "url": "https://youtu.be/yt-1", "timestamp": "2026-08-29T00:00:00"},
            "x": {"id": "x-1", "url": "https://x.com/x-1", "timestamp": "2026-08-29T00:00:00"},
        }
    }

    controller = SimpleNamespace(
        _state=SimpleNamespace(get_video=lambda vid: video),
        _config=config,
    )
    raw = AppController.getVideoMetrics(controller, "vid-1")
    data = json.loads(raw)

    assert data["available"] is True
    assert data["totals"]["views"] == 120
    assert data["platforms"]["youtube"]["views"] == 120
    assert data["platforms"]["x"]["views"] == 0  # no snapshot yet


def test_backend_get_video_metrics_unknown_video(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    controller = SimpleNamespace(
        _state=SimpleNamespace(get_video=lambda vid: None),
        _config=config,
    )
    data = json.loads(AppController.getVideoMetrics(controller, "nope"))
    assert data["available"] is False
    assert "Unknown video" in data["error"]


# ── Delete idempotency ────────────────────────────────────────────────────────


def test_engine_delete_post_already_deleted_short_circuits(tmp_path):
    """Second delete of a tombstoned post must return 'already deleted'
    WITHOUT consulting any platform adapter."""
    state = StateManager(str(tmp_path / "config"))
    video_id, platform = "vid-qa", "youtube"
    state.mark_video_posted(video_id, platform, post_id="yt-42", post_url="https://youtu.be/yt-42")
    state.record_delete_tombstone(video_id, platform, reason="hard_delete")

    class _EngineShim:
        def __init__(self) -> None:
            self.state = state
        adapter_calls = 0

    result = asyncio.run(
        mcp_delete_helper(_EngineShim(), video_id, platform)
    )
    assert result.outcome is DeleteOutcome.DELETED
    assert "already deleted" in result.message.lower()
    assert result.detail == "already deleted"


async def mcp_delete_helper(engine, video_id, platform):
    from xpst.engine import CrossPostEngine

    return await CrossPostEngine.delete_post(engine, video_id, platform)


# ── Live-cycle harness ────────────────────────────────────────────────────────


def test_live_cycle_harness_all_steps_pass(tmp_path):
    report = harness.run_all(tmp_path)
    assert report["passed"] is True, report
    assert report["step_count"] == 4
    assert all(step["pass"] for step in report["steps"])


def test_live_cycle_harness_report_is_json_serializable(tmp_path):
    report = harness.run_all(tmp_path)
    text = json.dumps(report, default=str)
    assert json.loads(text)["harness"] == "live_cycle_test"


def test_live_cycle_harness_cli_exit_zero():
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "live_cycle_test.py"), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["passed"] is True


# ── MCP fixes ─────────────────────────────────────────────────────────────────


def _tool_result_payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_mcp_delete_unknown_video_returns_success_false():
    engine = SimpleNamespace(state=SimpleNamespace(get_video=lambda vid: None))
    result = asyncio.run(mcp_server._handle_delete(engine, {"video_id": "ghost"}))
    payload = _tool_result_payload(result)
    assert payload["success"] is False
    assert payload["removed"] == []
    assert "ghost" in payload["error"]


def test_mcp_delete_known_video_returns_success_true():
    video = {"posted_to": {"youtube": {"id": "yt-9"}}}
    removed = []

    class _State:
        def get_video(self, vid):
            return video

        def remove_post(self, vid, plat):
            removed.append((vid, plat))

        def save(self):
            pass

    result = asyncio.run(mcp_server._handle_delete(SimpleNamespace(state=_State()), {"video_id": "v1"}))
    payload = _tool_result_payload(result)
    assert payload["success"] is True
    assert payload["removed"] == ["youtube"]
    assert removed == [("v1", "youtube")]


def test_mcp_disconnect_tool_registered_and_guarded():
    tool_names = {tool.name for tool in mcp_server.TOOLS}
    assert "xpst_disconnect" in tool_names
    # disconnect removes real credentials -> must be guardrail-protected
    assert "xpst_disconnect" in mcp_server._MUTATING_TOOLS


def test_mcp_disconnect_blocked_by_readonly(monkeypatch):
    monkeypatch.setenv("XPST_MCP_READONLY", "1")
    result = asyncio.run(mcp_server.handle_call_tool("xpst_disconnect", {"platform": "x"}))
    assert result.isError is True
    assert "XPST_MCP_READONLY" in result.content[0].text


def test_mcp_disconnect_handler_removes_credentials(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.save()
    CredentialStore(str(tmp_path)).store("x_cookies", "secret")

    result = asyncio.run(
        mcp_server._handle_disconnect(config, {"platform": "x"})
    )
    payload = _tool_result_payload(result)
    assert payload["success"] is True
    assert "x_cookies" in payload["removed"]
    assert payload["disabled"] is True


def test_mcp_analytics_handlers_use_config_dir(tmp_path):
    """Analytics handlers must resolve stores under config.config_dir, never
    a hardcoded ~/.xpst."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)

    asyncio.run(mcp_server._handle_analytics(config, {}))
    assert (Path(tmp_path) / "analytics.db").exists(), (
        "xpst_analytics must create its store under config.config_dir"
    )

    asyncio.run(mcp_server._handle_followers(config))
    assert (Path(tmp_path) / "analytics.db").exists()

    asyncio.run(mcp_server._handle_cross_post_analytics(config))


# ── Disconnect: connect.py contract ──────────────────────────────────────────


def test_disconnect_platform_removes_all_artifacts(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path / "cfg")
    config.save()

    cred_dir = tmp_path / "cfg" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    (cred_dir / "x_cookies.json").write_text("{}")
    CredentialStore(str(tmp_path / "cfg")).store("x_cookies", "secret")

    result = disconnect_platform("x", config)
    assert result["success"] is True
    assert "x_cookies" in result["removed"]
    assert "x_cookies.json" in result["removed"]
    assert not (cred_dir / "x_cookies.json").exists()

    # config persisted with platform disabled
    reloaded = XPSTConfig.load(str(tmp_path / "cfg" / "config.yaml"))
    assert reloaded.x.enabled is False


def test_disconnect_platform_unknown_platform_fails_cleanly():
    result = disconnect_platform("myspace")
    assert result["success"] is False
    assert "myspace" in result["error"]


def test_cli_disconnect_registered():
    from xpst.cli import main

    assert "disconnect" in main.commands
