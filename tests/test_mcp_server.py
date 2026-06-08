"""Tests for xPST MCP tool helpers."""

from types import SimpleNamespace


def test_mcp_get_analytics_returns_stable_report(monkeypatch, tmp_path):
    """MCP analytics should share the same report shape as CLI analytics."""
    from xpst import mcp_server
    from xpst.analytics import AnalyticsCollector

    async def mock_collect_all(self, post_ids):
        return {
            "youtube": {
                "v1": {
                    "platform": "youtube",
                    "post_id": "v1",
                    "views": 321,
                    "likes": 12,
                    "comments": 3,
                    "shares": 1,
                }
            }
        }

    engine = SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path)))
    monkeypatch.setattr(mcp_server, "_get_engine", lambda config_path=None: engine)
    monkeypatch.setattr(AnalyticsCollector, "_discover_post_ids", lambda self: {"youtube": ["v1"]})
    monkeypatch.setattr(AnalyticsCollector, "collect_all", mock_collect_all)

    report = mcp_server.get_analytics(platforms=["youtube"], top_n=1)

    assert report["ok"] is True
    assert report["status"] == "ok"
    assert report["totals"]["posts"] == 1
    assert report["totals"]["views"] == 321
    assert report["top_posts"][0]["post_id"] == "v1"


def test_mcp_get_analytics_rejects_unknown_platform():
    """MCP analytics should return a structured error for invalid platform filters."""
    from xpst import mcp_server

    report = mcp_server.get_analytics(platforms=["threads"], top_n=1)

    assert report["ok"] is False
    assert "Unknown analytics platform" in report["error"]
