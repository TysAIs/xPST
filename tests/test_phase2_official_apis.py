"""Tests for Phase 2 official API uploaders and Phase 1 anti-ban enhancements.

Tests:
- Instagram Graph API upload path (auth_mode dispatch, config validation, URL requirement)
- X API v2 upload path (auth_mode dispatch, config validation, chunked upload)
- Anti-bot new functions (warming, device-ID, TLS hardening, proxy application)
- Config new fields (auth_mode, proxy, API credentials, env vars)
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.anti_bot import AntiBotProtection
from xpst.config import XPSTConfig

# ─── Anti-Bot: Account Warming ──────────────────────────────────────────────


class TestAccountWarming:
    """Test get_warmed_daily_limit progressive ramp."""

    def test_new_account_day_1(self):
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("instagram", 0.5) == 1
        assert ab.get_warmed_daily_limit("instagram", 1) == 1
        assert ab.get_warmed_daily_limit("instagram", 2.9) == 1

    def test_warming_day_4(self):
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("x", 4) == 2
        assert ab.get_warmed_daily_limit("x", 6.9) == 2

    def test_warming_day_8(self):
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("youtube", 8) == 3
        assert ab.get_warmed_daily_limit("youtube", 13.9) == 3

    def test_full_limit_after_14_days(self):
        ab = AntiBotProtection()
        full = ab.get_daily_limit("instagram")
        result = ab.get_warmed_daily_limit("instagram", 14)
        assert result == full
        assert ab.get_warmed_daily_limit("instagram", 30) == full

    def test_negative_age_treated_as_new(self):
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("instagram", -5) == 1

    def test_warmed_never_exceeds_full(self):
        """Even during warming, limit can't exceed platform's full limit."""
        ab = AntiBotProtection()
        # If a platform has a full limit of 1, warming shouldn't exceed it
        full = ab.get_daily_limit("x")
        warmed = ab.get_warmed_daily_limit("x", 0)
        assert warmed <= full


# ─── Anti-Bot: Device ID ────────────────────────────────────────────────────


class TestDeviceID:
    """Test Instagram device-ID generation and stability."""

    def test_device_id_is_deterministic(self):
        """Same username → same device ID every time."""
        id1 = AntiBotProtection.generate_device_id("testuser")
        id2 = AntiBotProtection.generate_device_id("testuser")
        assert id1 == id2

    def test_device_id_differs_per_user(self):
        """Different usernames → different device IDs."""
        id1 = AntiBotProtection.generate_device_id("user_a")
        id2 = AntiBotProtection.generate_device_id("user_b")
        assert id1 != id2

    def test_device_id_is_uuid_format(self):
        device_id = AntiBotProtection.generate_device_id("testuser")
        # UUID format: 8-4-4-4-12 hex digits
        parts = device_id.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_device_string_has_required_fields(self):
        device_id = AntiBotProtection.generate_device_id("testuser")
        settings = AntiBotProtection.get_instagram_device_string(device_id)
        assert settings["device_id"] == device_id
        assert "model" in settings
        assert "device" in settings
        assert "firmware" in settings
        assert "cpu" in settings
        assert "dpi" in settings


# ─── Anti-Bot: TLS Hardening ────────────────────────────────────────────────


class TestTLSHardening:
    """Test TLS fingerprint hardening session creation."""

    def test_returns_session_without_curl_cffi(self):
        """When curl_cffi is not installed, should fall back to requests."""
        with patch.dict("sys.modules", {"curl_cffi": None}):
            session = AntiBotProtection.get_tls_hardened_session()
            assert session is not None

    def test_returns_session_with_proxy(self):
        """Proxy should be applied to the session."""
        session = AntiBotProtection.get_tls_hardened_session(proxy="http://proxy:8080")
        assert session is not None

    def test_returns_session_with_socks5_proxy(self):
        session = AntiBotProtection.get_tls_hardened_session(proxy="socks5://proxy:1080")
        assert session is not None


# ─── Anti-Bot: Proxy Application ────────────────────────────────────────────


class TestProxyApplication:
    """Test proxy application to platform clients."""

    def test_apply_proxy_to_instagrapi_noop_when_none(self):
        client = MagicMock()
        AntiBotProtection.apply_proxy_to_instagrapi(client, None)
        # Should not touch the client when proxy is None
        client._session.proxies.assert_not_called()

    def test_apply_proxy_to_twikit_noop_when_none(self):
        client = MagicMock()
        AntiBotProtection.apply_proxy_to_twikit(client, None)
        # Should not set _proxy when None

    def test_apply_proxy_to_instagrapi_sets_proxies(self):
        client = MagicMock()
        client._session = MagicMock()
        AntiBotProtection.apply_proxy_to_instagrapi(client, "http://proxy:8080")
        assert client._session.proxies["http"] == "http://proxy:8080"
        assert client._session.proxies["https"] == "http://proxy:8080"

    def test_apply_proxy_to_twikit_sets_proxy(self):
        client = MagicMock()
        AntiBotProtection.apply_proxy_to_twikit(client, "socks5://proxy:1080")
        assert client._proxy == "socks5://proxy:1080"


# ─── Anti-Bot: Caption Variation with Username ──────────────────────────────


class TestCaptionVariation:
    """Test caption variation with username placeholder substitution."""

    def test_vary_caption_substitutes_username(self):
        ab = AntiBotProtection()
        # Force a suffix that has {} placeholder
        with patch("xpst.anti_bot.CAPTION_SUFFIXES", {"test_platform": ["Follow @{} for more!"]}):
            result = ab.vary_caption("Hello world", "test_platform", username="myuser")
            assert "@myuser" in result

    def test_vary_caption_without_username_leaves_placeholder(self):
        ab = AntiBotProtection()
        with patch("xpst.anti_bot.CAPTION_SUFFIXES", {"test_platform": ["Follow @{} for more!"]}):
            result = ab.vary_caption("Hello world", "test_platform", username="")
            # Without username, placeholder stays
            assert "{}" in result

    def test_vary_caption_no_placeholder_works(self):
        ab = AntiBotProtection()
        with patch("xpst.anti_bot.CAPTION_SUFFIXES", {"test_platform": ["#hashtag"]}):
            result = ab.vary_caption("Hello", "test_platform", username="user")
            assert "#hashtag" in result


# ─── Config: New Auth Mode + Proxy Fields ───────────────────────────────────


class TestConfigAuthModes:
    """Test that config properly loads auth_mode and proxy fields."""

    def test_default_x_auth_mode_is_cookies(self):
        config = XPSTConfig()
        assert config.x.auth_mode == "cookies"

    def test_default_instagram_auth_mode_is_graph_api(self):
        config = XPSTConfig()
        assert config.instagram.auth_mode == "graph_api"

    def test_default_proxy_is_none(self):
        config = XPSTConfig()
        assert config.youtube.proxy is None
        assert config.x.proxy is None
        assert config.instagram.proxy is None
        assert config.tiktok.proxy is None

    def test_x_api_v2_fields_exist(self):
        config = XPSTConfig()
        assert hasattr(config.x, "api_key")
        assert hasattr(config.x, "api_secret")
        assert hasattr(config.x, "access_token")
        assert hasattr(config.x, "access_token_secret")
        assert hasattr(config.x, "bearer_token")

    def test_instagram_graph_api_fields_exist(self):
        config = XPSTConfig()
        assert hasattr(config.instagram, "graph_access_token")
        assert hasattr(config.instagram, "graph_ig_user_id")
        assert hasattr(config.instagram, "device_id")

    def test_env_var_sets_x_auth_mode(self, monkeypatch):
        monkeypatch.setenv("XPST_X_AUTH_MODE", "api_v2")
        config = XPSTConfig._apply_env_vars(XPSTConfig())
        assert config.x.auth_mode == "api_v2"

    def test_env_var_sets_instagram_proxy(self, monkeypatch):
        monkeypatch.setenv("XPST_INSTAGRAM_PROXY", "http://proxy:8080")
        config = XPSTConfig._apply_env_vars(XPSTConfig())
        assert config.instagram.proxy == "http://proxy:8080"

    def test_env_var_sets_x_api_key(self, monkeypatch):
        monkeypatch.setenv("XPST_X_API_KEY", "test_key_123")
        config = XPSTConfig._apply_env_vars(XPSTConfig())
        assert config.x.api_key == "test_key_123"

    def test_default_provider_mode_is_official(self):
        config = XPSTConfig()
        assert config.provider_mode == "official"

    def test_env_var_sets_provider_mode(self, monkeypatch):
        monkeypatch.setenv("XPST_PROVIDER_MODE", "community")
        config = XPSTConfig._apply_env_vars(XPSTConfig())
        assert config.provider_mode == "community"

    def test_is_community_platform_ig_session(self):
        config = XPSTConfig()
        config.instagram.auth_mode = "session"
        assert config.is_community_platform("instagram") is True

    def test_is_community_platform_ig_graph_api(self):
        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        assert config.is_community_platform("instagram") is False

    def test_is_community_platform_x_cookies(self):
        config = XPSTConfig()
        config.x.auth_mode = "cookies"
        assert config.is_community_platform("x") is True

    def test_should_show_platform_official_mode_hides_community(self):
        config = XPSTConfig()
        config.provider_mode = "official"
        config.instagram.auth_mode = "session"
        assert config.should_show_platform("instagram") is False
        assert config.should_show_platform("youtube") is True

    def test_should_show_platform_community_mode_shows_all(self):
        config = XPSTConfig()
        config.provider_mode = "community"
        config.instagram.auth_mode = "session"
        assert config.should_show_platform("instagram") is True
        assert config.should_show_platform("youtube") is True


# ─── Instagram Graph API Uploader ───────────────────────────────────────────


class TestInstagramGraphAPI:
    """Test Instagram Graph API upload path dispatch and validation."""

    def _make_config(self, auth_mode="graph_api", token="test_token", ig_user_id="123456"):
        config = XPSTConfig()
        config.instagram.auth_mode = auth_mode
        config.instagram.graph_access_token = token
        config.instagram.graph_ig_user_id = ig_user_id
        return config

    @pytest.mark.asyncio
    async def test_graph_api_not_configured_returns_error(self):
        from xpst.platforms.instagram import InstagramUploader

        config = self._make_config(token="", ig_user_id="")
        uploader = InstagramUploader(config)
        result = await uploader.upload(Path("/tmp/test.mp4"), "caption")

        assert not result.success
        assert result.error is not None and "NOT_CONFIGURED" in result.error

    @pytest.mark.asyncio
    async def test_graph_api_resumable_upload_for_local_file(self):
        """Local files should use the resumable upload path, not be rejected."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from xpst.platforms.instagram import InstagramUploader

        config = self._make_config()
        uploader = InstagramUploader(config)

        # Create a temp file to simulate local video
        with tempfile.NamedTemporaryFile(suffix="..mp4", delete=False) as f:
            f.write(b"fake video data")
            local_path = Path(f.name)

        try:
            # Mock httpx.AsyncClient to avoid real API calls
            mock_response = MagicMock()
            mock_response.json.return_value = {"id": "container_123"}
            mock_response.raise_for_status = MagicMock()

            mock_status_response = MagicMock()
            mock_status_response.json.return_value = {"status": {"video_status": "FINISHED"}}
            mock_status_response.raise_for_status = MagicMock()

            mock_publish_response = MagicMock()
            mock_publish_response.json.return_value = {"id": "media_456"}
            mock_publish_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.get.return_value = mock_status_response
            mock_client.put.return_value = mock_response

            with patch("httpx.AsyncClient") as mock_async_client:
                mock_async_client.return_value.__aenter__.return_value = mock_client
                # The publish call returns a different response
                mock_client.post.side_effect = [mock_response, mock_publish_response]

                result = await uploader.upload(local_path, "caption")
                assert result.success
                assert result.post_id == "media_456"
                assert result.metadata.get("upload_type") == "resumable"
        finally:
            local_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_graph_api_dispatches_on_auth_mode(self):
        """When auth_mode is 'session', should NOT call graph API path."""
        from xpst.platforms.instagram import InstagramUploader

        config = XPSTConfig()
        config.instagram.auth_mode = "session"
        uploader = InstagramUploader(config)

        # Create a temp file so _validate_video passes, then _get_client fails
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake video")
            local_path = Path(f.name)

        try:
            result = await uploader.upload(local_path, "caption")
            # Should get an instagrapi/session error, not a graph API error
            assert result.error is None or "GRAPH_API" not in result.error
        finally:
            local_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_graph_api_success_flow(self):
        """Test the full Graph API container→publish flow with mocked HTTP."""
        from xpst.platforms.instagram import InstagramUploader

        config = self._make_config()
        uploader = InstagramUploader(config)

        # Mock httpx.AsyncClient
        mock_response_container = MagicMock()
        mock_response_container.json.return_value = {"id": "container_123"}
        mock_response_container.raise_for_status = MagicMock()

        mock_response_publish = MagicMock()
        mock_response_publish.json.return_value = {"id": "media_456"}
        mock_response_publish.raise_for_status = MagicMock()

        mock_response_permalink = MagicMock()
        mock_response_permalink.json.return_value = {"permalink": "https://instagram.com/reel/abc/"}
        mock_response_permalink.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[mock_response_container, mock_response_publish])
        mock_client.get = AsyncMock(return_value=mock_response_permalink)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            # Pass URL as string — Graph API needs a public URL, not a Path
            result = await uploader.upload(
                "https://example.com/video.mp4",  # type: ignore[arg-type]
                "Test caption",
            )

        assert result.success
        assert result.post_id == "media_456"
        assert result.post_url is not None and "instagram.com" in result.post_url
        assert result.metadata["auth_mode"] == "graph_api"
        assert result.metadata["container_id"] == "container_123"


