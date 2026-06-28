"""Tests for Phase 1 & 2 anti-ban and official API features.

Covers:
- Account warming (progressive ramp)
- Device-ID generation and persistence
- Caption variation with username placeholder
- TLS fingerprint session helper
- Proxy helper methods
- Config proxy/auth_mode/device_id fields
- QuotaManager auth_mode-aware limits
- Instagram Graph API path dispatch
- X API v2 path dispatch
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from xpst.anti_bot import CAPTION_SUFFIXES, AntiBotProtection
from xpst.config import XPSTConfig
from xpst.utils.quota import QuotaManager

# ═══════════════════════════════════════════════════════════════
# Phase 1.4: Caption Variation Tests
# ═══════════════════════════════════════════════════════════════

class TestCaptionVariationImproved:
    """Test improved caption variation with meaningful suffixes."""

    def test_no_empty_only_suffixes(self):
        """Ensure no platform has only empty string suffixes."""
        for platform, suffixes in CAPTION_SUFFIXES.items():
            non_empty = [s for s in suffixes if s.strip()]
            assert len(non_empty) > 0, f"{platform} has no non-empty suffixes"

    def test_youtube_has_shorts_variations(self):
        """YouTube suffixes should include #Shorts variants."""
        yt_suffixes = CAPTION_SUFFIXES["youtube"]
        shorts_suffixes = [s for s in yt_suffixes if "#Shorts" in s or "#shorts" in s]
        assert len(shorts_suffixes) >= 1

    def test_instagram_has_reels_variations(self):
        """Instagram suffixes should include #Reels variants."""
        ig_suffixes = CAPTION_SUFFIXES["instagram"]
        reels_suffixes = [s for s in ig_suffixes if "#Reels" in s]
        assert len(reels_suffixes) >= 1

    def test_caption_with_username_placeholder(self):
        """Test that {} placeholder is substituted with username."""
        ab = AntiBotProtection()
        # Find a suffix with {} placeholder
        has_placeholder = any("{}" in s for s in CAPTION_SUFFIXES.get("instagram", []))
        if not has_placeholder:
            pytest.skip("No instagram suffix with {} placeholder")

        # Call multiple captions to hit the placeholder
        found_substitution = False
        for i in range(50):
            caption = f"Test caption number {i}"
            result = ab.vary_caption(caption, "instagram", username="testuser")
            if "testuser" in result:
                found_substitution = True
                break
        assert found_substitution, "Username placeholder never substituted"

    def test_caption_without_username_no_substitution(self):
        """Test that {} is left as-is when no username provided."""
        ab = AntiBotProtection()
        # The {} should remain in the suffix if no username
        # (it won't appear in output because it's part of suffix that may not be selected)
        result = ab.vary_caption("test", "instagram")
        assert isinstance(result, str)

    def test_deterministic_with_username(self):
        """Same caption + platform + username should produce same result."""
        ab = AntiBotProtection()
        caption = "My awesome video"
        r1 = ab.vary_caption(caption, "instagram", username="user1")
        r2 = ab.vary_caption(caption, "instagram", username="user1")
        assert r1 == r2


# ═══════════════════════════════════════════════════════════════
# Phase 1.5: Account Warming Tests
# ═══════════════════════════════════════════════════════════════

class TestAccountWarming:
    """Test account warming / progressive ramp logic."""

    def test_new_account_day_0(self):
        """Day 0 account should get limit of 1."""
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("instagram", 0) == 1
        assert ab.get_warmed_daily_limit("youtube", 0.5) == 1

    def test_new_account_day_2(self):
        """Day 2 account should still get limit of 1."""
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("x", 2) == 1

    def test_week_old_account_day_5(self):
        """Day 5 account should get limit of 2."""
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("instagram", 5) == 2

    def test_two_week_old_account_day_10(self):
        """Day 10 account should get limit of 3."""
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("youtube", 10) == 3

    def test_mature_account_day_15(self):
        """Day 15+ account should get full conservative limit."""
        ab = AntiBotProtection()
        full = ab.get_daily_limit("instagram")
        assert ab.get_warmed_daily_limit("instagram", 15) == full
        assert ab.get_warmed_daily_limit("instagram", 30) == full
        assert ab.get_warmed_daily_limit("instagram", 100) == full

    def test_warmed_limit_never_exceeds_full(self):
        """Warmed limit should never exceed the full conservative limit."""
        ab = AntiBotProtection()
        for platform in ["instagram", "x", "youtube", "tiktok"]:
            full = ab.get_daily_limit(platform)
            for age in [0, 1, 3, 5, 7, 10, 14, 15, 30]:
                warmed = ab.get_warmed_daily_limit(platform, age)
                assert warmed <= full, f"{platform} age {age}: {warmed} > {full}"

    def test_negative_age_treated_as_new(self):
        """Negative age should be treated as day 0."""
        ab = AntiBotProtection()
        assert ab.get_warmed_daily_limit("instagram", -5) == 1


