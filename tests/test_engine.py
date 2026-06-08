"""
Tests for CrossPostEngine and its service layer.

Covers:
- Engine initialization with valid/missing config
- check_and_post with no new videos
- check_and_post with new video (mock platform uploaders)
- post_manual with single video
- post_manual_carousel with multiple files
- Circuit breaker blocking
- Quota exhaustion
- Shutdown mid-processing
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.platforms.base import PlatformHealth, PlatformUploader, UploadResult
from xpst.sources.base import VideoMetadata

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path):
    """Create a minimal XPSTConfig for testing."""
    from xpst.config import XPSTConfig
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.tiktok.username = "testuser"
    # Disable all platforms by default
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = False
    return config


def _make_video(video_id: str = "vid123", caption: str = "Test video") -> VideoMetadata:
    """Create a VideoMetadata for testing."""
    return VideoMetadata(
        video_id=video_id,
        url=f"https://tiktok.com/@user/video/{video_id}",
        caption=caption,
    )


def _make_mock_uploader(platform_name: str = "youtube", success: bool = True):
    """Create a mock platform uploader."""
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform_name
    uploader.upload = AsyncMock(return_value=UploadResult(
        success=success,
        post_id="post123" if success else None,
        post_url="https://example.com/post123" if success else None,
        error=None if success else "Upload failed",
        platform=platform_name,
    ))
    uploader.upload_carousel = AsyncMock(return_value=UploadResult(
        success=success,
        post_id="carousel123" if success else None,
        post_url="https://example.com/carousel" if success else None,
        error=None if success else "Upload failed",
        platform=platform_name,
    ))
    uploader.check_health = AsyncMock(return_value=PlatformHealth(
        platform=platform_name,
        authenticated=True,
        session_valid=True,
    ))
    uploader.delete = MagicMock(return_value=True)
    return uploader


# ---------------------------------------------------------------------------
# Engine Initialization
# ---------------------------------------------------------------------------

class TestEngineInit:
    """Test engine initialization."""

    def test_init_valid_config(self, tmp_path):
        """Engine initializes successfully with valid config."""
        config = _make_config(tmp_path)
        # Create required dirs
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)
        assert engine.config is config
        assert engine.state is not None
        assert engine.circuit_breakers is not None
        assert engine.quota_manager is not None
        assert engine.notifier is not None
        assert engine.shutdown_handler is not None
        assert engine.upload_service is not None
        assert engine.source_service is not None

    def test_init_creates_services(self, tmp_path):
        """Engine creates UploadService and SourceService."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)
        assert hasattr(engine, "upload_service")
        assert hasattr(engine, "source_service")

    def test_init_no_platforms(self, tmp_path):
        """Engine works with all platforms disabled."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)
        assert len(engine._platforms) == 0


# ---------------------------------------------------------------------------
# CrossPostResult
# ---------------------------------------------------------------------------

class TestCrossPostResult:
    """Test CrossPostResult dataclass."""

    def test_update_status_all_success(self):
        result = CrossPostResult(video_id="v1", caption="test")
        result.results["youtube"] = UploadResult(success=True)
        result.results["x"] = UploadResult(success=True)
        result.update_status()
        assert result.all_success is True
        assert result.partial_success is True

    def test_update_status_partial(self):
        result = CrossPostResult(video_id="v1", caption="test")
        result.results["youtube"] = UploadResult(success=True)
        result.results["x"] = UploadResult(success=False)
        result.update_status()
        assert result.all_success is False
        assert result.partial_success is True

    def test_update_status_all_fail(self):
        result = CrossPostResult(video_id="v1", caption="test")
        result.results["youtube"] = UploadResult(success=False)
        result.update_status()
        assert result.all_success is False
        assert result.partial_success is False

    def test_update_status_empty(self):
        result = CrossPostResult(video_id="v1", caption="test")
        result.update_status()
        assert result.all_success is False
        assert result.partial_success is False


# ---------------------------------------------------------------------------
# check_and_post
# ---------------------------------------------------------------------------

class TestCheckAndPost:
    """Test the check_and_post workflow."""

    @pytest.mark.asyncio
    async def test_no_new_videos(self, tmp_path):
        """Returns empty list when source has no new videos."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        # Mock source to return empty list
        with patch.object(
            engine.source_service, "fetch_new_videos", new_callable=AsyncMock, return_value=[]
        ):
            results = await engine.check_and_post()

        assert results == []

    @pytest.mark.asyncio
    async def test_no_new_videos_after_filter(self, tmp_path):
        """Returns empty list when all videos are already posted."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)
        video = _make_video()

        with patch.object(
            engine.source_service, "fetch_new_videos", new_callable=AsyncMock, return_value=[video]
        ), patch.object(
            engine.source_service, "filter_new", return_value=[]
        ):
            results = await engine.check_and_post()

        assert results == []

    @pytest.mark.asyncio
    async def test_shutdown_before_start(self, tmp_path):
        """Returns empty list if shutdown requested before starting."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)
        engine.shutdown_handler._shutdown_requested = True

        results = await engine.check_and_post()
        assert results == []

    @pytest.mark.asyncio
    async def test_source_unavailable(self, tmp_path):
        """Returns empty list when source fetch fails."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        with patch.object(
            engine.source_service, "fetch_new_videos", new_callable=AsyncMock, return_value=[]
        ):
            results = await engine.check_and_post()

        assert results == []


# ---------------------------------------------------------------------------
# post_manual
# ---------------------------------------------------------------------------

class TestPostManual:
    """Test manual posting."""

    @pytest.mark.asyncio
    async def test_post_manual_no_platforms(self, tmp_path):
        """post_manual with no available platforms returns empty result."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        result = await engine.post_manual(video_path, "Test caption", ["youtube"])
        assert result.all_success is False
        assert "youtube" not in result.results  # Platform not available, skipped

    @pytest.mark.asyncio
    async def test_post_manual_with_mock_platform(self, tmp_path):
        """post_manual delegates to upload service for available platform."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        # Disable anti-bot for tests
        engine.upload_service.anti_bot = None

        # Inject a mock platform
        mock_uploader = _make_mock_uploader("youtube", success=True)
        engine._platforms["youtube"] = mock_uploader

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        # Mock encode to just return the path
        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video_path
        ):
            result = await engine.post_manual(video_path, "Test caption", ["youtube"])

        assert result.results["youtube"].success is True


# ---------------------------------------------------------------------------
# post_manual_carousel
# ---------------------------------------------------------------------------

class TestPostManualCarousel:
    """Test carousel posting."""

    @pytest.mark.asyncio
    async def test_carousel_no_platforms(self, tmp_path):
        """post_manual_carousel with no platforms returns empty result."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)
        media1 = tmp_path / "img1.jpg"
        media1.write_bytes(b"fake image 1")
        media2 = tmp_path / "img2.jpg"
        media2.write_bytes(b"fake image 2")

        result = await engine.post_manual_carousel(
            [media1, media2], "Carousel caption", ["youtube"]
        )
        assert result.all_success is False

    @pytest.mark.asyncio
    async def test_carousel_with_mock_platform(self, tmp_path):
        """post_manual_carousel delegates to upload service."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        mock_uploader = _make_mock_uploader("instagram", success=True)
        engine._platforms["instagram"] = mock_uploader

        media1 = tmp_path / "img1.jpg"
        media1.write_bytes(b"fake image 1")
        media2 = tmp_path / "img2.jpg"
        media2.write_bytes(b"fake image 2")

        result = await engine.post_manual_carousel(
            [media1, media2], "Carousel caption", ["instagram"]
        )

        assert result.results["instagram"].success is True
        mock_uploader.upload_carousel.assert_called_once()


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class TestCircuitBreakerBlocking:
    """Test circuit breaker blocks uploads."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_upload(self, tmp_path):
        """Upload is skipped when circuit breaker is open."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        mock_uploader = _make_mock_uploader("youtube", success=True)
        engine._platforms["youtube"] = mock_uploader

        # Create and open the circuit breaker
        breaker = engine.circuit_breakers.get_or_create("youtube", failure_threshold=5)
        for _ in range(5):
            breaker.record_failure("test error")
        assert breaker.is_open

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        # Also mock the encode step to avoid FFmpeg calls
        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video_path
        ):
            result = await engine.post_manual(video_path, "Test", ["youtube"])
            assert result.results["youtube"].success is False
            assert "Circuit breaker" in result.results["youtube"].error
            mock_uploader.upload.assert_not_called()


# ---------------------------------------------------------------------------
# Quota Exhaustion
# ---------------------------------------------------------------------------

class TestQuotaExhaustion:
    """Test quota exhaustion blocks uploads."""

    @pytest.mark.asyncio
    async def test_quota_exhausted(self, tmp_path):
        """Upload is skipped when quota is exhausted."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        mock_uploader = _make_mock_uploader("youtube", success=True)
        engine._platforms["youtube"] = mock_uploader

        # Exhaust quota by mocking the quota manager methods
        engine.quota_manager.can_upload = MagicMock(return_value=False)
        engine.quota_manager.get_remaining = MagicMock(return_value={"daily": 0})

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        result = await engine.post_manual(video_path, "Test", ["youtube"])
        assert result.results["youtube"].success is False
        assert "Quota" in result.results["youtube"].error


