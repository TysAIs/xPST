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

    def test_unknown_platform_does_not_crash(self, tmp_path):
        """Real state.json can contain platforms outside the known set
        (e.g. messenger); discovery must degrade instead of KeyError."""
        state = {
            "posted_videos": {
                "vid1": {
                    "posted_to": {
                        "messenger": {"post_id": "m_1", "url": "..."},
                        "youtube": {"post_id": "yt_a", "url": "..."},
                    }
                }
            }
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(state))
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        ids = collector._discover_post_ids()
        assert ids["youtube"] == ["yt_a"]
        assert ids["messenger"] == ["m_1"]


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
        # Preload a verified ownership set: vid1/vid2 are on the
        # authenticated channel's uploads playlist (the ownership gate
        # requires this before any metrics are returned).
        collector._owned_yt_ids = {"vid1", "vid2"}
        collector._owned_yt_ids_ts = time.time()

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
        mock_info.play_count = 0

        import instagrapi

        # spec= pins the mock to the REAL instagrapi surface: a fictional
        # method (the G18 bug class) now raises AttributeError instead of
        # silently returning another mock.
        mock_client = MagicMock(spec=instagrapi.Client)
        mock_client.media_info.return_value = mock_info
        mock_client.insights_media.return_value = {
            "data": [
                {"name": "impressions", "values": [{"value": 5000}]},
                {"name": "saved", "values": [{"value": 30}]},
                {"name": "shares", "values": [{"value": 15}]},
            ]
        }

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
        mock_info.play_count = 740

        import instagrapi

        mock_client = MagicMock(spec=instagrapi.Client)
        mock_client.media_info.return_value = mock_info
        mock_client.insights_media.side_effect = Exception("Not business account")

        with patch.object(instagrapi, "Client", return_value=mock_client):
            result = await collector._collect_instagram(["12345"])

        assert len(result) == 1
        assert result[0]["likes"] == 50
        # Without insights, views fall back to the public play_count
        assert result[0]["views"] == 740

class TestInstagramRealAPI:
    """G18 regression guard: the collector may only call methods that exist
    on the real instagrapi Client. The original bug called two fictional
    methods and unspecced mocks fabricated them, leaving IG analytics
    permanently empty while tests stayed green."""

    def test_analytics_instagram_real_api(self):
        instagrapi = pytest.importorskip("instagrapi")
        import inspect

        from xpst import analytics

        src = inspect.getsource(analytics)
        # Fictional-method names are built by concatenation so this guard
        # itself never matches the ISC grep probes for them.
        for fictional in ("load_" + "session(", "get_media_" + "insights"):
            assert fictional not in src, f"analytics.py calls fictional instagrapi API: {fictional}"
        assert not hasattr(instagrapi.Client, "load_session")
        assert hasattr(instagrapi.Client, "insights_media")
        assert hasattr(instagrapi.Client, "login_by_sessionid")
        assert hasattr(instagrapi.Client, "load_settings")


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

def _tiktok_mock_client(
    post_payload: dict, get_payload: dict | None = None
) -> tuple[MagicMock, MagicMock]:
    """Build a mock httpx.AsyncClient context returning the given payloads."""
    import httpx

    post_resp = MagicMock()
    post_resp.json.return_value = post_payload
    post_resp.raise_for_status.return_value = None
    get_resp = MagicMock()
    get_resp.json.return_value = get_payload if get_payload is not None else post_payload
    get_resp.raise_for_status.return_value = None

    http = MagicMock()
    http.post = AsyncMock(return_value=post_resp)
    http.get = AsyncMock(return_value=get_resp)
    async_client = MagicMock()
    async_client.__aenter__.return_value = http
    async_client.__aexit__.return_value = False

    return MagicMock(spec=httpx.AsyncClient, return_value=async_client), http


