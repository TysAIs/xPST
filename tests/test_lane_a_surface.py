"""Analytics + agent surface tests (Lane A / G19-G21, G24-G28)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from xpst.analytics_store import AnalyticsStore
from xpst.dashboard.analytics import AnalyticsCollector as DashboardAnalytics


def _seed_store(config_dir, rows):
    store = AnalyticsStore(config_dir / "analytics.db")
    store.record_snapshots(rows)
    return store


class TestAnalyticsPayload:
    def test_analytics_payload_has_metrics(self, tmp_path):
        """G19: the QML contract — summary.total_* and platforms[].total_*
        populated from persisted snapshots, no network."""
        _seed_store(tmp_path, [
            {"platform": "youtube", "post_id": "v1", "views": 100, "likes": 10,
             "comments": 2, "shares": 1, "timestamp": "2026-06-11T10:00:00+00:00"},
            {"platform": "x", "post_id": "t1", "views": 50, "likes": 5,
             "comments": 1, "shares": 3, "timestamp": "2026-06-11T10:00:00+00:00"},
        ])
        dash = DashboardAnalytics(config_dir=str(tmp_path))
        payload = dash.get_analytics_payload(live=False)

        assert payload["available"] is True
        assert payload["summary"]["total_views"] == 150
        assert payload["summary"]["total_shares"] == 4
        by_platform = {p["platform"]: p for p in payload["platforms"]}
        assert by_platform["youtube"]["total_views"] == 100
        assert by_platform["x"]["total_views"] == 50

    def test_payload_prev_totals_none_without_history(self, tmp_path):
        """G21: no week-old snapshots → prev_totals is None, never fabricated."""
        _seed_store(tmp_path, [
            {"platform": "x", "post_id": "t1", "views": 50,
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        dash = DashboardAnalytics(config_dir=str(tmp_path))
        payload = dash.get_analytics_payload(live=False)
        assert payload["summary"]["prev_totals"] is None

    def test_payload_prev_totals_from_real_history(self, tmp_path):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        _seed_store(tmp_path, [
            {"platform": "x", "post_id": "t1", "views": 40, "likes": 4,
             "comments": 0, "shares": 0, "timestamp": old_ts},
            {"platform": "x", "post_id": "t1", "views": 90, "likes": 9,
             "comments": 0, "shares": 0,
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        dash = DashboardAnalytics(config_dir=str(tmp_path))
        payload = dash.get_analytics_payload(live=False)
        assert payload["summary"]["prev_totals"] == {
            "views": 40, "likes": 4, "comments": 0, "shares": 0,
        }
        assert payload["summary"]["total_views"] == 90

    def test_summary_stats_default_does_no_network(self, tmp_path, monkeypatch):
        """G20: get_summary_stats must never live-fetch by default — the
        desktop refresh timer calls it on the GUI thread."""
        dash = DashboardAnalytics(config_dir=str(tmp_path))

        def boom(*a, **k):
            raise AssertionError("network path invoked from default summary")

        monkeypatch.setattr(dash, "get_engagement_data", boom)
        summary = dash.get_summary_stats()
        assert "total_posts" in summary

    def test_no_fabricated_multipliers_in_qml(self):
        from pathlib import Path

        qml = (
            Path(__file__).parent.parent
            / "src/xpst/desktop_app/qml/pages/AnalyticsPage.qml"
        ).read_text(encoding="utf-8-sig")
        assert "lastWeekMultipliers" not in qml
        assert "0.72" not in qml


class TestMcpSurface:
    def test_mcp_enums_are_dynamic(self):
        """G25: tool schemas derive platform/source enums from the provider
        catalog, so plugin providers are reachable."""
        from xpst.mcp import server as mcp_server

        assert isinstance(mcp_server._PLATFORM_ENUM, list)
        assert set(mcp_server._PLATFORM_ENUM) >= {"youtube", "x", "instagram"}
        assert set(mcp_server._SOURCE_ENUM) >= {
            "tiktok", "youtube", "x", "instagram", "local",
        }
        run_tool = next(t for t in mcp_server.TOOLS if t.name == "xpst_run")
        assert run_tool.inputSchema["properties"]["source"]["enum"] == (
            mcp_server._SOURCE_ENUM
        )

    def test_mcp_analytics_tool_registered(self):
        """G27: xpst_analytics exists with a schema a cold agent can use."""
        from xpst.mcp import server as mcp_server

        tool = next(t for t in mcp_server.TOOLS if t.name == "xpst_analytics")
        assert "live" in tool.inputSchema["properties"]
        assert tool.description

    @pytest.mark.asyncio
    async def test_mcp_analytics_returns_persisted_metrics(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from xpst.mcp.server import _handle_analytics

        result = await _handle_analytics({})
        payload = json.loads(result.content[0].text)
        assert set(payload) == {"live", "snapshot_count", "platforms", "posts"}
        assert payload["live"] is False

    @pytest.mark.asyncio
    async def test_config_show_masks_monitoring_secrets(self, monkeypatch):
        """G26 regression: the dashboard password hash must never reach an
        agent. The old handler dumped monitoring.__dict__ unmasked."""
        from xpst.config import XPSTConfig
        from xpst.mcp.server import _handle_config_show

        config = XPSTConfig()
        config.monitoring.dashboard_password_hash = "scrypt$deadbeef"
        if hasattr(config.monitoring, "dashboard_username"):
            config.monitoring.dashboard_username = "alice"

        result = await _handle_config_show(config)
        text = result.content[0].text
        assert "scrypt$deadbeef" not in text, "password hash leaked to MCP output"
        payload = json.loads(text)
        masked_value = payload["monitoring"]["dashboard_password_hash"]
        assert masked_value != "scrypt$deadbeef"


class TestCliJson:
    def test_analytics_json_flag_emits_json(self, tmp_path, monkeypatch):
        """G24: xpst analytics --json must produce parseable JSON."""
        from click.testing import CliRunner

        from xpst.cli import main as cli

        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli, ["analytics", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "platforms" in payload

    def test_analytics_json_source_has_branch(self):
        from pathlib import Path

        src = (Path(__file__).parent.parent / "src/xpst/cli.py").read_text()
        assert "if as_json" in src


class TestRunResults:
    @pytest.mark.asyncio
    async def test_xpst_run_returns_per_post(self, monkeypatch):
        """G28: xpst_run returns structured per-video results, not a bare
        success string."""
        from unittest.mock import AsyncMock, MagicMock

        from xpst.engine import CrossPostResult
        from xpst.mcp import server as mcp_server
        from xpst.platforms.base import UploadResult

        fake_result = CrossPostResult(video_id="v1", caption="c")
        fake_result.results["youtube"] = UploadResult(
            success=True, post_id="p1",
            post_url="https://youtube.com/watch?v=p1", platform="youtube",
        )
        engine = MagicMock()
        engine.check_and_post = AsyncMock(return_value=[fake_result])

        result = await mcp_server._handle_run(engine, {"source": "youtube"})
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is True
        assert payload["processed"] == 1
        assert payload["results"][0]["video_id"] == "v1"
        text = result.content[0].text
        assert "https://youtube.com/watch?v=p1" in text


class TestScheduleTools:
    @pytest.mark.asyncio
    async def test_schedule_add_and_list_roundtrip(self, tmp_path):
        """G29: agents can schedule posts and read the schedule over MCP."""
        from unittest.mock import MagicMock

        from xpst.mcp.server import _handle_schedule_add, _handle_schedule_list

        config = MagicMock()
        config.config_dir = str(tmp_path)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        result = await _handle_schedule_add(config, {
            "video_path": str(video),
            "caption": "scheduled hello",
            "scheduled_time": "2026-06-12T09:30:00",
            "platforms": ["youtube"],
        })
        assert not getattr(result, "isError", False), result.content[0].text
        entry = json.loads(result.content[0].text)["scheduled"]
        assert entry["status"] == "pending"

        listing = await _handle_schedule_list(config)
        schedules = json.loads(listing.content[0].text)["schedules"]
        assert len(schedules) == 1
        assert schedules[0]["caption"] == "scheduled hello"

    @pytest.mark.asyncio
    async def test_schedule_add_rejects_missing_file_and_bad_time(self, tmp_path):
        from unittest.mock import MagicMock

        from xpst.mcp.server import _handle_schedule_add

        config = MagicMock()
        config.config_dir = str(tmp_path)
        r1 = await _handle_schedule_add(config, {
            "video_path": str(tmp_path / "missing.mp4"),
            "caption": "c", "scheduled_time": "2026-06-12T09:30:00",
        })
        assert r1.isError
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        r2 = await _handle_schedule_add(config, {
            "video_path": str(video), "caption": "c",
            "scheduled_time": "not-a-time",
        })
        assert r2.isError

    @pytest.mark.asyncio
    async def test_schedule_add_is_guarded(self, monkeypatch):
        monkeypatch.setenv("XPST_MCP_READONLY", "1")
        from xpst.mcp.server import handle_call_tool

        result = await handle_call_tool("xpst_schedule_add", {})
        assert result.isError and "Blocked" in result.content[0].text
