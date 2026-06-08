"""Tests for xPST analytics module.

Tests the unified AnalyticsCollector, per-platform collectors,
caching, parallel fetching, and graceful failure handling.
All platform API calls are mocked.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.analytics import AnalyticsCollector, PlatformMetrics

# ── PlatformMetrics ─────────────────────────────────────────────────────

class TestPlatformMetrics:
    """Test PlatformMetrics data class."""

    def test_basic_creation(self):
        m = PlatformMetrics(platform="youtube", post_id="abc123")
        assert m.platform == "youtube"
        assert m.post_id == "abc123"
        assert m.views == 0
        assert m.likes == 0
        assert m.comments == 0
        assert m.shares == 0
        assert m.saves == 0
        assert m.timestamp is not None

    def test_full_creation(self):
        m = PlatformMetrics(
            platform="instagram",
            post_id="12345",
            views=1000,
            likes=50,
            comments=10,
            shares=5,
            saves=3,
            timestamp="2025-01-01T00:00:00",
        )
        assert m.views == 1000
        assert m.likes == 50
        assert m.saves == 3

    def test_to_dict(self):
        m = PlatformMetrics(platform="x", post_id="999", views=42, likes=7)
        d = m.to_dict()
        assert d["platform"] == "x"
        assert d["post_id"] == "999"
        assert d["views"] == 42
        assert d["likes"] == 7
        assert "timestamp" in d

    def test_extra_kwargs(self):
        m = PlatformMetrics(platform="tiktok", post_id="tt1", bookmarks=5)
        d = m.to_dict()
        assert d["bookmarks"] == 5


# ── AnalyticsCollector initialization ───────────────────────────────────

class TestAnalyticsCollectorInit:
    """Test collector initialization and config loading."""

    def test_init_with_no_config(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert collector._config == {}
        assert collector._cache == {}

    def test_init_with_config(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "accounts:\n  tiktok:\n    username: test_user\n"
        )
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert collector._config["accounts"]["tiktok"]["username"] == "test_user"

    def test_cache_ttl(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path), cache_ttl=300)
        assert collector._cache_ttl == 300

    def test_is_cache_valid_empty(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert not collector._is_cache_valid()

    def test_is_cache_valid_fresh(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path), cache_ttl=900)
        collector._cache = {"youtube": {"vid1": {}}}
        collector._cache_time = time.time()
        assert collector._is_cache_valid()

    def test_is_cache_valid_expired(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path), cache_ttl=1)
        collector._cache = {"youtube": {"vid1": {}}}
        collector._cache_time = time.time() - 10
        assert not collector._is_cache_valid()


# ── Discover post IDs ───────────────────────────────────────────────────

class TestDiscoverPostIds:
    """Test post ID discovery from state.json."""

    def test_no_state_file(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        ids = collector._discover_post_ids()
        assert ids == {}

    def test_empty_state(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"posted_videos": {}}))
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        ids = collector._discover_post_ids()
        assert ids["youtube"] == []
        assert ids["instagram"] == []

    def test_with_posted_videos(self, tmp_path):
        state = {
            "posted_videos": {
                "vid1": {
                    "posted_to": {
                        "youtube": {"post_id": "yt_abc", "url": "..."},
                        "instagram": {"post_id": "ig_123", "url": "..."},
                    }
                },
                "vid2": {
                    "posted_to": {
                        "youtube": {"post_id": "yt_def", "url": "..."},
                        "x": {"post_id": "x_456", "url": "..."},
                        "tiktok": {"post_id": "tt_789", "url": "..."},
                    }
                },
            }
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(state))
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        ids = collector._discover_post_ids()
        assert set(ids["youtube"]) == {"yt_abc", "yt_def"}
        assert ids["instagram"] == ["ig_123"]
        assert ids["x"] == ["x_456"]
        assert ids["tiktok"] == ["tt_789"]

    def test_corrupt_state(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("not valid json{{{")
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        ids = collector._discover_post_ids()
        assert ids == {}


# ── YouTube collection ──────────────────────────────────────────────────

class TestCollectYouTube:
    """Test YouTube metrics collection with mocked API."""

    @pytest.mark.asyncio
    async def test_collect_youtube_success(self, tmp_path):
        # Setup credentials dir
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        token_path = creds_dir / "youtube_token.json"
        token_path.write_text(json.dumps({
            "token": "fake",
            "refresh_token": "fake",
            "client_id": "fake",
            "client_secret": "fake",
            "token_uri": "https://oauth2.googleapis.com/token",
        }))

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_response = {
            "items": [
                {
                    "id": "vid1",
                    "statistics": {
                        "viewCount": "1000",
                        "likeCount": "50",
                        "commentCount": "10",
                    },
                },
                {
                    "id": "vid2",
                    "statistics": {
                        "viewCount": "5000",
                        "likeCount": "200",
                        "commentCount": "30",
                    },
                },
            ]
        }

        mock_service = MagicMock()
        mock_service.videos.return_value.list.return_value.execute.return_value = mock_response

        # Patch the modules that get imported inside _collect_youtube
        import google.oauth2.credentials
        import googleapiclient.discovery

        with patch.object(google.oauth2.credentials, "Credentials") as mock_creds_cls, \
             patch.object(googleapiclient.discovery, "build", return_value=mock_service):
            mock_creds_cls.from_authorized_user_file.return_value = MagicMock(valid=True)
            result = await collector._collect_youtube(["vid1", "vid2"])

        assert len(result) == 2
        assert result[0]["platform"] == "youtube"
        assert result[0]["views"] == 1000
        assert result[0]["likes"] == 50
        assert result[0]["comments"] == 10
        assert result[1]["views"] == 5000

    @pytest.mark.asyncio
    async def test_collect_youtube_no_token(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        result = await collector._collect_youtube(["vid1"])
        assert result == []

    @pytest.mark.asyncio
    async def test_collect_youtube_api_error(self, tmp_path):
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        token_path = creds_dir / "youtube_token.json"
        token_path.write_text('{"token": "fake"}')

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        import google.oauth2.credentials
        import googleapiclient.discovery

        with patch.object(google.oauth2.credentials, "Credentials") as mock_creds_cls, \
             patch.object(googleapiclient.discovery, "build", side_effect=Exception("API Error")):
            mock_creds_cls.from_authorized_user_file.return_value = MagicMock()
            result = await collector._collect_youtube(["vid1"])

        assert result == []


# ── Instagram collection ────────────────────────────────────────────────

class TestCollectInstagram:
    """Test Instagram metrics collection with mocked instagrapi."""

    @pytest.mark.asyncio
    async def test_collect_instagram_success(self, tmp_path):
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        session_path = creds_dir / "instagram_session.json"
        session_path.write_text(json.dumps({
            "authorization_data": {"sessionid": "fake_session"}
        }))

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_info = MagicMock()
        mock_info.like_count = 100
        mock_info.comment_count = 20

        mock_client = MagicMock()
        mock_client.media_info.return_value = mock_info
        mock_client.insights.get_media_insights.return_value = {
            "data": [
                {"name": "impressions", "values": [{"value": 5000}]},
                {"name": "saved", "values": [{"value": 30}]},
                {"name": "shares", "values": [{"value": 15}]},
            ]
        }

        import instagrapi
        with patch.object(instagrapi, "Client", return_value=mock_client):
            result = await collector._collect_instagram(["12345"])

        assert len(result) == 1
        assert result[0]["platform"] == "instagram"
        assert result[0]["views"] == 5000  # from impressions
        assert result[0]["likes"] == 100
        assert result[0]["comments"] == 20
        assert result[0]["saves"] == 30
        assert result[0]["shares"] == 15

    @pytest.mark.asyncio
    async def test_collect_instagram_no_session(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        result = await collector._collect_instagram(["12345"])
        assert result == []

    @pytest.mark.asyncio
    async def test_collect_instagram_fallback_no_insights(self, tmp_path):
        """Test fallback when insights API fails."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        session_path = creds_dir / "instagram_session.json"
        session_path.write_text(json.dumps({
            "authorization_data": {"sessionid": "fake_session"}
        }))

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_info = MagicMock()
        mock_info.like_count = 50
        mock_info.comment_count = 5

        mock_client = MagicMock()
        mock_client.media_info.return_value = mock_info
        mock_client.insights.get_media_insights.side_effect = Exception("Not business account")

        import instagrapi
        with patch.object(instagrapi, "Client", return_value=mock_client):
            result = await collector._collect_instagram(["12345"])

        assert len(result) == 1
        assert result[0]["likes"] == 50
        assert result[0]["views"] == 0  # No impressions from insights


