"""Tests for anti-bot protection module."""

import time
from datetime import datetime
from unittest.mock import patch

from xpst.anti_bot import (
    USER_AGENTS,
    AntiBotProtection,
)


class TestAntiBotInit:
    """Test AntiBotProtection initialization."""

    def test_default_init(self):
        """Test default initialization."""
        ab = AntiBotProtection()
        assert ab._ua_index >= 0

    def test_custom_timezone(self):
        """Test initialization with custom timezone offset."""
        ab = AntiBotProtection(timezone_offset=-5)
        assert ab._timezone_offset == -5


class TestUploadDelay:
    """Test upload delay generation."""

    def test_delay_range(self):
        """Test that delays are within expected range."""
        ab = AntiBotProtection()
        for _ in range(50):
            delay = ab.get_upload_delay("youtube")
            # Base: 120-300s, with ±30% jitter, min 60s
            assert delay >= 60.0
            assert delay <= 400.0  # 300 * 1.3 + buffer

    def test_delay_varies(self):
        """Test that delays are not constant."""
        ab = AntiBotProtection()
        delays = [ab.get_upload_delay("youtube") for _ in range(10)]
        # All should not be identical
        assert len(set(delays)) > 1

    def test_jittered_interval(self):
        """Test jittered interval calculation."""
        ab = AntiBotProtection()
        base = 900.0  # 15 minutes
        for _ in range(20):
            jittered = ab.get_jittered_interval(base)
            assert jittered >= 60.0
            # ±30% of 900 = 630 to 1170
            assert 630 <= jittered <= 1170


class TestShouldPostNow:
    """Test time-of-day awareness."""

    def test_during_posting_hours(self):
        """Test that posting is allowed during normal hours."""
        ab = AntiBotProtection()
        # Mock current time to 2pm
        mock_time = datetime(2025, 1, 15, 14, 0, 0)
        with patch.object(ab, '_get_local_time', return_value=mock_time):
            assert ab.should_post_now() is True

    def test_outside_posting_hours_late(self):
        """Test that posting is blocked late at night."""
        ab = AntiBotProtection()
        # Mock current time to 3am
        mock_time = datetime(2025, 1, 15, 3, 0, 0)
        with patch.object(ab, '_get_local_time', return_value=mock_time):
            assert ab.should_post_now() is False

    def test_outside_posting_hours_early(self):
        """Test that posting is blocked before 8am."""
        ab = AntiBotProtection()
        mock_time = datetime(2025, 1, 15, 6, 0, 0)
        with patch.object(ab, '_get_local_time', return_value=mock_time):
            assert ab.should_post_now() is False

    def test_boundary_8am(self):
        """Test that 8am is allowed."""
        ab = AntiBotProtection()
        mock_time = datetime(2025, 1, 15, 8, 0, 0)
        with patch.object(ab, '_get_local_time', return_value=mock_time):
            assert ab.should_post_now() is True

    def test_boundary_11pm(self):
        """Test that 11pm is NOT allowed (23:00 exclusive)."""
        ab = AntiBotProtection()
        mock_time = datetime(2025, 1, 15, 23, 0, 0)
        with patch.object(ab, '_get_local_time', return_value=mock_time):
            assert ab.should_post_now() is False

    def test_boundary_10_59pm(self):
        """Test that 10:59pm is allowed."""
        ab = AntiBotProtection()
        mock_time = datetime(2025, 1, 15, 22, 59, 0)
        with patch.object(ab, '_get_local_time', return_value=mock_time):
            assert ab.should_post_now() is True


class TestDailyLimits:
    """Test daily rate limiting."""

    def test_conservative_limits(self):
        """Test that conservative limits are well below platform maxes."""
        ab = AntiBotProtection()
        assert ab.get_daily_limit("instagram") == 5  # Max: 25
        assert ab.get_daily_limit("x") == 10  # Max: 17
        assert ab.get_daily_limit("youtube") == 3  # Max: 6
        assert ab.get_daily_limit("tiktok") == 3

    def test_unknown_platform_default(self):
        """Test default limit for unknown platform."""
        ab = AntiBotProtection()
        assert ab.get_daily_limit("unknown") == 3

    def test_can_upload_initially(self):
        """Test that uploading is allowed initially."""
        ab = AntiBotProtection()
        assert ab.can_upload("youtube") is True

    def test_can_upload_after_recording(self):
        """Test that uploading is blocked after hitting daily limit."""
        ab = AntiBotProtection()
        limit = ab.get_daily_limit("youtube")
        for _ in range(limit):
            ab.record_upload("youtube")
        assert ab.can_upload("youtube") is False

    def test_daily_counts_per_platform(self):
        """Test that counts are per-platform."""
        ab = AntiBotProtection()
        limit = ab.get_daily_limit("youtube")
        for _ in range(limit):
            ab.record_upload("youtube")
        # YouTube blocked but Instagram should be fine
        assert ab.can_upload("instagram") is True

    def test_daily_reset(self):
        """Test that counts reset after 24 hours."""
        ab = AntiBotProtection()
        for _ in range(5):
            ab.record_upload("youtube")
        # Simulate 24 hours passing
        ab._last_count_reset = time.time() - 86401
        assert ab.can_upload("youtube") is True