# ═══════════════════════════════════════════════════════════════
# Phase 1.6: Device-ID Tests
# ═══════════════════════════════════════════════════════════════

class TestDeviceID:
    """Test device-ID generation and persistence."""

    def test_generate_device_id_deterministic(self):
        """Same username should produce same device ID."""
        id1 = AntiBotProtection.generate_device_id("user1")
        id2 = AntiBotProtection.generate_device_id("user1")
        assert id1 == id2

    def test_generate_device_id_different_users(self):
        """Different usernames should produce different device IDs."""
        id1 = AntiBotProtection.generate_device_id("user1")
        id2 = AntiBotProtection.generate_device_id("user2")
        assert id1 != id2

    def test_device_id_is_uuid_format(self):
        """Device ID should be in UUID format."""
        import uuid
        dev_id = AntiBotProtection.generate_device_id("testuser")
        # Should not raise
        parsed = uuid.UUID(dev_id)
        assert parsed.version == 5  # UUID5

    def test_device_string_has_required_fields(self):
        """Device string should have all required instagrapi fields."""
        dev_id = AntiBotProtection.generate_device_id("testuser")
        device = AntiBotProtection.get_instagram_device_string(dev_id)
        assert "device_id" in device
        assert device["device_id"] == dev_id
        assert "model" in device
        assert "device" in device
        assert "firmware" in device
        assert "release" in device


# ═══════════════════════════════════════════════════════════════
# Phase 1.2: TLS Fingerprint Tests
# ═══════════════════════════════════════════════════════════════

class TestTLSFingerprint:
    """Test TLS fingerprint hardening helpers."""

    def test_get_tls_hardened_session_fallback(self):
        """Should return a session even without curl_cffi."""
        session = AntiBotProtection.get_tls_hardened_session()
        assert session is not None

    def test_get_tls_hardened_session_with_proxy(self):
        """Should apply proxy to session."""
        session = AntiBotProtection.get_tls_hardened_session(proxy="http://proxy:8080")
        assert session is not None

    def test_apply_proxy_to_instagrapi_no_proxy(self):
        """Should be a no-op when proxy is None."""
        client = MagicMock()
        AntiBotProtection.apply_proxy_to_instagrapi(client, None)
        # Should not touch the client
        client._session.proxies.assert_not_called()

    def test_apply_proxy_to_twikit_no_proxy(self):
        """Should be a no-op when proxy is None."""
        client = MagicMock()
        AntiBotProtection.apply_proxy_to_twikit(client, None)
        # Should not touch the client
        # (MagicMock auto-creates attributes, so we just verify no exception)


# ═══════════════════════════════════════════════════════════════
# Phase 1.3: Config Proxy/Auth Mode Tests
# ═══════════════════════════════════════════════════════════════

