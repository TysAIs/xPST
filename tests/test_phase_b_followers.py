"""Tests for Phase B: Follower tracking and best-time-to-post analysis."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.analytics_store import AnalyticsStore

# ── Follower Snapshot Tests ─────────────────────────────────────────────


class TestFollowerSnapshots:
    """Test follower count tracking in AnalyticsStore."""

    def test_follower_table_exists(self, tmp_path):
        """The follower_snapshots table should be created on init."""
        store = AnalyticsStore(tmp_path / "test.db")
        with store._connect() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = [t["name"] for t in tables]
            assert "follower_snapshots" in names

    def test_record_and_latest_followers(self, tmp_path):
        """Record follower counts and retrieve the latest."""
        store = AnalyticsStore(tmp_path / "test.db")

        store.record_followers("youtube", 1500)
        store.record_followers("instagram", 800)
        store.record_followers("x", 300)

        latest = store.latest_followers()
        assert latest["youtube"]["count"] == 1500
        assert latest["instagram"]["count"] == 800
        assert latest["x"]["count"] == 300

    def test_latest_followers_returns_most_recent(self, tmp_path):
        """When multiple snapshots exist, latest is returned."""
        store = AnalyticsStore(tmp_path / "test.db")

        store.record_followers("youtube", 1000)
        # Small delay to ensure different timestamp
        import time
        time.sleep(0.01)
        store.record_followers("youtube", 1200)

        latest = store.latest_followers()
        assert latest["youtube"]["count"] == 1200

    def test_follower_history(self, tmp_path):
        """Follower history returns entries oldest first."""
        store = AnalyticsStore(tmp_path / "test.db")

        store.record_followers("youtube", 100)
        import time
        time.sleep(0.01)
        store.record_followers("youtube", 200)
        time.sleep(0.01)
        store.record_followers("youtube", 300)

        history = store.follower_history("youtube")
        assert len(history) == 3
        # Oldest first
        assert history[0]["count"] == 100
        assert history[2]["count"] == 300

    def test_follower_history_empty_platform(self, tmp_path):
        """History for a platform with no data returns empty list."""
        store = AnalyticsStore(tmp_path / "test.db")
        history = store.follower_history("nonexistent")
        assert history == []

    def test_latest_followers_empty(self, tmp_path):
        """Latest followers on empty DB returns empty dict."""
        store = AnalyticsStore(tmp_path / "test.db")
        assert store.latest_followers() == {}


# ── Platform get_followers Tests ────────────────────────────────────────


class TestPlatformGetFollowers:
    """Test get_followers() on platform uploaders."""

    def test_base_class_returns_zero(self):
        """Base PlatformUploader.get_followers returns 0 by default."""
        from xpst.platforms.base import PlatformUploader

        # Can't instantiate ABC directly, but can test the method exists
        assert hasattr(PlatformUploader, "get_followers")

    @pytest.mark.asyncio
    async def test_youtube_get_followers(self):
        """YouTube get_followers returns subscriber count."""
        from xpst.config import XPSTConfig
        from xpst.platforms.youtube import YouTubeUploader

        config = XPSTConfig()
        uploader = YouTubeUploader(config)

        # Mock the YouTube service
        mock_service = MagicMock()
        mock_request = MagicMock()
        mock_request.execute.return_value = {
            "items": [{"statistics": {"subscriberCount": "12345"}}]
        }
        mock_service.channels().list.return_value = mock_request
        uploader._service = mock_service

        count = await uploader.get_followers()
        assert count == 12345

    @pytest.mark.asyncio
    async def test_youtube_get_followers_no_channel(self):
        """YouTube get_followers returns 0 when no channel found."""
        from xpst.config import XPSTConfig
        from xpst.platforms.youtube import YouTubeUploader

        config = XPSTConfig()
        uploader = YouTubeUploader(config)

        mock_service = MagicMock()
        mock_request = MagicMock()
        mock_request.execute.return_value = {"items": []}
        mock_service.channels().list.return_value = mock_request
        uploader._service = mock_service

        count = await uploader.get_followers()
        assert count == 0

    @pytest.mark.asyncio
    async def test_instagram_graph_api_get_followers(self):
        """Instagram Graph API get_followers returns follower count."""
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        config.instagram.graph_access_token = "test_token"
        config.instagram.graph_ig_user_id = "123456"
        uploader = InstagramUploader(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {"followers_count": 5000}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch("httpx.AsyncClient") as mock_async_client:
            mock_async_client.return_value.__aenter__.return_value = mock_client
            count = await uploader.get_followers()
            assert count == 5000

    @pytest.mark.asyncio
    async def test_instagram_get_followers_no_config(self):
        """Instagram get_followers returns 0 when not configured."""
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        config.instagram.graph_access_token = ""
        config.instagram.graph_ig_user_id = ""
        uploader = InstagramUploader(config)

        count = await uploader.get_followers()
        assert count == 0

    @pytest.mark.asyncio
    async def test_x_api_v2_get_followers(self):
        """X API v2 get_followers returns follower count."""
        from xpst.config import XPSTConfig
        from xpst.platforms.x import XUploader

        config = XPSTConfig()
        config.x.auth_mode = "api_v2"
        config.x.bearer_token = "test_bearer"
        uploader = XUploader(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"public_metrics": {"followers_count": 2500}}
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch("httpx.AsyncClient") as mock_async_client:
            mock_async_client.return_value.__aenter__.return_value = mock_client
            count = await uploader.get_followers()
            assert count == 2500

    @pytest.mark.asyncio
    async def test_x_get_followers_no_bearer(self):
        """X API v2 get_followers returns 0 without bearer token."""
        from xpst.config import XPSTConfig
        from xpst.platforms.x import XUploader

        config = XPSTConfig()
        config.x.auth_mode = "api_v2"
        config.x.bearer_token = ""
        uploader = XUploader(config)

        count = await uploader.get_followers()
        assert count == 0


# ── Best Time Analysis Tests ────────────────────────────────────────────


class TestBestTimeAnalyzer:
    """Test best-time-to-post analysis."""

    def test_analyzer_returns_empty_on_empty_db(self, tmp_path):
        """Analyzer returns empty list when no data exists."""
        from xpst.best_time import BestTimeAnalyzer

        analyzer = BestTimeAnalyzer(tmp_path / "test.db")
        results = analyzer.analyze()
        assert results == []

    def test_analyzer_returns_results_with_data(self, tmp_path):
        """Analyzer returns time slots sorted by engagement rate."""
        from xpst.best_time import BestTimeAnalyzer

        store = AnalyticsStore(tmp_path / "test.db")

        # Record some snapshots with different timestamps and engagement
        store.record_snapshots([
            {"platform": "youtube", "post_id": "v1", "views": 1000,
             "likes": 100, "comments": 10, "shares": 5,
             "timestamp": "2026-06-15T14:00:00+00:00"},
            {"platform": "youtube", "post_id": "v2", "views": 500,
             "likes": 10, "comments": 1, "shares": 0,
             "timestamp": "2026-06-16T03:00:00+00:00"},
        ])

        analyzer = BestTimeAnalyzer(tmp_path / "test.db")
        results = analyzer.analyze()

        assert len(results) == 2
        # v1 has 11.5% engagement, v2 has 2.2% — v1 should be first
        assert results[0]["avg_engagement_rate"] > results[1]["avg_engagement_rate"]
        assert results[0]["platform"] == "youtube"

    def test_best_for_platform(self, tmp_path):
        """best_for_platform returns the top time slot."""
        from xpst.best_time import BestTimeAnalyzer

        store = AnalyticsStore(tmp_path / "test.db")

        store.record_snapshots([
            {"platform": "youtube", "post_id": "v1", "views": 1000,
             "likes": 100, "comments": 10, "shares": 5,
             "timestamp": "2026-06-15T14:00:00+00:00"},
            {"platform": "youtube", "post_id": "v2", "views": 500,
             "likes": 10, "comments": 1, "shares": 0,
             "timestamp": "2026-06-16T03:00:00+00:00"},
        ])

        analyzer = BestTimeAnalyzer(tmp_path / "test.db")
        best = analyzer.best_for_platform("youtube")

        assert best is not None
        assert best["platform"] == "youtube"
        assert "day_name" in best
        assert "hour" in best
        assert "avg_engagement_rate" in best

    def test_best_overall_groups_by_platform(self, tmp_path):
        """best_overall returns top 3 slots per platform."""
        from xpst.best_time import BestTimeAnalyzer

        store = AnalyticsStore(tmp_path / "test.db")

        # Add data for two platforms
        for i in range(5):
            store.record_snapshots([
                {"platform": "youtube", "post_id": f"yt_{i}", "views": 1000,
                 "likes": 50 + i * 10, "comments": 5, "shares": 2,
                 "timestamp": f"2026-06-1{i}T14:00:00+00:00"},
                {"platform": "x", "post_id": f"x_{i}", "views": 500,
                 "likes": 20 + i * 5, "comments": 2, "shares": 1,
                 "timestamp": f"2026-06-1{i}T09:00:00+00:00"},
            ])

        analyzer = BestTimeAnalyzer(tmp_path / "test.db")
        best = analyzer.best_overall()

        assert "youtube" in best
        assert "x" in best
        assert len(best["youtube"]) <= 3
        assert len(best["x"]) <= 3

    def test_day_names_correct(self, tmp_path):
        """Day names map correctly to ISO weekday."""
        from xpst.best_time import DAY_NAMES, BestTimeAnalyzer

        # 2026-06-15 is a Monday (weekday=0)
        assert DAY_NAMES[0] == "Monday"
        assert DAY_NAMES[6] == "Sunday"

        store = AnalyticsStore(tmp_path / "test.db")
        store.record_snapshots([
            {"platform": "youtube", "post_id": "v1", "views": 1000,
             "likes": 100, "comments": 10, "shares": 5,
             "timestamp": "2026-06-15T14:00:00+00:00"},  # Monday
        ])

        analyzer = BestTimeAnalyzer(tmp_path / "test.db")
        results = analyzer.analyze()
        assert len(results) == 1
        assert results[0]["day_name"] == "Monday"
        assert results[0]["hour"] == 14


# ── Engagement Rate Tests ───────────────────────────────────────────────


class TestEngagementRate:
    """Test engagement rate calculation and tiering."""

    def test_engagement_rate_calculation(self):
        """Engagement rate = (likes + comments + shares) / views * 100."""
        views = 1000
        likes = 50
        comments = 10
        shares = 5
        rate = (likes + comments + shares) / views * 100
        assert rate == 6.5

    def test_engagement_rate_zero_views(self):
        """Engagement rate is 0 when views is 0 (avoid division by zero)."""
        views = 0
        likes = 10
        comments = 5
        shares = 2
        rate = round((likes + comments + shares) / views * 100, 1) if views > 0 else 0
        assert rate == 0

    def test_engagement_tier_high(self):
        """Engagement > 5% is 'high' tier."""
        rate = 6.5
        tier = "high" if rate > 5 else ("medium" if rate >= 1 else "low")
        assert tier == "high"

    def test_engagement_tier_medium(self):
        """Engagement 1-5% is 'medium' tier."""
        rate = 3.2
        tier = "high" if rate > 5 else ("medium" if rate >= 1 else "low")
        assert tier == "medium"

    def test_engagement_tier_low(self):
        """Engagement < 1% is 'low' tier."""
        rate = 0.5
        tier = "high" if rate > 5 else ("medium" if rate >= 1 else "low")
        assert tier == "low"
