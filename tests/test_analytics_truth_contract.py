"""Truth-contract regressions for analytics ownership, freshness, and honesty."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.analytics import AnalyticsCollector
from xpst.analytics_store import AnalyticsStore
from xpst.config import XPSTConfig
from xpst.dashboard.analytics import AnalyticsCollector as DashboardAnalytics
from xpst.scheduler import Scheduler

PLATFORMS = ("youtube", "x", "instagram", "tiktok", "threads")


def _row(platform: str, post_id: str, views: int = 1) -> dict[str, object]:
    return {
        "platform": platform,
        "post_id": post_id,
        "views": views,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "timestamp": "2026-09-11T12:00:00+00:00",
    }


def _state_with_ids(tmp_path, ids: dict[str, str]) -> None:
    posted_to = {platform: {"id": post_id} for platform, post_id in ids.items()}
    (tmp_path / "state.json").write_text(
        json.dumps({"posted_videos": {"video-1": {"posted_to": posted_to}}})
    )


def test_store_purges_foreign_rows_for_every_supported_platform(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    # Keep the construction explicit so each platform is covered by the same
    # public persistence API rather than a YouTube-only special case.
    rows = [
        item
        for platform in PLATFORMS
        for item in (
            _row(platform, f"{platform}-owned"),
            _row(platform, f"{platform}-foreign", views=99),
        )
    ]
    assert store.record_snapshots(rows) == len(rows)

    for platform in PLATFORMS:
        assert store.delete_snapshots_not_in(platform, {f"{platform}-owned"}) == 1

    assert {
        (row["platform"], row["post_id"])
        for row in store.latest()
    } == {(platform, f"{platform}-owned") for platform in PLATFORMS}


def test_store_normalizes_offset_timestamps_before_ordering(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [_row("x", "same", views=1) | {"timestamp": "2026-09-11T12:00:00+02:00"}]
    )
    store.record_snapshots(
        [_row("x", "same", views=2) | {"timestamp": "2026-09-11T10:00:00+00:00"}]
    )
    assert store.snapshot_count() == 1
    assert store.latest("x")[0]["views"] == 2


def test_empty_ownership_never_wipes_a_platform(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    rows = [_row(platform, f"{platform}-existing") for platform in PLATFORMS]
    store.record_snapshots(rows)

    for platform in PLATFORMS:
        assert store.delete_snapshots_not_in(platform, set()) == 0

    assert store.snapshot_count() == len(PLATFORMS)


@pytest.mark.asyncio
async def test_persistence_gate_rejects_foreign_ids_on_all_platforms(tmp_path):
    ids = {platform: f"{platform}-owned" for platform in PLATFORMS}
    _state_with_ids(tmp_path, ids)
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    collector._owned_yt_ids = {ids["youtube"]}
    collector._owned_yt_ids_ts = time.time()

    async def fake_collect(platform: str, requested: list[str]) -> dict[str, dict[str, object]]:
        return {
            post_id: _row(platform, post_id)
            for post_id in requested
        }

    requested = {
        platform: [ids[platform], f"{platform}-foreign"]
        for platform in PLATFORMS
    }
    with patch.object(collector, "_collect_platform", side_effect=fake_collect):
        data = await collector.collect_all(requested)

    assert set(data["tiktok"]) == {ids["tiktok"], "tiktok-foreign"}
    persisted = {
        (row["platform"], row["post_id"])
        for row in collector.store.latest()
    }
    assert persisted == {(platform, ids[platform]) for platform in PLATFORMS}
    assert all(f"{platform}:{platform}-foreign" in collector._warned_foreign for platform in PLATFORMS)


@pytest.mark.asyncio
async def test_empty_or_unverifiable_ownership_fails_closed_without_purge(tmp_path):
    # A valid state file with no platform IDs is a verified empty ownership set.
    (tmp_path / "state.json").write_text(json.dumps({"posted_videos": {}}))
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    collector._owned_yt_ids = set()
    collector._owned_yt_ids_ts = time.time()
    existing = [_row(platform, f"{platform}-existing", views=7) for platform in PLATFORMS]
    collector.store.record_snapshots(existing)

    async def foreign_rows(platform: str, requested: list[str]) -> dict[str, dict[str, object]]:
        return {requested[0]: _row(platform, requested[0], views=88)}

    requested = {platform: [f"{platform}-new"] for platform in PLATFORMS}
    with patch.object(collector, "_collect_platform", side_effect=foreign_rows):
        await collector.collect_all(requested)

    # No new row may cross the persistence boundary, but old history remains
    # because an empty ownership set is not evidence that the database is safe
    # to wipe.
    assert {
        (row["platform"], row["post_id"])
        for row in collector.store.latest()
    } == {(platform, f"{platform}-existing") for platform in PLATFORMS}

    # A missing state file is unverifiable: it is also a no-op for purge and a
    # fail-closed input gate.
    (tmp_path / "state.json").unlink()
    assert collector._filter_owned_snapshots([_row("x", "unverified")]) == []
    before = collector.store.snapshot_count()
    collector._purge_stale_snapshots()
    assert collector.store.snapshot_count() == before


def test_freshness_metadata_reports_empty_fresh_and_stale_states(tmp_path):
    dashboard = DashboardAnalytics(config_dir=str(tmp_path))
    empty = dashboard.get_summary_stats()
    assert empty["last_captured"] is None
    assert empty["staleness_hours"] is None
    assert empty["last_captured_relative"] == "—"
    assert empty["freshness"] == "unknown"

    store = AnalyticsStore(tmp_path / "analytics.db")
    captured = datetime.now(timezone.utc).isoformat()
    store.record_snapshots([_row("x", "fresh", views=5) | {"timestamp": captured}])
    fresh = dashboard.get_summary_stats()
    assert fresh["last_captured"] is not None
    assert 0 <= fresh["staleness_hours"] < 1
    assert fresh["last_captured_relative"] == "just now"
    assert fresh["freshness"] == "fresh"

    old_dir = tmp_path / "stale"
    old_store = AnalyticsStore(old_dir / "analytics.db")
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    old_store.record_snapshots([_row("instagram", "old", views=2) | {"timestamp": old}])
    stale = DashboardAnalytics(config_dir=str(old_dir)).get_summary_stats()
    assert stale["staleness_hours"] >= 24
    assert stale["last_captured_relative"].endswith(("h ago", "d ago"))
    assert stale["freshness"] == "stale"


def test_report_exposes_as_of_and_staleness_without_fabricating_values(tmp_path):
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    captured = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    report = collector.build_report(
        {
            "youtube": {
                "yt1": {
                    "platform": "youtube",
                    "post_id": "yt1",
                    "views": 10,
                    "timestamp": captured,
                }
            }
        }
    )
    youtube = report["platforms"]["youtube"]
    assert youtube["as_of"] == captured
    assert youtube["last_captured"] == captured
    assert youtube["staleness_hours"] >= 1
    assert youtube["totals"] == {"views": 10}
    assert "likes" not in youtube["totals"]


@pytest.mark.asyncio
async def test_instagram_insights_gated_metrics_are_missing_not_zero(tmp_path):
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "instagram_session.json").write_text(
        json.dumps({"authorization_data": {"sessionid": "fake"}})
    )
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    info = MagicMock()
    info.like_count = 0
    info.comment_count = 0
    info.play_count = 740
    import instagrapi

    client = MagicMock(spec=instagrapi.Client)
    client.media_info.return_value = info
    client.insights_media.side_effect = RuntimeError("Business account required")
    with patch.object(instagrapi, "Client", return_value=client):
        result = await collector._collect_instagram(["12345"])

    assert result[0]["views"] == 740
    assert result[0]["likes"] == 0
    assert result[0]["comments"] == 0
    assert "shares" not in result[0]
    assert "saves" not in result[0]


def test_state_only_lineup_uses_null_metrics(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "posted_videos": {
                    "video-1": {
                        "caption": "caption",
                        "posted_to": {"threads": {"id": "thread-1", "timestamp": now}},
                    }
                }
            }
        )
    )
    entry = next(
        row for row in DashboardAnalytics(config_dir=str(tmp_path)).get_video_lineup()
        if row["post_id"] == "thread-1"
    )
    assert {key: entry[key] for key in ("views", "likes", "comments", "shares")} == {
        "views": None,
        "likes": None,
        "comments": None,
        "shares": None,
    }


def test_scheduler_snapshot_capture_is_optional_bounded_and_isolated(tmp_path):
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.schedule.analytics_snapshot_enabled = True
    config.schedule.analytics_snapshot_interval = 60
    config._validate()
    scheduler = Scheduler(engine=MagicMock(), config=config)
    collector = MagicMock()
    collector.collect_all = AsyncMock(return_value={"x": {"id": {"views": 1}}})

    with patch("xpst.analytics.AnalyticsCollector", return_value=collector):
        scheduler._maybe_capture_analytics()
        scheduler._maybe_capture_analytics()
        assert collector.collect_all.await_count == 1
        scheduler._last_snapshot_capture -= 61
        scheduler._maybe_capture_analytics()

    assert collector.collect_all.await_count == 2

    failing = MagicMock()
    failing.collect_all = AsyncMock(side_effect=RuntimeError("metrics down"))
    scheduler._last_snapshot_capture = None
    with patch("xpst.analytics.AnalyticsCollector", return_value=failing):
        scheduler._maybe_capture_analytics()
        scheduler._maybe_capture_analytics()
    # Failure is isolated and still respects the cadence, avoiding a hot loop.
    assert failing.collect_all.await_count == 1


def test_snapshot_interval_validation_has_lower_and_upper_bounds():
    config = XPSTConfig()
    config.schedule.analytics_snapshot_interval = 59
    with pytest.raises(ValueError, match="snapshot interval"):
        config._validate()
    config.schedule.analytics_snapshot_interval = 86_401
    with pytest.raises(ValueError, match="snapshot interval"):
        config._validate()


def test_report_and_dashboard_payload_carry_freshness(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([_row("x", "x1", views=5)])
    dashboard = DashboardAnalytics(config_dir=str(tmp_path))
    payload = dashboard.get_analytics_payload(live=False)
    assert payload["last_captured"] is not None
    assert payload["staleness_hours"] is not None
    assert payload["summary"]["last_captured"] is not None


@pytest.mark.asyncio
async def test_youtube_hidden_counts_remain_missing(tmp_path):
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "youtube_token.json").write_text(json.dumps({"token": "fake"}))
    collector = AnalyticsCollector(config_dir=str(tmp_path))
    collector._owned_yt_ids = {"yt1"}
    collector._owned_yt_ids_ts = time.time()
    service = MagicMock()
    service.videos.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "yt1", "statistics": {"viewCount": "12"}}]
    }
    import google.oauth2.credentials
    import googleapiclient.discovery

    with patch.object(google.oauth2.credentials, "Credentials") as credentials_cls, patch.object(
        googleapiclient.discovery, "build", return_value=service
    ):
        credentials_cls.from_authorized_user_file.return_value = MagicMock()
        result = await collector._collect_youtube(["yt1"])

    assert result[0]["views"] == 12
    assert result[0].get("likes") is None
    assert result[0].get("comments") is None