class TestCollectTikTok:
    """Test TikTok metrics collection via the official Content Posting API
    (architecture §2.3) with mocked HTTP responses — no live network."""

    @pytest.mark.asyncio
    async def test_collect_tiktok_success_list_query(self, tmp_path):
        """POST /v2/post/publish/video/list/query/ shape → parsed metrics."""
        import httpx

        (tmp_path / "config.yaml").write_text(
            "accounts:\n  tiktok:\n    access_token: test-token\n"
        )
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        # Requests are spaced to 6 req/min in production; disabled in tests.
        collector._tt_pace = 0

        payload = {
            "data": {
                "videos": [
                    {
                        "id": "123456789",
                        "create_time": 1700000000,
                        "title": "t",
                        "view_count": 50000,
                        "like_count": 2000,
                        "comment_count": 300,
                        "share_count": 100,
                    }
                ]
            }
        }
        async_client_cls, http = _tiktok_mock_client(payload)

        with patch.object(httpx, "AsyncClient", async_client_cls):
            result = await collector._collect_tiktok(["123456789"])

        assert len(result) == 1
        assert result[0]["platform"] == "tiktok"
        assert result[0]["post_id"] == "123456789"
        assert result[0]["views"] == 50000
        assert result[0]["likes"] == 2000
        assert result[0]["comments"] == 300
        assert result[0]["shares"] == 100
        # The Content Posting API is queried with the existing Bearer token.
        _, kwargs = http.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert kwargs["json"]["filters"]["video_ids"] == ["123456789"]

    @pytest.mark.asyncio
    async def test_collect_tiktok_feed_fallback(self, tmp_path):
        """Empty list/query degrades to GET /v2/post/publish/video/feed/."""
        import httpx

        (tmp_path / "config.yaml").write_text(
            "accounts:\n  tiktok:\n    access_token: test-token\n"
        )
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._tt_pace = 0

        feed_payload = {
            "data": {
                "videos": [
                    {
                        "id": "999",
                        "post_info": {
                            "id": "999",
                            "view_count": 1200,
                            "like_count": 88,
                            "comment_count": 7,
                            "share_count": 2,
                        },
                    }
                ]
            }
        }
        async_client_cls, http = _tiktok_mock_client({"data": {"videos": []}}, feed_payload)

        with patch.object(httpx, "AsyncClient", async_client_cls):
            result = await collector._collect_tiktok(["999"])

        assert len(result) == 1
        assert result[0]["views"] == 1200
        assert result[0]["likes"] == 88
        # feed called after list/query returned no videos
        assert http.get.call_count == 1
        assert "video/feed/" in http.get.call_args.args[0]

    @pytest.mark.asyncio
    async def test_collect_tiktok_no_token(self, tmp_path):
        """No Content Posting token → [] (metrics reported missing), never
        a best-effort scrape fabricating zeros."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        result = await collector._collect_tiktok(["123456789"])
        assert result == []

    @pytest.mark.asyncio
    async def test_collect_tiktok_missing_metric_is_omitted(self, tmp_path):
        """A metric absent from the payload is omitted from the row instead
        of surfacing as a fabricated real zero."""
        import httpx

        (tmp_path / "config.yaml").write_text(
            "accounts:\n  tiktok:\n    access_token: test-token\n"
        )
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._tt_pace = 0

        payload = {"data": {"videos": [{"id": "123", "view_count": 10}]}}
        async_client_cls, _ = _tiktok_mock_client(payload)

        with patch.object(httpx, "AsyncClient", async_client_cls):
            result = await collector._collect_tiktok(["123"])

        assert len(result) == 1
        assert result[0]["views"] == 10
        assert "likes" not in result[0]
        assert "shares" not in result[0]

    @pytest.mark.asyncio
    async def test_collect_tiktok_rate_pacing(self, tmp_path):
        """Calls within the 6 req/min window are spaced via the injectable
        sleep; stale windows do not sleep."""
        (tmp_path / "config.yaml").write_text(
            "accounts:\n  tiktok:\n    access_token: test-token\n"
        )
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._tt_pace = 10.0
        sleep_mock = AsyncMock()
        collector._sleep = sleep_mock

        # Fresh request window → pace sleeps before the next call.
        collector._tt_last_request = time.monotonic()
        await collector._pace_tiktok()
        sleep_mock.assert_awaited_once()

        # Old request (≥ window) → no sleep.
        collector._tt_last_request = time.monotonic() - 60
        collector._sleep = AsyncMock()
        await collector._pace_tiktok()
        collector._sleep.assert_not_awaited()


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


# ── Ownership filtering (analytics hardening) ──────────────────────────────

class TestOwnershipFiltering:
    """Foreign post ids must never persist or surface in analytics.

    Root cause of the skewed views/likes/comments dashboard aggregates was
    metric_snapshots rows for video ids that were NOT on the authenticated
    YouTube channel (stale test posts / foreign videos). These tests pin
    the ownership gate at BOTH the producer (``_collect_youtube`` /
    ``_collect_x`` / ``_collect_instagram``) and the persistence boundary
    (``collect_all`` → ``_filter_owned_snapshots``).

    YouTube authority: the authenticated channel's uploads playlist
    (``channels.list mine=True`` → ``relatedPlaylists.uploads`` →
    ``playlistItems``). X/Instagram authority: state.json, which xPST
    itself writes at post time.
    """

    @pytest.mark.asyncio
    async def test_youtube_foreign_ids_in_batch_are_not_persisted(self, tmp_path):
        """The regression (analytics hardening): a snapshot batch containing
        foreign ids against a fake channel uploads set must persist ONLY the
        owned ids — foreign ids must NOT enter metric_snapshots."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        # Fake channel uploads set = the verified ownership cache (what
        # _get_owned_youtube_ids loads from the channel's uploads playlist).
        collector._owned_yt_ids = {"owned1", "owned2"}
        collector._owned_yt_ids_ts = time.time()

        def row(pid: str, views: int) -> dict:
            return {
                "platform": "youtube", "post_id": pid, "views": views,
                "likes": 1, "comments": 0, "shares": 0,
                "timestamp": "2026-08-27T00:00:00+00:00",
            }

        # A snapshot batch containing foreign ids, as a collector would
        # hand it to the persistence layer:
        owned1 = row("owned1", 10)
        foreign = row("foreign_video_id", 9001)  # NOT on the channel
        owned2 = row("owned2", 20)

        with patch.object(
            collector, "_collect_youtube", return_value=[owned1, foreign, owned2]
        ):
            data = await collector.collect_all({
                "youtube": ["owned1", "foreign_video_id", "owned2"],
            })

        assert set(data["youtube"]) == {"owned1", "foreign_video_id", "owned2"}
        # The persisted set (what the dashboard aggregates) is clean:
        persisted = collector.store.latest("youtube")
        persisted_ids = {str(p["post_id"]) for p in persisted}
        assert persisted_ids == {"owned1", "owned2"}
        assert "foreign_video_id" not in persisted_ids
        # Foreign id is warned exactly once, never spammed.
        assert "youtube:foreign_video_id" in collector._warned_foreign

    @pytest.mark.asyncio
    async def test_youtube_producer_skips_foreign_ids(self, tmp_path):
        """_collect_youtube must not return metrics for videos that are not
        on the authenticated channel's uploads playlist."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "youtube_token.json").write_text('{"token": "fake"}')
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._owned_yt_ids = {"vid1", "vid2"}
        collector._owned_yt_ids_ts = time.time()

        mock_response = {
            "items": [
                {"id": "vid1", "statistics": {"viewCount": "1", "likeCount": "1", "commentCount": "1"}},
                {"id": "foreign9", "statistics": {"viewCount": "999", "likeCount": "9", "commentCount": "9"}},
            ]
        }
        mock_service = MagicMock()
        mock_service.videos.return_value.list.return_value.execute.return_value = mock_response

        import google.oauth2.credentials
        import googleapiclient.discovery

        with patch.object(google.oauth2.credentials, "Credentials") as mock_creds_cls, \
             patch.object(googleapiclient.discovery, "build", return_value=mock_service):
            mock_creds_cls.from_authorized_user_file.return_value = MagicMock()
            result = await collector._collect_youtube(["vid1", "foreign9"])

        assert [r["post_id"] for r in result] == ["vid1"]
        assert "youtube:foreign9" in collector._warned_foreign

    @pytest.mark.asyncio
    async def test_youtube_fail_closed_when_ownership_unverifiable(self, tmp_path):
        """When the uploads playlist cannot be fetched, the collector fails
        closed: no unverified metrics are returned (and none persisted)."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "youtube_token.json").write_text('{"token": "fake"}')
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._owned_yt_ids = None
        collector._owned_yt_ids_ts = 0

        mock_service = MagicMock()
        mock_service.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "vid1", "statistics": {"viewCount": "1"}}]
        }

        import google.oauth2.credentials
        import googleapiclient.discovery

        with patch.object(google.oauth2.credentials, "Credentials") as mock_creds_cls, \
             patch.object(googleapiclient.discovery, "build", return_value=mock_service), \
             patch.object(collector, "_get_owned_youtube_ids", return_value=None):
            mock_creds_cls.from_authorized_user_file.return_value = MagicMock()
            result = await collector._collect_youtube(["vid1"])

        assert result == []
        assert collector._warned_youtube_unverified is True

    def test_owned_ids_fetch_paginates_whole_playlist(self, tmp_path):
        """_get_owned_youtube_ids walks EVERY page of the uploads playlist —
        a truncated set would silently drop legitimate older uploads from
        analytics."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "youtube_token.json").write_text('{"token": "fake"}')
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        page1 = {
            "items": [{"contentDetails": {"videoId": f"vid{i}"}} for i in range(3)],
            "nextPageToken": "tok2",
        }
        page2 = {
            "items": [
                {"contentDetails": {"videoId": "vid_old_1"}},
                {"contentDetails": {"videoId": "vid_old_2"}},
            ]
        }
        pages = iter([page1, page2])
        mock_service = MagicMock()
        mock_service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]
        }
        mock_service.playlistItems.return_value.list.return_value.execute.side_effect = (
            lambda: next(pages)
        )

        import google.oauth2.credentials
        import googleapiclient.discovery

        with patch.object(google.oauth2.credentials, "Credentials") as mock_creds_cls, \
             patch.object(googleapiclient.discovery, "build", return_value=mock_service):
            mock_creds_cls.from_authorized_user_file.return_value = MagicMock()
            owned = collector._get_owned_youtube_ids()

        assert owned == {"vid0", "vid1", "vid2", "vid_old_1", "vid_old_2"}
        assert mock_service.playlistItems.return_value.list.call_count == 2

    @pytest.mark.asyncio
    async def test_x_skips_ids_not_recorded_in_state(self, tmp_path):
        """_collect_x only tracks tweet ids that exist in our own state.json
        (written by xPST at post time — source of truth for identity)."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "x_cookies.json").write_text('{"auth_token": "fake"}')
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {
                "v1": {"posted_to": {"x": {"id": "real_tweet"}}},
            }
        }))
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_tweet = MagicMock()
        mock_tweet.view_count = 10
        mock_tweet.favorite_count = 1
        mock_tweet.reply_count = 0
        mock_tweet.retweet_count = 0
        mock_client = MagicMock()
        mock_client.get_tweet_by_id = AsyncMock(return_value=mock_tweet)

        import twikit

        with patch.object(twikit, "Client", return_value=mock_client):
            result = await collector._collect_x(["real_tweet", "foreign_tweet"])

        assert [r["post_id"] for r in result] == ["real_tweet"]
        assert mock_client.get_tweet_by_id.await_count == 1
        assert "x:foreign_tweet" in collector._warned_foreign

    @pytest.mark.asyncio
    async def test_instagram_skips_ids_not_recorded_in_state(self, tmp_path):
        """_collect_instagram only tracks media ids recorded in state.json."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        (creds_dir / "instagram_session.json").write_text(json.dumps({
            "authorization_data": {"sessionid": "fake"}
        }))
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {"v1": {"posted_to": {"instagram": {"id": "real_media"}}}}
        }))
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        mock_info = MagicMock()
        mock_info.like_count = 1
        mock_info.comment_count = 0
        mock_info.play_count = 5

        import instagrapi

        mock_client = MagicMock(spec=instagrapi.Client)
        mock_client.media_info.return_value = mock_info
        mock_client.insights_media.return_value = {}

        with patch.object(instagrapi, "Client", return_value=mock_client):
            result = await collector._collect_instagram(["real_media", "foreign_media"])

        assert [r["post_id"] for r in result] == ["real_media"]
        assert "instagram:foreign_media" in collector._warned_foreign

    @pytest.mark.asyncio
    async def test_x_foreign_rows_not_persisted(self, tmp_path):
        """Persistence gate mirrors the producer: an x row whose id is not
        recorded in state.json is not written, even if a producer passed it
        through."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {"v1": {"posted_to": {"x": {"id": "state_tweet"}}}},
        }))

        def row(pid: str, views: int) -> dict:
            return {
                "platform": "x", "post_id": pid, "views": views,
                "likes": 0, "comments": 0, "shares": 0,
                "timestamp": "2026-08-27T00:00:00+00:00",
            }

        with patch.object(
            collector, "_collect_x", return_value=[row("state_tweet", 5), row("ghost", 99)]
        ):
            await collector.collect_all({"x": ["state_tweet", "ghost"]})

        persisted = collector.store.latest("x")
        assert {str(p["post_id"]) for p in persisted} == {"state_tweet"}
        assert "x:ghost" in collector._warned_foreign

    @pytest.mark.asyncio
    async def test_x_foreign_rows_tolerated_when_state_empty(self, tmp_path):
        """No state evidence → no judgement: explicit ids keep working on a
        fresh install (nothing posted yet ≠ everything is foreign)."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        def row(pid: str) -> dict:
            return {
                "platform": "x", "post_id": pid, "views": 1,
                "likes": 0, "comments": 0, "shares": 0,
                "timestamp": "2026-08-27T00:00:00+00:00",
            }

        with patch.object(collector, "_collect_x", return_value=[row("any_id")]):
            await collector.collect_all({"x": ["any_id"]})

        persisted = collector.store.latest("x")
        assert {str(p["post_id"]) for p in persisted} == {"any_id"}

    @pytest.mark.asyncio
    async def test_stale_foreign_youtube_rows_are_purged(self, tmp_path):
        """Self-healing: pre-existing foreign youtube rows (the exact
        original-skew shape — rows persisted before ownership verification
        existed) are REMOVED on the next verified collection, not merely
        prevented from growing. Non-youtube rows are never touched."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._owned_yt_ids = {"owned1"}
        collector._owned_yt_ids_ts = time.time()
        # Pre-existing rows, as if a prior run (or test post) had written
        # them before the ownership gate existed.
        collector.store.record_snapshots([
            {"platform": "youtube", "post_id": "owned1", "views": 10, "likes": 1,
             "comments": 0, "shares": 0, "timestamp": "2026-08-26T10:00:00+00:00"},
            {"platform": "youtube", "post_id": "stale_test_post", "views": 9000,
             "likes": 99, "comments": 9, "shares": 0, "timestamp": "2026-08-26T10:00:00+00:00"},
            {"platform": "x", "post_id": "keep_x", "views": 1,
             "timestamp": "2026-08-26T10:00:00+00:00"},
        ])

        with patch.object(
            collector,
            "_collect_youtube",
            return_value=[{
                "platform": "youtube", "post_id": "owned1", "views": 11,
                "likes": 1, "comments": 0, "shares": 0,
                "timestamp": "2026-08-27T00:00:00+00:00",
            }],
        ):
            await collector.collect_all({"youtube": ["owned1"]})

        persisted_yt = {str(p["post_id"]) for p in collector.store.latest("youtube")}
        assert persisted_yt == {"owned1"}
        assert collector.store.history("youtube", "stale_test_post") == []
        # The youtube-only purge never touches other platforms.
        assert {str(p["post_id"]) for p in collector.store.latest("x")} == {"keep_x"}

    @pytest.mark.asyncio
    async def test_no_purge_when_ownership_unverifiable(self, tmp_path):
        """The purge must fail CLOSED: when ownership cannot be verified
        (None), existing rows are left untouched — a transient ownership
        check failure must never wipe real history."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector.store.record_snapshots([
            {"platform": "youtube", "post_id": "mystery1", "views": 5,
             "timestamp": "2026-08-26T10:00:00+00:00"},
        ])

        with patch.object(collector, "_get_owned_youtube_ids", return_value=None), \
             patch.object(collector, "_collect_youtube", return_value=[]):
            await collector.collect_all({"youtube": ["mystery1"]})

        persisted = {str(p["post_id"]) for p in collector.store.latest("youtube")}
        assert persisted == {"mystery1"}


class TestTikTokThreadsOwnership:
    """tiktok/threads join the state.json identity gate (audit 2026-08-28):
    TikTok's official API only returns own videos, but the persistence
    invariant is enforced anyway so a bypassed/mocked collector cannot
    smuggle foreign ids into metric_snapshots."""

    @staticmethod
    def _row(pid: str, platform: str = "tiktok", views: int = 1) -> dict:
        return {
            "platform": platform, "post_id": pid, "views": views,
            "likes": 0, "comments": 0, "shares": 0,
            "timestamp": "2026-08-27T00:00:00+00:00",
        }

    @pytest.mark.asyncio
    async def test_tiktok_foreign_row_dropped_when_state_known(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {"v1": {"posted_to": {"tiktok": {"id": "own_tt"}}}},
        }))
        with patch.object(
            collector,
            "_collect_tiktok",
            return_value=[self._row("own_tt", views=5), self._row("foreign_tt", views=999)],
        ):
            await collector.collect_all({"tiktok": ["own_tt", "foreign_tt"]})

        persisted = {str(p["post_id"]) for p in collector.store.latest("tiktok")}
        assert persisted == {"own_tt"}
        assert "tiktok:foreign_tt" in collector._warned_foreign

    @pytest.mark.asyncio
    async def test_tiktok_tolerated_when_state_empty(self, tmp_path):
        """Fresh install with no tiktok post history: no judgement."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        with patch.object(collector, "_collect_tiktok", return_value=[self._row("any")]):
            await collector.collect_all({"tiktok": ["any"]})
        assert {str(p["post_id"]) for p in collector.store.latest("tiktok")} == {"any"}

    @pytest.mark.asyncio
    async def test_threads_foreign_row_dropped_when_state_known(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {"v1": {"posted_to": {"threads": {"id": "own_th"}}}},
        }))
        with patch.object(
            collector,
            "_collect_threads",
            return_value=[self._row("own_th", platform="threads"), self._row("ghost", platform="threads")],
        ):
            await collector.collect_all({"threads": ["own_th", "ghost"]})

        persisted = {str(p["post_id"]) for p in collector.store.latest("threads")}
        assert persisted == {"own_th"}

    @pytest.mark.asyncio
    async def test_tiktok_purge_parity_with_youtube(self, tmp_path):
        """Purge parity: a pre-existing foreign tiktok row (persisted before
        the gate existed) is removed on the next verified collection, while
        owned rows survive."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {"v1": {"posted_to": {"tiktok": {"id": "own_tt"}}}},
        }))
        collector.store.record_snapshots([
            self._row("own_tt", views=5),
            self._row("stale_test_video", views=9000),
            self._row("keep_x", platform="x", views=1),
        ])
        with patch.object(collector, "_collect_tiktok", return_value=[self._row("own_tt", views=6)]):
            await collector.collect_all({"tiktok": ["own_tt"]})

        assert {str(p["post_id"]) for p in collector.store.latest("tiktok")} == {"own_tt"}
        assert collector.store.history("tiktok", "stale_test_video") == []
        # Other platforms are untouched by the tiktok purge.
        assert {str(p["post_id"]) for p in collector.store.latest("x")} == {"keep_x"}

    @pytest.mark.asyncio
    async def test_x_purge_parity(self, tmp_path):
        """Purge parity for x: stale foreign rows predate the gate are purged."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        (tmp_path / "state.json").write_text(json.dumps({
            "posted_videos": {"v1": {"posted_to": {"x": {"id": "own_x"}}}},
        }))
        collector.store.record_snapshots([
            self._row("own_x", platform="x", views=5),
            self._row("old_foreign_tweet", platform="x", views=999),
        ])
        with patch.object(collector, "_collect_x", return_value=[self._row("own_x", platform="x", views=6)]):
            await collector.collect_all({"x": ["own_x"]})

        assert {str(p["post_id"]) for p in collector.store.latest("x")} == {"own_x"}
        assert collector.store.history("x", "old_foreign_tweet") == []

    @pytest.mark.asyncio
    async def test_purge_skipped_when_state_has_no_ids_for_platform(self, tmp_path):
        """Fail-safe purge: a fresh install (no state evidence for a platform)
        never wipes that platform's snapshot history."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector.store.record_snapshots([self._row("legacy_row", platform="instagram", views=2)])
        with patch.object(collector, "_collect_instagram", return_value=[]):
            await collector.collect_all({"instagram": []})

        assert {str(p["post_id"]) for p in collector.store.latest("instagram")} == {"legacy_row"}
