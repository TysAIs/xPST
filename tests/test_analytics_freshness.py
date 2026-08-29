"""Analytics freshness: scheduled snapshot capture, staleness surface, and
honesty-leak regressions (audit-map/analytics-accuracy-2026-08-28.md).

Covers:
1. Scheduler ``_maybe_capture_analytics`` — config gate, interval gating,
   collect_all wiring, failure isolation.
2. Freshness fields (last_captured / staleness_hours / relative) on the
   /state summary and desktop analytics payloads.
3. Honesty leaks: YouTube hidden like/comment counts stored as MISSING not 0;
   IG fallback omits (not zeroes) reach/saves/shares; lineup state-only
   entries carry None metrics, not fabricated zeros.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from xpst.analytics import AnalyticsCollector
from xpst.analytics_store import AnalyticsStore
from xpst.config import XPSTConfig
from xpst.dashboard.analytics import AnalyticsCollector as DashboardAnalytics
from xpst.scheduler import Scheduler

# ── 1. Scheduler: optional snapshot capture ──────────────────────────────


def _scheduler(tmp_path, enabled: bool, interval: int = 3600) -> Scheduler:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.schedule.analytics_snapshot_enabled = enabled
    config.schedule.analytics_snapshot_interval = interval
    engine = MagicMock()
    engine.state.save = MagicMock()
    return Scheduler(engine=engine, config=config)


class TestScheduledCapture:
    def test_disabled_by_default(self):
        config = XPSTConfig()
        assert config.schedule.analytics_snapshot_enabled is False
        assert config.schedule.analytics_snapshot_interval == 3600

    def test_disabled_never_collects(self, tmp_path):
        sched = _scheduler(tmp_path, enabled=False)
        with patch("xpst.analytics.AnalyticsCollector") as mock_cls:
            sched._maybe_capture_analytics()
            mock_cls.assert_not_called()

    def test_enabled_collects_all(self, tmp_path):
        sched = _scheduler(tmp_path, enabled=True)
        collector = MagicMock()
        collector.collect_all = MagicMock(
            return_value={"youtube": {"v1": {"views": 1}}, "x": {"t1": {"views": 2}}}
        )
        with patch("xpst.analytics.AnalyticsCollector", return_value=collector) as mock_cls:
            sched._maybe_capture_analytics()
            mock_cls.assert_called_once()
            assert mock_cls.call_args.kwargs["config_dir"] == str(tmp_path)
            collector.collect_all.assert_called_once()
        assert sched._last_snapshot_capture is not None

    def test_interval_gate_skips_early_call(self, tmp_path):
        sched = _scheduler(tmp_path, enabled=True, interval=3600)
        sched._last_snapshot_capture = time.monotonic()
        with patch("xpst.analytics.AnalyticsCollector") as mock_cls:
            sched._maybe_capture_analytics()
            mock_cls.assert_not_called()

    def test_failure_never_raises(self, tmp_path):
        sched = _scheduler(tmp_path, enabled=True)
        with patch(
            "xpst.analytics.AnalyticsCollector", side_effect=RuntimeError("boom")
        ):
            # Must not raise — the watch loop must survive capture failures.
            sched._maybe_capture_analytics()

    def test_config_validation_rejects_tiny_interval(self, tmp_path):
        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.schedule.analytics_snapshot_interval = 10
        with pytest.raises(ValueError, match="snapshot interval"):
            config._validate()

    def test_config_file_roundtrip(self, tmp_path):
        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.schedule.analytics_snapshot_enabled = True
        config.schedule.analytics_snapshot_interval = 7200
        config.save(str(tmp_path / "config.yaml"))

        text = (tmp_path / "config.yaml").read_text()
        assert "analytics_snapshot_enabled" in text
        assert "analytics_snapshot_interval" in text


# ── 2. Freshness surface ─────────────────────────────────────────────────


class TestFreshness:
    def _dashboard(self, tmp_path) -> DashboardAnalytics:
        return DashboardAnalytics(config_dir=str(tmp_path))

    def test_no_snapshots_returns_none_fields(self, tmp_path):
        d = self._dashboard(tmp_path)
        stats = d.get_summary_stats()
        assert stats["last_captured"] is None
        assert stats["staleness_hours"] is None

    def test_fresh_snapshot_reports_low_staleness(self, tmp_path):
        store = AnalyticsStore(tmp_path / "analytics.db")
        store.record_snapshots([
            {"platform": "x", "post_id": "t1", "views": 5,
             "timestamp": datetime.now().isoformat()},
        ])
        d = self._dashboard(tmp_path)
        stats = d.get_summary_stats()
        assert stats["last_captured"] is not None
        assert stats["staleness_hours"] is not None
        assert 0 <= stats["staleness_hours"] < 1
        assert stats["last_captured_relative"] == "just now"

    def test_stale_snapshot_reports_hours(self, tmp_path):
        store = AnalyticsStore(tmp_path / "analytics.db")
        old = (datetime.now() - timedelta(hours=30)).isoformat()
        store.record_snapshots([
            {"platform": "x", "post_id": "t1", "views": 5, "timestamp": old},
        ])
        d = self._dashboard(tmp_path)
        stats = d.get_summary_stats()
        assert stats["staleness_hours"] >= 24
        assert stats["last_captured_relative"].endswith("d ago") or \
            stats["last_captured_relative"].endswith("h ago")

    def test_analytics_payload_carries_freshness(self, tmp_path):
        store = AnalyticsStore(tmp_path / "analytics.db")
        store.record_snapshots([
            {"platform": "x", "post_id": "t1", "views": 5,
             "timestamp": datetime.now().isoformat()},
        ])
        d = self._dashboard(tmp_path)
        payload = d.get_analytics_payload(live=False)
        assert payload["last_captured"] is not None
        assert payload["staleness_hours"] is not None
        assert payload["summary"]["last_captured"] is not None

    def test_store_last_captured_at_empty_db(self, tmp_path):
        store = AnalyticsStore(tmp_path / "analytics.db")
        assert store.last_captured_at() is None


# ── 3. Honesty leaks ─────────────────────────────────────────────────────


class TestYoutubeHiddenCounts:
    """YouTube omits likeCount/commentCount when the creator hides them —
    the collector must store MISSING (absent key), never a fabricated 0."""

    @pytest.mark.asyncio
    async def test_hidden_likes_comments_absent_not_zero(self, tmp_path):
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "youtube_token.json").write_text(json.dumps({
            "token": "fake", "refresh_token": "fake", "client_id": "fake",
            "client_secret": "fake",
            "token_uri": "https://oauth2.googleapis.com/token",
        }))

        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._owned_yt_ids = {"vid1"}
        collector._owned_yt_ids_ts = time.time()

        mock_response = {
            "items": [{
                "id": "vid1",
                # Creator hides likes and comments → keys omitted by YouTube
                "statistics": {"viewCount": "900"},
            }],
        }
        mock_service = MagicMock()
        mock_service.videos.return_value.list.return_value.execute.return_value = mock_response

        import google.oauth2.credentials
        import googleapiclient.discovery

        with patch.object(google.oauth2.credentials, "Credentials") as mock_creds_cls, \
             patch.object(googleapiclient.discovery, "build", return_value=mock_service):
            mock_creds_cls.from_authorized_user_file.return_value = MagicMock(valid=True)
            result = await collector._collect_youtube(["vid1"])

        assert len(result) == 1
        row = result[0]
        assert row["views"] == 900
        assert "likes" not in row, "hidden likes must be MISSING, not 0"
        assert "comments" not in row, "hidden comments must be MISSING, not 0"

    @pytest.mark.asyncio
    async def test_present_counts_still_recorded(self, tmp_path):
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "youtube_token.json").write_text(json.dumps({
            "token": "fake", "refresh_token": "fake", "client_id": "fake",
            "client_secret": "fake",
            "token_uri": "https://oauth2.googleapis.com/token",
        }))

        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._owned_yt_ids = {"vid1"}
        collector._owned_yt_ids_ts = time.time()

        mock_response = {
            "items": [{
                "id": "vid1",
                "statistics": {"viewCount": "10", "likeCount": "2", "commentCount": "1"},
            }],
        }
        mock_service = MagicMock()
        mock_service.videos.return_value.list.return_value.execute.return_value = mock_response

        import google.oauth2.credentials
        import googleapiclient.discovery

        with patch.object(google.oauth2.credentials, "Credentials") as mock_creds_cls, \
             patch.object(googleapiclient.discovery, "build", return_value=mock_service):
            mock_creds_cls.from_authorized_user_file.return_value = MagicMock(valid=True)
            result = await collector._collect_youtube(["vid1"])

        assert result[0]["likes"] == 2
        assert result[0]["comments"] == 1

    def test_missing_key_persists_as_null_not_zero(self, tmp_path):
        """Absent likes in a snapshot row must not read back as a real 0."""
        store = AnalyticsStore(tmp_path / "analytics.db")
        store.record_snapshots([
            {"platform": "youtube", "post_id": "v1", "views": 900},
        ])
        row = store.latest("youtube")[0]
        assert row.get("likes") is None
        assert row.get("views") == 900


class TestInstagramFallbackOmission:
    """IG fallback must OMIT (not zero) insights-gated reach/saves/shares."""

    @pytest.mark.asyncio
    async def test_fallback_omits_shares_saves(self, tmp_path):
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "instagram_session.json").write_text(json.dumps({
            "authorization_data": {"sessionid": "fake_session"},
        }))

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_info = MagicMock()
        mock_info.like_count = 50
        mock_info.comment_count = 5
        mock_info.play_count = 740

        import instagrapi

        mock_client = MagicMock(spec=instagrapi.Client)
        mock_client.media_info.return_value = mock_info
        mock_client.insights_media.side_effect = Exception("Not business account")

        with patch.object(instagrapi, "Client", return_value=mock_client):
            result = await collector._collect_instagram(["12345"])

        assert len(result) == 1
        row = result[0]
        assert row["views"] == 740
        assert "shares" not in row, "unavailable shares must be omitted, not 0"
        assert "saves" not in row, "unavailable saves must be omitted, not 0"

    @pytest.mark.asyncio
    async def test_insights_values_still_included(self, tmp_path):
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "instagram_session.json").write_text(json.dumps({
            "authorization_data": {"sessionid": "fake_session"},
        }))

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_info = MagicMock()
        mock_info.like_count = 50
        mock_info.comment_count = 5
        mock_info.play_count = 740

        import instagrapi

        mock_client = MagicMock(spec=instagrapi.Client)
        mock_client.media_info.return_value = mock_info
        mock_client.insights_media.return_value = {
            "data": [
                {"name": "shares", "values": [{"value": 12}]},
                {"name": "saved", "values": [{"value": 3}]},
            ],
        }

        with patch.object(instagrapi, "Client", return_value=mock_client):
            result = await collector._collect_instagram(["12345"])

        row = result[0]
        assert row["shares"] == 12
        assert row["saves"] == 3


class TestLineupStateOnlyNoFakeZeros:
    """Lineup state-only entries (no snapshots yet) must carry None metrics,
    not hardcoded 0s that read as measured zeros."""

    def test_state_only_entry_metrics_are_none(self, tmp_path):
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {
                "abc123": {
                    "caption": "hello",
                    "downloaded_at": datetime.now().isoformat(),
                    "posted_to": {
                        "tiktok": {"id": "tt999", "timestamp": datetime.now().isoformat()},
                    },
                },
            },
        }))
        d = DashboardAnalytics(config_dir=str(tmp_path))
        lineup = d.get_video_lineup()
        entry = next(e for e in lineup if e["post_id"] == "tt999")
        assert entry["views"] is None
        assert entry["likes"] is None
        assert entry["comments"] is None
        assert entry["shares"] is None

    def test_snapshotted_entry_keeps_real_metrics(self, tmp_path):
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {
                "abc123": {
                    "caption": "hello",
                    "downloaded_at": datetime.now().isoformat(),
                    "posted_to": {
                        "tiktok": {"id": "tt999", "timestamp": datetime.now().isoformat()},
                    },
                },
            },
        }))
        store = AnalyticsStore(tmp_path / "analytics.db")
        store.record_snapshots([
            {"platform": "tiktok", "post_id": "tt999", "views": 1234, "likes": 5},
        ])
        d = DashboardAnalytics(config_dir=str(tmp_path))
        lineup = d.get_video_lineup()
        entry = next(e for e in lineup if e["post_id"] == "tt999")
        assert entry["views"] == 1234
        assert entry["likes"] == 5