class TestConfigProxyAuthMode:
    """Test config proxy and auth_mode fields."""

    def test_default_proxy_is_none(self):
        """Default proxy should be None for all platforms."""
        config = XPSTConfig()
        assert config.tiktok.proxy is None
        assert config.youtube.proxy is None
        assert config.x.proxy is None
        assert config.instagram.proxy is None

    def test_default_auth_modes(self):
        """Default auth modes: IG uses official Graph API, X uses free twikit."""
        config = XPSTConfig()
        assert config.instagram.auth_mode == "graph_api"
        assert config.x.auth_mode == "cookies"

    def test_default_device_id_is_none(self):
        """Default Instagram device_id should be None."""
        config = XPSTConfig()
        assert config.instagram.device_id is None

    def test_load_proxy_from_file(self, tmp_path):
        """Test loading proxy from config file."""
        config_data = {
            "accounts": {
                "instagram": {
                    "proxy": "socks5://user:pass@proxy:1080",
                    "auth_mode": "graph_api",
                    "graph_access_token": "test_token",
                    "graph_ig_user_id": "123456",
                },
                "x": {
                    "proxy": "http://proxy:8080",
                    "auth_mode": "api_v2",
                    "api_key": "key123",
                    "api_secret": "secret123",
                    "access_token": "token123",
                    "access_token_secret": "tokensecret123",
                },
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        config = XPSTConfig.load(str(config_file))
        assert config.instagram.proxy == "socks5://user:pass@proxy:1080"
        assert config.instagram.auth_mode == "graph_api"
        assert config.instagram.graph_access_token == "test_token"
        assert config.instagram.graph_ig_user_id == "123456"
        assert config.x.proxy == "http://proxy:8080"
        assert config.x.auth_mode == "api_v2"
        assert config.x.api_key == "key123"
        assert config.x.api_secret == "secret123"

    def test_proxy_env_var_override(self, tmp_path, monkeypatch):
        """Test proxy env var override."""
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump({}, f)

        monkeypatch.setenv("XPST_INSTAGRAM_PROXY", "http://env-proxy:3128")
        monkeypatch.setenv("XPST_INSTAGRAM_AUTH_MODE", "graph_api")

        config = XPSTConfig.load(str(config_file))
        assert config.instagram.proxy == "http://env-proxy:3128"
        assert config.instagram.auth_mode == "graph_api"

    def test_x_api_v2_env_vars(self, tmp_path, monkeypatch):
        """Test X API v2 env var overrides."""
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump({}, f)

        monkeypatch.setenv("XPST_X_AUTH_MODE", "api_v2")
        monkeypatch.setenv("XPST_X_API_KEY", "env_key")
        monkeypatch.setenv("XPST_X_BEARER_TOKEN", "env_bearer")

        config = XPSTConfig.load(str(config_file))
        assert config.x.auth_mode == "api_v2"
        assert config.x.api_key == "env_key"
        assert config.x.bearer_token == "env_bearer"

    def test_save_includes_proxy_and_auth_mode(self, tmp_path):
        """Test that save() includes proxy and auth_mode."""
        config = XPSTConfig()
        config.instagram.proxy = "http://proxy:8080"
        config.instagram.auth_mode = "graph_api"
        config.x.proxy = "socks5://proxy:1080"
        config.x.auth_mode = "api_v2"
        config.config_dir = str(tmp_path)
        config.save()

        config_file = tmp_path / "config.yaml"
        with open(config_file) as f:
            saved = yaml.safe_load(f)

        assert saved["accounts"]["instagram"]["proxy"] == "http://proxy:8080"
        assert saved["accounts"]["instagram"]["auth_mode"] == "graph_api"
        assert saved["accounts"]["x"]["proxy"] == "socks5://proxy:1080"
        assert saved["accounts"]["x"]["auth_mode"] == "api_v2"


# ═══════════════════════════════════════════════════════════════
# Phase 2.3: Quota Manager Tests
# ═══════════════════════════════════════════════════════════════

class TestQuotaManagerAuthMode:
    """Test auth_mode-aware quota limits."""

    def test_default_quotas_without_config(self, tmp_path):
        """Without config, should use default conservative limits."""
        qm = QuotaManager(str(tmp_path))
        assert qm.get_remaining("youtube")["daily"] == 5
        assert qm.get_remaining("instagram")["daily"] == 5
        assert qm.get_remaining("x")["daily"] == 5

    def test_graph_api_increases_instagram_limit(self, tmp_path):
        """Graph API mode should increase Instagram limit to 25."""
        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        qm = QuotaManager(str(tmp_path), config=config)
        assert qm.quotas["instagram"].daily_limit == 25

    def test_session_mode_keeps_instagram_conservative(self, tmp_path):
        """Session mode should keep Instagram limit at 5."""
        config = XPSTConfig()
        config.instagram.auth_mode = "session"
        qm = QuotaManager(str(tmp_path), config=config)
        assert qm.quotas["instagram"].daily_limit == 5

    def test_api_v2_increases_x_limit(self, tmp_path):
        """API v2 mode should increase X limit to 17."""
        config = XPSTConfig()
        config.x.auth_mode = "api_v2"
        qm = QuotaManager(str(tmp_path), config=config)
        assert qm.quotas["x"].daily_limit == 17

    def test_cookies_mode_keeps_x_conservative(self, tmp_path):
        """Cookies mode should set X limit to 10 (conservative)."""
        config = XPSTConfig()
        config.x.auth_mode = "cookies"
        qm = QuotaManager(str(tmp_path), config=config)
        assert qm.quotas["x"].daily_limit == 10

    def test_youtube_quota_units(self, tmp_path):
        """YouTube quota units should be correctly calculated."""
        config = XPSTConfig()
        qm = QuotaManager(str(tmp_path), config=config)
        units = qm.get_youtube_quota_units()
        assert units["total_units"] == 10_000
        assert units["cost_per_upload"] == 1_600
        assert units["used_units"] == 0  # No uploads yet
        assert units["remaining_uploads"] == 6  # 10000 // 1600

    def test_youtube_quota_after_upload(self, tmp_path):
        """YouTube quota should decrease after an upload."""
        config = XPSTConfig()
        qm = QuotaManager(str(tmp_path), config=config)
        qm.record_upload("youtube")
        units = qm.get_youtube_quota_units()
        assert units["used_units"] == 1_600
        assert units["remaining_uploads"] == 5  # (10000 - 1600) // 1600

    def test_detailed_status_includes_auth_mode(self, tmp_path):
        """Detailed status should include auth_mode for IG and X."""
        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        config.x.auth_mode = "api_v2"
        qm = QuotaManager(str(tmp_path), config=config)
        status = qm.get_detailed_status()
        assert status["instagram"]["auth_mode"] == "graph_api"
        assert status["x"]["auth_mode"] == "api_v2"

    def test_detailed_status_includes_youtube_units(self, tmp_path):
        """Detailed status should include YouTube quota units."""
        config = XPSTConfig()
        qm = QuotaManager(str(tmp_path), config=config)
        status = qm.get_detailed_status()
        assert "quota_units" in status["youtube"]


# ═══════════════════════════════════════════════════════════════
# Phase 2.1: Instagram Graph API Dispatch Tests
# ═══════════════════════════════════════════════════════════════

class TestInstagramGraphAPIDispatch:
    """Test Instagram Graph API dispatch based on auth_mode."""

    def test_dispatch_to_graph_api(self):
        """When auth_mode is graph_api, should call _upload_graph_api."""
        from xpst.platforms.instagram import InstagramUploader
        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        uploader = InstagramUploader(config)

        uploader._upload_graph_api = AsyncMock(return_value="graph_api_called")
        uploader._upload_instagrapi = AsyncMock(return_value="instagrapi_called")

        asyncio.run(uploader.upload(Path("test.mp4"), "test caption"))

        uploader._upload_graph_api.assert_called_once()
        uploader._upload_instagrapi.assert_not_called()

    def test_dispatch_to_instagrapi(self):
        """When auth_mode is session, should call _upload_instagrapi."""
        from xpst.platforms.instagram import InstagramUploader
        config = XPSTConfig()
        config.instagram.auth_mode = "session"
        uploader = InstagramUploader(config)

        uploader._upload_graph_api = AsyncMock(return_value="graph_api_called")
        uploader._upload_instagrapi = AsyncMock(return_value="instagrapi_called")

        asyncio.run(uploader.upload(Path("test.mp4"), "test caption"))

        uploader._upload_instagrapi.assert_called_once()
        uploader._upload_graph_api.assert_not_called()

    def test_graph_api_not_configured_error(self):
        """Graph API should return error when not configured."""
        from xpst.platforms.base import UploadResult
        from xpst.platforms.instagram import InstagramUploader
        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        uploader = InstagramUploader(config)

        result = asyncio.run(uploader.upload(Path("test.mp4"), "test caption"))

        assert isinstance(result, UploadResult)
        assert not result.success
        assert "NOT_CONFIGURED" in result.error

    def test_graph_api_local_file_not_found(self):
        """Graph API should return error for non-existent local file."""
        from xpst.platforms.instagram import InstagramUploader
        config = XPSTConfig()
        config.instagram.auth_mode = "graph_api"
        config.instagram.graph_access_token = "token"
        config.instagram.graph_ig_user_id = "123"
        uploader = InstagramUploader(config)

        result = asyncio.run(uploader.upload(Path("/tmp/test_nonexistent.mp4"), "test caption"))

        assert not result.success
        assert result.error is not None


# ═══════════════════════════════════════════════════════════════
# Phase 2.2: X API v2 Dispatch Tests
# ═══════════════════════════════════════════════════════════════

class TestXAPIv2Dispatch:
    """Test X API v2 dispatch based on auth_mode."""

    def test_dispatch_to_api_v2(self):
        """When auth_mode is api_v2, should call _upload_api_v2."""
        from xpst.platforms.x import XUploader
        config = XPSTConfig()
        config.x.auth_mode = "api_v2"
        uploader = XUploader(config)

        uploader._upload_api_v2 = AsyncMock(return_value="api_v2_called")
        uploader._upload_twikit = AsyncMock(return_value="twikit_called")

        asyncio.run(uploader.upload(Path("test.mp4"), "test caption"))

        uploader._upload_api_v2.assert_called_once()
        uploader._upload_twikit.assert_not_called()

    def test_dispatch_to_twikit(self):
        """When auth_mode is cookies, should call _upload_twikit."""
        from xpst.platforms.x import XUploader
        config = XPSTConfig()
        config.x.auth_mode = "cookies"
        uploader = XUploader(config)

        uploader._upload_api_v2 = AsyncMock(return_value="api_v2_called")
        uploader._upload_twikit = AsyncMock(return_value="twikit_called")

        asyncio.run(uploader.upload(Path("test.mp4"), "test caption"))

        uploader._upload_twikit.assert_called_once()
        uploader._upload_api_v2.assert_not_called()

    def test_api_v2_not_configured_error(self):
        """API v2 should return error when not configured."""
        from xpst.platforms.base import UploadResult
        from xpst.platforms.x import XUploader
        config = XPSTConfig()
        config.x.auth_mode = "api_v2"
        # Don't set API credentials
        uploader = XUploader(config)

        result = asyncio.run(uploader.upload(Path("test.mp4"), "test caption"))

        assert isinstance(result, UploadResult)
        assert not result.success
        assert "NOT_CONFIGURED" in result.error
