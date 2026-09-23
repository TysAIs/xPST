"""Regression tests for the dashboard live analytics read path (t_620480fc).

``AnalyticsReadModel.get_engagement_data`` built its post-id list from
``posted_to[platform]["post_id"]``, but ``StateManager`` persists that entry
as ``{"id": ...}`` — so the live path never collected anything and every
"live" refresh silently reported zero metrics.

Contract pinned here:
- the id is resolved from the real schema (``id`` first, legacy ``post_id``
  fallback) and state written by the real StateManager yields a non-empty
  post-id list and actually invokes the collector;
- live totals equal the recorded snapshot totals for the same metrics;
- a missing id is the expected shape (no fetch for that destination), not
  an error, and never fabricates metrics;
- collection errors fail closed: they propagate instead of being swallowed
  into zeros;
- the method must not be called from inside a running asyncio loop
  (it calls ``asyncio.run``) — that raises rather than hanging.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.dashboard.analytics import AnalyticsCollector as DashboardAnalytics
from xpst.state_manager import StateManager

X_METRICS = {"views": 120, "likes": 8, "comments": 2, "shares": 1}


def _record_post_via_state_manager(config_dir, post_id: str | None) -> None:
    """Write a posted video through the REAL state writer.

    ``StateManager.add_posted_video`` (the ``mark_video_posted`` path)
    persists ``posted_to["x"] = {"id": ..., "url": ..., "timestamp": ...}``,
    i.e. the production schema with no ``post_id`` key.
    """
    destination: dict[str, str] = {"url": "https://x.com/ty/status/state_tweet"}
    if post_id is not None:
        destination["id"] = post_id
    StateManager(str(config_dir)).add_posted_video(
        video_id="vid-1",
        source_url="https://www.tiktok.com/@creator/video/1",
        source_platform="tiktok",
        posted_to={"x": destination},
        caption="hello",
    )


def _assert_state_uses_real_schema(config_dir) -> None:
    """Guard: the fixture really exercises the production schema."""
    from xpst.dashboard.analytics import load_state

    entry = load_state(str(config_dir))["posted_videos"]["vid-1"]["posted_to"]["x"]
    assert "id" in entry and "post_id" not in entry


def _collector_returning(data: dict) -> AsyncMock:
    collector = AsyncMock()
    collector.collect_all = AsyncMock(return_value=data)
    return collector


def test_live_path_resolves_real_state_schema_and_invokes_collector(tmp_path):
    """State written by StateManager ("id") must reach the collector."""
    _record_post_via_state_manager(tmp_path, post_id="state_tweet")
    _assert_state_uses_real_schema(tmp_path)

    dash = DashboardAnalytics(config_dir=str(tmp_path))
    collector = _collector_returning({"x": {"state_tweet": dict(X_METRICS)}})
    dash.__dict__["_live_collector"] = collector

    with patch(
        "xpst.analytics.AnalyticsCollector",
        side_effect=AssertionError("get_engagement_data must reuse the cached collector"),
    ):
        engagement = dash.get_engagement_data()

    collector.collect_all.assert_awaited_once_with(
        {
            "youtube": [],
            "instagram": [],
            "x": ["state_tweet"],
            "tiktok": [],
            "threads": [],
        }
    )
    assert engagement["x"]["posts"] == 1
    for metric, value in X_METRICS.items():
        assert engagement["x"][metric] == value


def test_live_totals_equal_recorded_snapshot_totals(tmp_path):
    """Acceptance: totals from the live collection equal the recorded
    snapshot totals for the same metrics, and the desktop payload surface
    (get_analytics_payload(live=True)) reports the same numbers.

    Uses a REAL AnalyticsCollector with only the per-platform fetch faked,
    so the snapshots persist through the real ownership gate — state.json
    written by StateManager is the same ownership evidence for "x".
    """
    from xpst.analytics import AnalyticsCollector

    _record_post_via_state_manager(tmp_path, post_id="state_tweet")

    dash = DashboardAnalytics(config_dir=str(tmp_path))
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    async def fake_platform(platform, ids):
        return {pid: dict(X_METRICS) for pid in ids}

    with patch.object(collector, "_collect_platform", side_effect=fake_platform):
        dash.__dict__["_live_collector"] = collector

        live = dash.get_engagement_data()
        # collect_all persisted one owned snapshot row, so the recorded
        # path must now agree with the live path metric for metric.
        recorded = dash.get_engagement_from_snapshots()
        payload = dash.get_analytics_payload(live=True)

    for metric, value in X_METRICS.items():
        assert live["x"][metric] == recorded["x"][metric] == value

    assert payload["live"] is True
    by_platform = {p["platform"]: p for p in payload["platforms"]}
    assert by_platform["x"]["total_views"] == X_METRICS["views"]
    assert payload["summary"]["total_views"] == X_METRICS["views"]


def test_missing_id_is_expected_shape_and_never_fabricates(tmp_path):
    """A destination entry without an id (writer default for a failed
    upload) must not error and must not fabricate metrics — posts are
    counted from state, metrics stay honestly zero, no fetch is made."""
    _record_post_via_state_manager(tmp_path, post_id=None)
    _assert_state_uses_real_schema(tmp_path)

    dash = DashboardAnalytics(config_dir=str(tmp_path))
    collector = _collector_returning({})
    dash.__dict__["_live_collector"] = collector

    with patch(
        "xpst.analytics.AnalyticsCollector",
        side_effect=AssertionError("no ids means no collection"),
    ):
        engagement = dash.get_engagement_data()

    collector.collect_all.assert_not_awaited()
    assert engagement["x"]["posts"] == 1
    assert engagement["x"]["views"] == 0
    assert engagement["x"]["likes"] == 0
    assert engagement["x"]["comments"] == 0
    assert engagement["x"]["shares"] == 0


def test_collection_error_propagates_fail_closed(tmp_path):
    """No silent zeros: a collector failure must surface to the caller
    (the desktop live refresh already renders it on a worker thread)."""
    _record_post_via_state_manager(tmp_path, post_id="state_tweet")

    dash = DashboardAnalytics(config_dir=str(tmp_path))
    collector = _collector_returning({})
    collector.collect_all = AsyncMock(side_effect=RuntimeError("metrics down"))
    dash.__dict__["_live_collector"] = collector

    with patch(
        "xpst.analytics.AnalyticsCollector",
        side_effect=AssertionError("get_engagement_data must reuse the cached collector"),
    ):
        with pytest.raises(RuntimeError, match="metrics down"):
            dash.get_engagement_data()

    assert collector.collect_all.await_count == 1


def test_legacy_post_id_key_still_collected(tmp_path):
    """Older hand-written state files that used "post_id" keep working."""
    state = {
        "version": 4,
        "posted_videos": {
            "vid-legacy": {
                "source_url": "https://www.tiktok.com/@creator/video/2",
                "source_platform": "tiktok",
                "caption": "legacy",
                "posted_to": {
                    "x": {
                        "post_id": "legacy_tweet",
                        "url": "https://x.com/ty/status/legacy_tweet",
                        "timestamp": "2026-01-01T00:00:00+00:00",
                    }
                },
                "downloaded_at": "2026-01-01T00:00:00+00:00",
                "last_attempt": "2026-01-01T00:00:00+00:00",
            }
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    dash = DashboardAnalytics(config_dir=str(tmp_path))
    collector = _collector_returning({"x": {"legacy_tweet": {"views": 7, "likes": 1, "comments": 0, "shares": 0}}})
    dash.__dict__["_live_collector"] = collector

    engagement = dash.get_engagement_data()

    collector.collect_all.assert_awaited_once_with(
        {
            "youtube": [],
            "instagram": [],
            "x": ["legacy_tweet"],
            "tiktok": [],
            "threads": [],
        }
    )
    assert engagement["x"]["views"] == 7
    assert engagement["x"]["posts"] == 1


def test_get_engagement_data_rejects_running_loop(tmp_path):
    """The live path calls asyncio.run: inside a running loop it must fail
    loudly (RuntimeError) instead of being misused by async callers.

    collect_all is stubbed with a plain (non-coroutine) return so the only
    raised error is asyncio.run's own running-loop guard — the same guard a
    real coroutine would hit first."""
    _record_post_via_state_manager(tmp_path, post_id="state_tweet")

    dash = DashboardAnalytics(config_dir=str(tmp_path))
    collector = MagicMock()
    collector.collect_all = MagicMock(return_value={"x": {}})
    dash.__dict__["_live_collector"] = collector

    async def _call_in_loop():
        dash.get_engagement_data()

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio.run(_call_in_loop())
    assert collector.collect_all.call_count == 1
