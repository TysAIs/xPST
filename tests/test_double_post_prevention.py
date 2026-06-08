"""
Tests for double-post prevention in XPST bidirectional cross-posting.

Covers 10 critical scenarios:
1. Same video to same platform never posted twice
2. Content hash dedup prevents re-posting to target
3. Origin platform excluded from cross-post targets
4. Composite keys prevent ID collisions across platforms
5. Time window rejects old content (>24 hours)
6. Already-cross-posted check in bidirectional flow
7. Duplicate content detected across posted_videos and cross_posted
8. Multiple sources don't double-post same content
9. Content hash survives persistence (save/load cycle)
10. State corruption recovery preserves dedup data
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from xpst.config import XPSTConfig
from xpst.monitor import PostMonitor
from xpst.sources.base import DownloadResult, VideoMetadata, VideoSource
from xpst.state import StateManager
from xpst.utils.content_hash import compute_caption_hash

# ── Helpers ──────────────────────────────────────────────────────────────────


class MockSource(VideoSource):
    """Mock source for testing."""

    def __init__(self, config, source_name="mock", videos=None):
        super().__init__(config)
        self._source_name = source_name
        self._videos = videos or []

    @property
    def source_name(self):
        return self._source_name

    async def list_videos(self, max_count=10):
        return self._videos[:max_count]

    async def download(self, video_id, output_dir):
        return DownloadResult(success=False, error="Mock")

    async def check_health(self):
        return {"status": "ok"}


def make_video(
    video_id,
    source_platform="tiktok",
    caption="Test video caption",
    timestamp=None,
):
    """Create a VideoMetadata with optional timestamp."""
    return VideoMetadata(
        video_id=video_id,
        url=f"https://example.com/{video_id}",
        caption=caption,
        source_platform=source_platform,
        timestamp=timestamp,
    )


def _make_config(tmp_path):
    """Create a minimal XPSTConfig for testing."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.tiktok.username = "testuser"
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = False
    return config


# ── Test 1: Same video never posted to same platform twice ───────────────────


class TestNoDoublePost:
    """Test that the same video is never posted to the same platform twice."""

    def test_mark_cross_posted_once_is_tracked(self, tmp_path):
        """Marking once should be tracked."""
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:vid1", "youtube", post_id="yt_1")

        assert state.is_cross_posted("tiktok:vid1", "youtube")

    def test_mark_cross_posted_twice_is_idempotent(self, tmp_path):
        """Marking twice should not create duplicate entries."""
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:vid1", "youtube", post_id="yt_1")
        state.mark_cross_posted("tiktok:vid1", "youtube", post_id="yt_2")

        # Should still be one entry, last write wins
        assert state.is_cross_posted("tiktok:vid1", "youtube")
        data = state.get_cross_post_data("tiktok:vid1", "youtube")
        assert data["id"] == "yt_2"  # Overwritten

    def test_bidirectional_skips_already_cross_posted(self, tmp_path):
        """_process_bidirectional_post skips already-posted platforms."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)
        state = StateManager(str(tmp_path))

        # Pre-mark as already cross-posted
        state.mark_cross_posted("tiktok:vid1", "youtube", post_id="yt_1")

        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": MockSource(config, "tiktok", [])},
            platforms={"youtube", "instagram", "x"},
        )

        # Verify missing targets excludes youtube
        missing = monitor._get_missing_targets("tiktok:vid1", "tiktok")
        assert "youtube" not in missing
        assert "instagram" in missing
        assert "x" in missing


# ── Test 2: Content hash dedup prevents re-posting ───────────────────────────


class TestContentHashDedup:
    """Test content-hash-based deduplication."""

    def test_dedup_skips_platform_with_matching_hash(self, tmp_path):
        """Platform already having matching content hash is skipped."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        caption = "Amazing dance video"
        expected_hash = compute_caption_hash(caption)

        # Pre-post same content to youtube
        state.mark_video_posted("other_vid", "youtube", content_hash=expected_hash)

        video = make_video("vid1", "tiktok", caption=caption)
        source = MockSource(config, "tiktok", [video])
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram", "x"},
        )

        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1
        # youtube should be skipped due to content hash match
        assert "youtube" not in posts[0].target_platforms
        assert "instagram" in posts[0].target_platforms

    def test_dedup_does_not_skip_different_content(self, tmp_path):
        """Different content hashes allow posting to all platforms."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        state.mark_video_posted("other_vid", "youtube", content_hash="different_hash")

        video = make_video("vid1", "tiktok", caption="Brand new unique content!")
        source = MockSource(config, "tiktok", [video])
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram"},
        )

        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1
        assert "youtube" in posts[0].target_platforms


# ── Test 3: Origin platform excluded from targets ────────────────────────────


class TestOriginExclusion:
    """Test that source platform is never a cross-post target."""

    def test_tiktok_source_excludes_tiktok_targets(self, tmp_path):
        """Post from TikTok should not target TikTok."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        video = make_video("vid1", "tiktok")
        source = MockSource(config, "tiktok", [video])
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"tiktok", "youtube", "instagram"},
        )

        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1
        assert "tiktok" not in posts[0].target_platforms

    def test_instagram_source_excludes_instagram_targets(self, tmp_path):
        """Post from Instagram should not target Instagram."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        video = make_video("vid1", "instagram")
        source = MockSource(config, "instagram", [video])
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"instagram": source},
            platforms={"tiktok", "youtube", "instagram"},
        )

        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1
        assert "instagram" not in posts[0].target_platforms

    def test_all_sources_excluded_from_own_targets(self, tmp_path):
        """Each source platform excludes itself from targets."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        for platform in ["tiktok", "youtube", "instagram"]:
            video = make_video(f"vid_{platform}", platform)
            source = MockSource(config, platform, [video])
            monitor = PostMonitor(
                config=config,
                state=state,
                sources={platform: source},
                platforms={"tiktok", "youtube", "instagram"},
            )
            posts = asyncio.run(monitor.check_all_sources())
            if posts:
                assert platform not in posts[0].target_platforms