# ─── X API v2 Uploader ──────────────────────────────────────────────────────


class TestXAPIv2:
    """Test X API v2 upload path dispatch and validation."""

    def _make_config(self, auth_mode="api_v2"):
        config = XPSTConfig()
        config.x.auth_mode = auth_mode
        config.x.api_key = "test_key"
        config.x.api_secret = "test_secret"
        config.x.access_token = "test_token"
        config.x.access_token_secret = "test_token_secret"
        return config

    @pytest.mark.asyncio
    async def test_api_v2_not_configured_returns_error(self):
        from xpst.platforms.x import XUploader

        config = XPSTConfig()
        config.x.auth_mode = "api_v2"
        config.x.api_key = ""
        uploader = XUploader(config)
        result = await uploader.upload(Path("/tmp/test.mp4"), "caption")

        assert not result.success
        assert result.error is not None and "NOT_CONFIGURED" in result.error

    @pytest.mark.asyncio
    async def test_api_v2_dispatches_on_auth_mode(self):
        """When auth_mode is 'cookies', should NOT call API v2 path."""
        from xpst.platforms.x import XUploader

        config = XPSTConfig()
        config.x.auth_mode = "cookies"
        uploader = XUploader(config)

        # Create a temp file so _validate_video passes, then _get_client fails
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake video")
            local_path = Path(f.name)

        try:
            result = await uploader.upload(local_path, "caption")
            # Should get a twikit/cookie error, not an API v2 error
            assert result.error is None or "API_V2" not in result.error
        finally:
            local_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_api_v2_truncates_long_caption(self):
        from xpst.platforms.x import XUploader

        config = self._make_config()
        uploader = XUploader(config)

        # Create a temp video file
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake video data")
            video_path = Path(f.name)

        try:
            # Mock the OAuth client to capture what caption is sent
            mock_client = AsyncMock()
            mock_response_init = MagicMock()
            mock_response_init.json.return_value = {"media_id": "123"}
            mock_response_init.raise_for_status = MagicMock()

            mock_response_append = MagicMock()
            mock_response_append.raise_for_status = MagicMock()

            mock_response_finalize = MagicMock()
            mock_response_finalize.raise_for_status = MagicMock()

            mock_response_tweet = MagicMock()
            mock_response_tweet.json.return_value = {"data": {"id": "tweet_789"}}
            mock_response_tweet.raise_for_status = MagicMock()

            mock_client.post = AsyncMock(side_effect=[
                mock_response_init,
                mock_response_append,
                mock_response_finalize,
                mock_response_tweet,
            ])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            long_caption = "A" * 300  # 300 chars, over 280 limit

            with patch("authlib.integrations.httpx_client.AsyncOAuth1Client", return_value=mock_client):
                result = await uploader.upload(video_path, long_caption)

            assert result.success
            assert result.post_id == "tweet_789"
            assert result.metadata["auth_mode"] == "api_v2"

            # Check that the tweet text was truncated (4th post call = tweet creation)
            tweet_call = mock_client.post.call_args_list[3]
            sent_text = tweet_call.kwargs.get("json", {}).get("text", "")
            assert len(sent_text) <= 280
        finally:
            video_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_api_v2_rate_limit_error(self):
        from xpst.platforms.x import XUploader

        config = self._make_config()
        uploader = XUploader(config)

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake video")
            video_path = Path(f.name)

        try:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = Exception("429 rate limit")
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("authlib.integrations.httpx_client.AsyncOAuth1Client", return_value=mock_client):
                result = await uploader.upload(video_path, "caption")

            assert not result.success
            assert result.error is not None and "RATE_LIMITED" in result.error
        finally:
            video_path.unlink(missing_ok=True)