# ── X/Twitter collection ────────────────────────────────────────────────

class TestCollectX:
    """Test X/Twitter metrics collection with mocked twikit."""

    @pytest.mark.asyncio
    async def test_collect_x_success(self, tmp_path):
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        cookies_path = creds_dir / "x_cookies.json"
        cookies_path.write_text(json.dumps({"auth_token": "fake"}))

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_tweet = MagicMock()
        mock_tweet.view_count = 10000
        mock_tweet.favorite_count = 500
        mock_tweet.reply_count = 25
        mock_tweet.retweet_count = 100

        mock_client = MagicMock()
        mock_client.get_tweet_by_id = AsyncMock(return_value=mock_tweet)

        import twikit
        with patch.object(twikit, "Client", return_value=mock_client):
            result = await collector._collect_x(["99999"])

        assert len(result) == 1
        assert result[0]["platform"] == "x"
        assert result[0]["views"] == 10000
        assert result[0]["likes"] == 500
        assert result[0]["comments"] == 25
        assert result[0]["shares"] == 100

    @pytest.mark.asyncio
    async def test_collect_x_no_cookies(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        result = await collector._collect_x(["99999"])
        assert result == []

    @pytest.mark.asyncio
    async def test_collect_x_partial_failure(self, tmp_path):
        """Test that one failed tweet doesn't block others."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        cookies_path = creds_dir / "x_cookies.json"
        cookies_path.write_text(json.dumps({"auth_token": "fake"}))

        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_tweet = MagicMock()
        mock_tweet.view_count = 5000
        mock_tweet.favorite_count = 100
        mock_tweet.reply_count = 10
        mock_tweet.retweet_count = 20

        async def mock_get_tweet(tweet_id):
            if tweet_id == "bad_id":
                raise Exception("Tweet not found")
            return mock_tweet

        mock_client = MagicMock()
        mock_client.get_tweet_by_id = AsyncMock(side_effect=mock_get_tweet)

        import twikit
        with patch.object(twikit, "Client", return_value=mock_client):
            result = await collector._collect_x(["bad_id", "good_id"])

        assert len(result) == 1
        assert result[0]["post_id"] == "good_id"


# ── TikTok collection ──────────────────────────────────────────────────

class TestCollectTikTok:
    """Test TikTok metrics collection with mocked yt-dlp."""

    @pytest.mark.asyncio
    async def test_collect_tiktok_success(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_info = {
            "view_count": 50000,
            "like_count": 2000,
            "comment_count": 300,
            "repost_count": 100,
        }

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)

        import yt_dlp
        with patch.object(yt_dlp, "YoutubeDL", return_value=mock_ydl):
            result = await collector._collect_tiktok(["123456789"])

        assert len(result) == 1
        assert result[0]["platform"] == "tiktok"
        assert result[0]["views"] == 50000
        assert result[0]["likes"] == 2000

    @pytest.mark.asyncio
    async def test_collect_tiktok_no_ytdlp(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "yt_dlp":
                raise ImportError("No module named 'yt_dlp'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = await collector._collect_tiktok(["123456789"])

        assert result == []


# ── Parallel collection (collect_all) ───────────────────────────────────

class TestCollectAll:
    """Test parallel collection across all platforms."""

    @pytest.mark.asyncio
    async def test_collect_all_parallel(self, tmp_path):
        """Verify all platforms are fetched in parallel."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_yt = [
            {"platform": "youtube", "post_id": "vid1", "views": 100, "likes": 10,
             "comments": 2, "shares": 0, "timestamp": "2025-01-01"}
        ]
        mock_ig = [
            {"platform": "instagram", "post_id": "ig1", "views": 200, "likes": 20,
             "comments": 5, "shares": 3, "timestamp": "2025-01-01"}
        ]
        mock_x = [
            {"platform": "x", "post_id": "tweet1", "views": 500, "likes": 50,
             "comments": 8, "shares": 15, "timestamp": "2025-01-01"}
        ]
        mock_tt = [
            {"platform": "tiktok", "post_id": "tt1", "views": 1000, "likes": 100,
             "comments": 20, "shares": 5, "timestamp": "2025-01-01"}
        ]

        with patch.object(collector, "_collect_youtube", return_value=mock_yt), \
             patch.object(collector, "_collect_instagram", return_value=mock_ig), \
             patch.object(collector, "_collect_x", return_value=mock_x), \
             patch.object(collector, "_collect_tiktok", return_value=mock_tt):

            data = await collector.collect_all({
                "youtube": ["vid1"],
                "instagram": ["ig1"],
                "x": ["tweet1"],
                "tiktok": ["tt1"],
            })

        assert "youtube" in data
        assert "instagram" in data
        assert "x" in data
        assert "tiktok" in data
        assert data["youtube"]["vid1"]["views"] == 100
        assert data["instagram"]["ig1"]["views"] == 200
        assert data["x"]["tweet1"]["views"] == 500
        assert data["tiktok"]["tt1"]["views"] == 1000

    @pytest.mark.asyncio
    async def test_collect_all_caching(self, tmp_path):
        """Verify caching returns cached data on second call."""
        collector = AnalyticsCollector(config_dir=str(tmp_path), cache_ttl=900)

        call_count = 0

        async def mock_collect_youtube(ids):
            nonlocal call_count
            call_count += 1
            return [{"platform": "youtube", "post_id": "v1", "views": 42,
                     "likes": 1, "comments": 0, "shares": 0, "timestamp": ""}]

        with patch.object(collector, "_collect_youtube", side_effect=mock_collect_youtube), \
             patch.object(collector, "_collect_instagram", return_value=[]), \
             patch.object(collector, "_collect_x", return_value=[]), \
             patch.object(collector, "_collect_tiktok", return_value=[]):

            data1 = await collector.collect_all({"youtube": ["v1"], "instagram": [], "x": [], "tiktok": []})
            data2 = await collector.collect_all({"youtube": ["v1"], "instagram": [], "x": [], "tiktok": []})

        assert call_count == 1  # Only called once due to cache
        assert data1 == data2

    @pytest.mark.asyncio
    async def test_collect_all_graceful_failure(self, tmp_path):
        """One platform failing doesn't break others."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_yt = [{"platform": "youtube", "post_id": "v1", "views": 100,
                     "likes": 10, "comments": 2, "shares": 0, "timestamp": ""}]

        with patch.object(collector, "_collect_youtube", return_value=mock_yt), \
             patch.object(collector, "_collect_instagram", side_effect=Exception("IG down")), \
             patch.object(collector, "_collect_x", return_value=[]), \
             patch.object(collector, "_collect_tiktok", return_value=[]):

            data = await collector.collect_all({
                "youtube": ["v1"],
                "instagram": ["ig1"],
                "x": [],
                "tiktok": [],
            })

        assert data["youtube"]["v1"]["views"] == 100
        assert data["instagram"] == {}  # Empty due to failure

    @pytest.mark.asyncio
    async def test_collect_all_empty_ids(self, tmp_path):
        """Empty post IDs returns empty result."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        data = await collector.collect_all({
            "youtube": [],
            "instagram": [],
            "x": [],
            "tiktok": [],
        })

        # No tasks created since all ID lists are empty
        assert data == {}

    @pytest.mark.asyncio
    async def test_collect_all_partial_platforms(self, tmp_path):
        """Only platforms with IDs get fetched."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_yt = [{"platform": "youtube", "post_id": "v1", "views": 100,
                     "likes": 10, "comments": 2, "shares": 0, "timestamp": ""}]

        with patch.object(collector, "_collect_youtube", return_value=mock_yt) as mock_yt_fn, \
             patch.object(collector, "_collect_instagram") as mock_ig_fn, \
             patch.object(collector, "_collect_x") as mock_x_fn, \
             patch.object(collector, "_collect_tiktok") as mock_tt_fn:

            await collector.collect_all({
                "youtube": ["v1"],
                "instagram": [],
                "x": [],
                "tiktok": [],
            })

        mock_yt_fn.assert_called_once()
        mock_ig_fn.assert_not_called()
        mock_x_fn.assert_not_called()
        mock_tt_fn.assert_not_called()


# ── Aggregation helpers ─────────────────────────────────────────────────

class TestAggregation:
    """Test metric aggregation helpers."""

    def test_get_total_metrics(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        data = {
            "youtube": {"v1": {"views": 100, "likes": 10, "comments": 2, "shares": 0}},
            "instagram": {"ig1": {"views": 200, "likes": 20, "comments": 5, "shares": 3}},
        }
        totals = collector.get_total_metrics(data)
        assert totals["views"] == 300
        assert totals["likes"] == 30
        assert totals["comments"] == 7
        assert totals["shares"] == 3

    def test_get_total_metrics_empty(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        totals = collector.get_total_metrics({})
        assert totals == {"views": 0, "likes": 0, "comments": 0, "shares": 0}

    def test_get_platform_totals(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        data = {
            "youtube": {
                "v1": {"views": 100, "likes": 10, "comments": 2, "shares": 0},
                "v2": {"views": 200, "likes": 20, "comments": 4, "shares": 1},
            },
            "x": {
                "t1": {"views": 500, "likes": 50, "comments": 8, "shares": 15},
            },
        }
        pt = collector.get_platform_totals(data)
        assert pt["youtube"]["posts"] == 2
        assert pt["youtube"]["views"] == 300
        assert pt["x"]["posts"] == 1
        assert pt["x"]["views"] == 500

    def test_get_platform_totals_empty(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        pt = collector.get_platform_totals({})
        assert pt == {}


# ── Platform uploader analytics methods ─────────────────────────────────

class TestPlatformUploaderAnalytics:
    """Test core methods exist on platform uploader classes."""

    def test_youtube_upload_method_exists(self):
        """YouTubeUploader should have upload and delete."""
        from xpst.platforms.youtube import YouTubeUploader
        assert hasattr(YouTubeUploader, "upload")
        assert hasattr(YouTubeUploader, "delete")

    def test_instagram_upload_method_exists(self):
        """InstagramUploader should have upload and delete."""
        from xpst.platforms.instagram import InstagramUploader
        assert hasattr(InstagramUploader, "upload")
        assert hasattr(InstagramUploader, "delete")

    def test_x_upload_method_exists(self):
        """XUploader should have upload and delete."""
        from xpst.platforms.x import XUploader
        assert hasattr(XUploader, "upload")
        assert hasattr(XUploader, "delete")


# ── CLI analytics command ───────────────────────────────────────────────

class TestCLIAnalytics:
    """Test that the CLI analytics command is registered."""

    def test_analytics_command_exists(self):
        """CLI should have an analytics command."""
        from xpst.cli import main
        # Check that 'analytics' is in the command names
        cmd_names = list(main.commands.keys())
        assert "analytics" in cmd_names