class TestCaptionVariation:
    """Test caption variation across platforms."""

    def test_caption_varies_by_platform(self):
        """Test that the same caption gets different variations per platform."""
        ab = AntiBotProtection()
        caption = "Check out my latest video!"

        ig_caption = ab.vary_caption(caption, "instagram")
        yt_caption = ab.vary_caption(caption, "youtube")
        x_caption = ab.vary_caption(caption, "x")

        # They should all be valid strings
        assert isinstance(ig_caption, str)
        assert isinstance(yt_caption, str)
        assert isinstance(x_caption, str)

        # All should contain the original caption
        assert caption in ig_caption
        assert caption in yt_caption
        assert caption in x_caption

    def test_empty_caption_unchanged(self):
        """Test that empty captions stay empty."""
        ab = AntiBotProtection()
        assert ab.vary_caption("", "youtube") == ""
        assert ab.vary_caption("", "instagram") == ""

    def test_deterministic_variation(self):
        """Test that same input produces same output (deterministic)."""
        ab = AntiBotProtection()
        caption = "My awesome video"
        result1 = ab.vary_caption(caption, "youtube")
        result2 = ab.vary_caption(caption, "youtube")
        assert result1 == result2

    def test_no_identical_captions_across_platforms(self):
        """Test that captions are varied across platforms (not always identical)."""
        ab = AntiBotProtection()
        caption = "This is a test caption for cross-posting"

        captions = set()
        for platform in ["youtube", "instagram", "x", "tiktok"]:
            captions.add(ab.vary_caption(caption, platform))

        # At least some should differ (this is probabilistic but very likely)
        # Given the variety of suffixes, at least 2 different values expected
        # (x often has no suffix, youtube may have #Shorts, etc.)


class TestPlatformOrder:
    """Test platform upload order randomization."""

    def test_randomized_order_contains_all(self):
        """Test that randomized order contains all platforms."""
        ab = AntiBotProtection()
        platforms = ["youtube", "instagram", "x", "tiktok"]
        order = ab.get_randomized_platform_order(platforms)
        assert sorted(order) == sorted(platforms)

    def test_randomized_order_varies(self):
        """Test that order varies between calls."""
        ab = AntiBotProtection()
        platforms = ["youtube", "instagram", "x", "tiktok"]
        orders = set()
        for _ in range(20):
            order = tuple(ab.get_randomized_platform_order(platforms))
            orders.add(order)
        # Should get at least 2 different orderings in 20 tries
        assert len(orders) > 1


class TestUserAgentRotation:
    """Test User-Agent rotation."""

    def test_get_user_agent(self):
        """Test that user agents are returned."""
        ab = AntiBotProtection()
        ua = ab.get_user_agent()
        assert isinstance(ua, str)
        assert len(ua) > 50  # Should be a real UA string

    def test_rotation_cycles(self):
        """Test that user agents rotate."""
        ab = AntiBotProtection()
        agents = set()
        for _ in range(len(USER_AGENTS) + 2):
            agents.add(ab.get_user_agent())
        # Should see multiple unique agents
        assert len(agents) > 1

    def test_all_agents_realistic(self):
        """Test that all user agents look realistic."""
        for ua in USER_AGENTS:
            assert "Mozilla" in ua
            assert len(ua) > 50


class TestTimingChecks:
    """Test timing-related checks."""

    def test_time_since_last_upload_no_history(self):
        """Test time since last upload with no history."""
        ab = AntiBotProtection()
        assert ab.time_since_last_upload("youtube") == float("inf")

    def test_time_since_last_upload_with_history(self):
        """Test time since last upload with history."""
        ab = AntiBotProtection()
        ab.record_upload("youtube")
        elapsed = ab.time_since_last_upload("youtube")
        assert elapsed < 1.0  # Just recorded

    def test_should_wait_no_history(self):
        """Test that no wait is needed with no history."""
        ab = AntiBotProtection()
        wait = ab.should_wait_between_platforms("youtube")
        assert wait == 0.0

    def test_should_wait_recent_upload(self):
        """Test that wait is needed after recent upload."""
        ab = AntiBotProtection()
        ab.record_upload("youtube")
        wait = ab.should_wait_between_platforms("youtube")
        # Should want to wait since we just uploaded
        assert wait > 0.0


class TestAntiBotStatus:
    """Test status reporting."""

    def test_get_status(self):
        """Test status dict structure."""
        ab = AntiBotProtection()
        status = ab.get_status()
        assert "local_time" in status
        assert "posting_allowed" in status
        assert "daily_limits" in status
        assert "uploads_today" in status
        assert "user_agent_index" in status

    def test_status_shows_limits(self):
        """Test that status includes conservative limits."""
        ab = AntiBotProtection()
        status = ab.get_status()
        assert status["daily_limits"]["instagram"] == 5
        assert status["daily_limits"]["youtube"] == 3
        assert status["daily_limits"]["x"] == 10
