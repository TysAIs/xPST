"""Tests for cross-platform post monitor."""


from xpst.config import XPSTConfig
from xpst.monitor import NewPost, PostMonitor
from xpst.sources.base import (
    DownloadResult,
    VideoMetadata,
    VideoSource,
)
from xpst.state import StateManager


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


def make_video(video_id, source_platform="tiktok", caption="Test"):
    """Helper to create a VideoMetadata."""
    return VideoMetadata(
        video_id=video_id,
        url=f"https://example.com/{video_id}",
        caption=caption,
        source_platform=source_platform,
    )


class TestCompositeKey:
    """Test composite key generation."""

    def test_basic_key(self):
        """Test basic composite key format."""
        key = PostMonitor.make_composite_key("tiktok", "12345")
        assert key == "tiktok:12345"

    def test_different_platforms_same_id(self):
        """Test that different platforms with same ID get different keys."""
        key_tt = PostMonitor.make_composite_key("tiktok", "123")
        key_yt = PostMonitor.make_composite_key("youtube", "123")
        key_ig = PostMonitor.make_composite_key("instagram", "123")
        assert key_tt != key_yt != key_ig

    def test_key_format(self):
        """Test key format consistency."""
        key = PostMonitor.make_composite_key("x", "abc_123")
        assert key == "x:abc_123"
        parts = key.split(":")
        assert len(parts) == 2
        assert parts[0] == "x"
        assert parts[1] == "abc_123"