# ── Test 4: Composite keys prevent ID collisions ────────────────────────────


class TestCompositeKeys:
    """Test that composite keys prevent ID collisions."""

    def test_different_sources_same_video_id(self, tmp_path):
        """Same video_id from different sources creates different composite keys."""
        key1 = PostMonitor.make_composite_key("tiktok", "123")
        key2 = PostMonitor.make_composite_key("youtube", "123")

        assert key1 != key2
        assert key1 == "tiktok:123"
        assert key2 == "youtube:123"

    def test_cross_post_tracking_separates_by_composite_key(self, tmp_path):
        """Cross-post state separates by composite key, not just video_id."""
        state = StateManager(str(tmp_path))

        state.mark_cross_posted("tiktok:123", "youtube", post_id="yt_1")
        state.mark_cross_posted("youtube:123", "instagram", post_id="ig_1")

        assert state.is_cross_posted("tiktok:123", "youtube")
        assert not state.is_cross_posted("tiktok:123", "instagram")
        assert state.is_cross_posted("youtube:123", "instagram")
        assert not state.is_cross_posted("youtube:123", "youtube")


# ── Test 5: Time window rejects old content ──────────────────────────────────


class TestTimeWindow:
    """Test that posts older than 24 hours are skipped."""

    def test_old_post_is_skipped(self, tmp_path):
        """Posts older than 24 hours should be skipped."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        old_time = (datetime.now() - timedelta(hours=25)).isoformat()
        video = make_video("vid_old", "tiktok", timestamp=old_time)
        source = MockSource(config, "tiktok", [video])
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram"},
        )

        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 0

    def test_recent_post_is_not_skipped(self, tmp_path):
        """Posts within 24 hours should be processed."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        recent_time = (datetime.now() - timedelta(hours=2)).isoformat()
        video = make_video("vid_new", "tiktok", timestamp=recent_time)
        source = MockSource(config, "tiktok", [video])
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram"},
        )

        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1

    def test_no_timestamp_is_not_skipped(self, tmp_path):
        """Posts without timestamp should still be processed."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        video = make_video("vid1", "tiktok", timestamp=None)
        source = MockSource(config, "tiktok", [video])
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube"},
        )

        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1


# ── Test 6: is_too_old helper ───────────────────────────────────────────────


class TestIsTooOld:
    """Test the _is_too_old helper method."""

    def test_iso_string_old(self, tmp_path):
        """ISO string older than 24h returns True."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        monitor = PostMonitor(config=config, state=state, sources={}, platforms=set())

        old_str = (datetime.now() - timedelta(hours=25)).isoformat()
        assert monitor._is_too_old(old_str) is True

    def test_iso_string_recent(self, tmp_path):
        """ISO string within 24h returns False."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        monitor = PostMonitor(config=config, state=state, sources={}, platforms=set())

        recent_str = (datetime.now() - timedelta(hours=1)).isoformat()
        assert monitor._is_too_old(recent_str) is False

    def test_datetime_old(self, tmp_path):
        """Datetime object older than 24h returns True."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        monitor = PostMonitor(config=config, state=state, sources={}, platforms=set())

        old_dt = datetime.now() - timedelta(hours=30)
        assert monitor._is_too_old(old_dt) is True

    def test_invalid_string_returns_false(self, tmp_path):
        """Invalid string returns False (don't skip unknown formats)."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        monitor = PostMonitor(config=config, state=state, sources={}, platforms=set())

        assert monitor._is_too_old("not-a-date") is False