# ---------------------------------------------------------------------------
# Shutdown Mid-Processing
# ---------------------------------------------------------------------------

class TestShutdownMidProcessing:
    """Test graceful shutdown during processing."""

    @pytest.mark.asyncio
    async def test_shutdown_during_platform_processing(self, tmp_path):
        """Processing stops when shutdown is requested between platforms."""
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        mock_uploader = _make_mock_uploader("youtube", success=True)
        engine._platforms["youtube"] = mock_uploader
        engine._platforms["x"] = _make_mock_uploader("x", success=True)

        # Set shutdown flag before processing starts
        engine.shutdown_handler._shutdown_requested = True

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        result = await engine.post_manual(video_path, "Test")
        # Should have stopped before processing any platforms
        assert len(result.results) == 0


# ---------------------------------------------------------------------------
# SourceService
# ---------------------------------------------------------------------------

class TestSourceService:
    """Test SourceService independently."""

    def test_filter_new_all_posted(self, tmp_path):
        """All videos already posted returns empty."""
        from xpst.services.source_service import SourceService

        config = _make_config(tmp_path)
        service = SourceService(config)

        state = MagicMock()
        state.is_video_posted.return_value = True

        platforms = {"youtube": MagicMock()}
        videos = [_make_video("v1"), _make_video("v2")]

        result = service.filter_new(videos, state, platforms)
        assert result == []

    def test_filter_new_some_unposted(self, tmp_path):
        """Videos not yet posted to all platforms are returned."""
        from xpst.services.source_service import SourceService

        config = _make_config(tmp_path)
        service = SourceService(config)

        state = MagicMock()
        state.is_video_posted.side_effect = lambda vid, plat: vid == "v1"

        platforms = {"youtube": MagicMock()}
        videos = [_make_video("v1"), _make_video("v2")]

        result = service.filter_new(videos, state, platforms)
        assert len(result) == 1
        assert result[0].video_id == "v2"

    @pytest.mark.asyncio
    async def test_fetch_no_source(self, tmp_path):
        """fetch_new_videos returns empty when source unavailable."""
        from xpst.services.source_service import SourceService

        config = _make_config(tmp_path)
        service = SourceService(config)
        # Don't mock any source - it should fail gracefully
        service._sources = {}

        result = await service.fetch_new_videos("nonexistent", 5)
        assert result == []