class TestMonitorCheckAllSources:
    """Test check_all_sources method."""

    def test_empty_sources(self, tmp_path):
        """Test with no sources configured."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={},
            platforms={"youtube", "instagram", "x"},
        )
        import asyncio
        posts = asyncio.run(monitor.check_all_sources())
        assert posts == []

    def test_new_post_detected(self, tmp_path):
        """Test detecting a new post from a source."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        video = make_video("vid1", "tiktok")
        source = MockSource(config, "tiktok", [video])

        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram", "x"},
        )

        import asyncio
        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1
        assert posts[0].video_id == "vid1"
        assert posts[0].source_platform == "tiktok"
        assert set(posts[0].target_platforms) == {"youtube", "instagram", "x"}

    def test_already_cross_posted_excluded(self, tmp_path):
        """Test that already cross-posted items are excluded."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        # Mark as already cross-posted to all platforms
        composite_key = "tiktok:vid1"
        state.mark_cross_posted(composite_key, "youtube")
        state.mark_cross_posted(composite_key, "instagram")
        state.mark_cross_posted(composite_key, "x")
        state.save()

        video = make_video("vid1", "tiktok")
        source = MockSource(config, "tiktok", [video])

        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram", "x"},
        )

        import asyncio
        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 0

    def test_partial_cross_posting(self, tmp_path):
        """Test that partially cross-posted items show only missing targets."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        # Mark as posted to only youtube
        composite_key = "tiktok:vid1"
        state.mark_cross_posted(composite_key, "youtube")
        state.save()

        video = make_video("vid1", "tiktok")
        source = MockSource(config, "tiktok", [video])

        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram", "x"},
        )

        import asyncio
        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1
        assert "youtube" not in posts[0].target_platforms
        assert set(posts[0].target_platforms) == {"instagram", "x"}

    def test_source_not_in_targets(self, tmp_path):
        """Test that source platform is excluded from targets."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        video = make_video("vid1", "instagram")
        source = MockSource(config, "instagram", [video])

        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"instagram": source},
            platforms={"youtube", "instagram", "x", "tiktok"},
        )

        import asyncio
        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 1
        assert "instagram" not in posts[0].target_platforms
        assert set(posts[0].target_platforms) == {"youtube", "x", "tiktok"}

    def test_multiple_sources(self, tmp_path):
        """Test checking multiple sources."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        tt_video = make_video("tt1", "tiktok")
        yt_video = make_video("yt1", "youtube")
        tt_source = MockSource(config, "tiktok", [tt_video])
        yt_source = MockSource(config, "youtube", [yt_video])

        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": tt_source, "youtube": yt_source},
            platforms={"youtube", "instagram", "x", "tiktok"},
        )

        import asyncio
        posts = asyncio.run(monitor.check_all_sources())
        assert len(posts) == 2

        # Each should target all platforms except its source
        for post in posts:
            if post.source_platform == "tiktok":
                assert "tiktok" not in post.target_platforms
            elif post.source_platform == "youtube":
                assert "youtube" not in post.target_platforms

    def test_source_error_handled(self, tmp_path):
        """Test that source errors don't crash the monitor."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))

        class ErrorSource(MockSource):
            async def list_videos(self, max_count=10):
                raise RuntimeError("Connection failed")

        source = ErrorSource(config, "tiktok")
        monitor = PostMonitor(
            config=config,
            state=state,
            sources={"tiktok": source},
            platforms={"youtube", "instagram"},
        )

        import asyncio
        posts = asyncio.run(monitor.check_all_sources())
        assert posts == []


class TestMonitorIsFullyCrossPosted:
    """Test is_fully_cross_posted method."""

    def test_not_cross_posted(self, tmp_path):
        """Test not cross-posted at all."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        monitor = PostMonitor(
            config=config, state=state,
            sources={}, platforms={"youtube", "x"},
        )
        assert monitor.is_fully_cross_posted("tiktok:123", "tiktok") is False

    def test_partially_cross_posted(self, tmp_path):
        """Test partially cross-posted."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:123", "youtube")

        monitor = PostMonitor(
            config=config, state=state,
            sources={}, platforms={"youtube", "x"},
        )
        assert monitor.is_fully_cross_posted("tiktok:123", "tiktok") is False

    def test_fully_cross_posted(self, tmp_path):
        """Test fully cross-posted."""
        config = XPSTConfig()
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:123", "youtube")
        state.mark_cross_posted("tiktok:123", "x")

        monitor = PostMonitor(
            config=config, state=state,
            sources={}, platforms={"youtube", "x"},
        )
        assert monitor.is_fully_cross_posted("tiktok:123", "tiktok") is True


class TestNewPost:
    """Test NewPost dataclass."""

    def test_creation(self):
        """Test NewPost creation."""
        video = make_video("vid1", "tiktok")
        post = NewPost(
            video_id="vid1",
            composite_key="tiktok:vid1",
            source_platform="tiktok",
            caption="Test caption",
            url="https://example.com/vid1",
            metadata=video,
            target_platforms=["youtube", "instagram"],
        )
        assert post.video_id == "vid1"
        assert post.composite_key == "tiktok:vid1"
        assert post.source_platform == "tiktok"
        assert len(post.target_platforms) == 2


class TestStateManagerBidirectional:
    """Test state manager bidirectional methods."""

    def test_is_cross_posted_empty(self, tmp_path):
        """Test is_cross_posted with no data."""
        state = StateManager(str(tmp_path))
        assert state.is_cross_posted("tiktok:123", "youtube") is False

    def test_mark_and_check_cross_posted(self, tmp_path):
        """Test marking and checking cross-posted status."""
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:123", "youtube", post_id="yt_456")
        assert state.is_cross_posted("tiktok:123", "youtube") is True
        assert state.is_cross_posted("tiktok:123", "instagram") is False

    def test_mark_cross_posted_multiple_platforms(self, tmp_path):
        """Test marking multiple platforms."""
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:123", "youtube")
        state.mark_cross_posted("tiktok:123", "instagram")
        state.mark_cross_posted("tiktok:123", "x")

        assert state.is_cross_posted("tiktok:123", "youtube") is True
        assert state.is_cross_posted("tiktok:123", "instagram") is True
        assert state.is_cross_posted("tiktok:123", "x") is True

    def test_cross_post_data(self, tmp_path):
        """Test getting cross-post data."""
        state = StateManager(str(tmp_path))
        state.mark_cross_posted(
            "tiktok:123", "youtube",
            post_id="yt_456",
            post_url="https://youtube.com/shorts/456",
            caption="Test",
        )
        data = state.get_cross_post_data("tiktok:123", "youtube")
        assert data is not None
        assert data["id"] == "yt_456"
        assert data["url"] == "https://youtube.com/shorts/456"

    def test_cross_post_failed(self, tmp_path):
        """Test recording cross-post failures."""
        state = StateManager(str(tmp_path))
        state.mark_cross_post_failed("tiktok:123", "youtube", "Rate limited")

        # Should not be marked as cross-posted
        assert state.is_cross_posted("tiktok:123", "youtube") is False

    def test_different_composite_keys(self, tmp_path):
        """Test that different composite keys are independent."""
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:123", "youtube")
        state.mark_cross_posted("youtube:456", "instagram")

        assert state.is_cross_posted("tiktok:123", "youtube") is True
        assert state.is_cross_posted("youtube:456", "youtube") is False
        assert state.is_cross_posted("youtube:456", "instagram") is True
        assert state.is_cross_posted("tiktok:123", "instagram") is False

    def test_statistics_include_cross_posted_count(self, tmp_path):
        """Test that statistics include cross-posted count."""
        state = StateManager(str(tmp_path))
        state.mark_cross_posted("tiktok:123", "youtube")
        state.mark_cross_posted("tiktok:456", "instagram")

        stats = state.get_statistics()
        assert "cross_posted_count" in stats
        assert stats["cross_posted_count"] == 2