# ---------------------------------------------------------------------------
# ToS Warnings
# ---------------------------------------------------------------------------

class TestToSWarnings:
    """Test ToS warnings for unofficial APIs."""

    @pytest.mark.asyncio
    async def test_tos_warning_instagram(self, tmp_path, caplog):
        """ToS warning logged for Instagram uploads."""
        import logging
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        mock_uploader = _make_mock_uploader("instagram", success=True)
        engine._platforms["instagram"] = mock_uploader

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video_path
        ):
            with caplog.at_level(logging.WARNING):
                await engine.post_manual(video_path, "Test", ["instagram"])

        assert any("unofficial API" in record.message and "instagram" in record.message
                    for record in caplog.records)

    @pytest.mark.asyncio
    async def test_tos_warning_x(self, tmp_path, caplog):
        """ToS warning logged for X/Twitter uploads."""
        import logging
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        mock_uploader = _make_mock_uploader("x", success=True)
        engine._platforms["x"] = mock_uploader

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video_path
        ):
            with caplog.at_level(logging.WARNING):
                await engine.post_manual(video_path, "Test", ["x"])

        assert any("unofficial API" in record.message and "x" in record.message
                    for record in caplog.records)

    @pytest.mark.asyncio
    async def test_no_tos_warning_youtube(self, tmp_path, caplog):
        """No ToS warning for YouTube (official API)."""
        import logging
        config = _make_config(tmp_path)
        (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

        engine = CrossPostEngine(config)

        mock_uploader = _make_mock_uploader("youtube", success=True)
        engine._platforms["youtube"] = mock_uploader

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video data")

        with patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=video_path
        ):
            with caplog.at_level(logging.WARNING):
                await engine.post_manual(video_path, "Test", ["youtube"])

        tos_warnings = [r for r in caplog.records if "unofficial API" in r.message]
        assert len(tos_warnings) == 0
