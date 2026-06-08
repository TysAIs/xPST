"""
Integration tests for xPST cross-posting pipeline.

Tests the full workflow from TikTok source through multi-platform upload,
with isolated state via tmp_path and mocked platform APIs.

Covers:
- Full pipeline: TikTok → YouTube + X + Instagram
- Duplicate prevention (state-based deduplication)
- Circuit breaker activation after repeated failures
- Rate limit / quota enforcement
- Aggregated health check across all platforms
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.engine import CrossPostEngine
from xpst.platforms.base import PlatformHealth, PlatformUploader, UploadResult
from xpst.sources.base import DownloadResult, VideoMetadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path):
    """Create a minimal XPSTConfig for integration testing."""
    from xpst.config import XPSTConfig

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.tiktok.username = "testuser"
    # Enable all three target platforms
    config.youtube.enabled = True
    config.x.enabled = True
    config.instagram.enabled = True
    return config


def _make_video(video_id: str = "vid_001", caption: str = "Integration test video") -> VideoMetadata:
    """Create a VideoMetadata for testing."""
    return VideoMetadata(
        video_id=video_id,
        url=f"https://tiktok.com/@testuser/video/{video_id}",
        caption=caption,
    )


def _make_mock_uploader(platform_name: str, success: bool = True, raise_exc: bool = False):
    """Create a mock platform uploader.

    Args:
        platform_name: Platform identifier.
        success: Value for UploadResult.success (ignored when raise_exc=True).
        raise_exc: If True, upload() raises Exception instead of returning a result.
    """
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform_name

    if raise_exc:
        uploader.upload = AsyncMock(side_effect=Exception(f"{platform_name} upload failed"))
    else:
        uploader.upload = AsyncMock(return_value=UploadResult(
            success=success,
            post_id=f"{platform_name}_post_001" if success else None,
            post_url=f"https://{platform_name}.example.com/post/001" if success else None,
            error=None if success else f"{platform_name} upload failed",
            platform=platform_name,
        ))

    uploader.check_health = AsyncMock(return_value=PlatformHealth(
        platform=platform_name,
        authenticated=True,
        session_valid=True,
    ))
    uploader.delete = AsyncMock(return_value=True)
    return uploader


def _make_mock_source():
    """Create a mock TikTok source with download support."""
    source = MagicMock()
    source.source_name = "tiktok"
    source.check_health = AsyncMock(return_value={"status": "ok"})
    return source


def _setup_engine_with_platforms(tmp_path: Path, youtube_exc: bool = False,
                                  x_exc: bool = False, ig_exc: bool = False):
    """Build a CrossPostEngine with mock platforms injected."""
    config = _make_config(tmp_path)
    (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)

    engine = CrossPostEngine(config)

    # Inject mock uploaders
    engine._platforms["youtube"] = _make_mock_uploader("youtube", raise_exc=youtube_exc)
    engine._platforms["x"] = _make_mock_uploader("x", raise_exc=x_exc)
    engine._platforms["instagram"] = _make_mock_uploader("instagram", raise_exc=ig_exc)

    # Disable anti-bot for deterministic tests
    engine.upload_service.anti_bot = None

    return engine


# ---------------------------------------------------------------------------
# 1. Full pipeline: TikTok → all three platforms
# ---------------------------------------------------------------------------

class TestFullPipelineTikTokToAll:
    """End-to-end: one TikTok video → uploaded to YouTube, X, and Instagram."""

    @pytest.mark.asyncio
    async def test_full_pipeline_tiktok_to_all(self, tmp_path):
        """Verify all 3 uploaders are called and state tracks all 3 platforms."""
        engine = _setup_engine_with_platforms(tmp_path)
        video = _make_video("pipeline_vid_001", "Hello from TikTok!")

        # Create a fake downloaded video file
        download_dir = Path(engine.config.video.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        fake_video = download_dir / "pipeline_vid_001.mp4"
        fake_video.write_bytes(b"\x00" * 1024)  # 1 KB fake video

        # Mock source service to return our video
        with patch.object(
            engine.source_service, "fetch_new_videos",
            new_callable=AsyncMock, return_value=[video],
        ), patch.object(
            engine.source_service, "filter_new",
            return_value=[video],
        ), patch.object(
            engine._sources["tiktok"], "download",
            new_callable=AsyncMock,
            return_value=DownloadResult(success=True, video_path=fake_video),
        ), patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=fake_video,
        ):
            results = await engine.check_and_post()

        # Verify results
        assert len(results) == 1
        result = results[0]
        assert result.video_id == "pipeline_vid_001"

        # All three uploaders were called
        for platform_name in ("youtube", "x", "instagram"):
            mock_uploader = engine._platforms[platform_name]
            mock_uploader.upload.assert_called_once()
            assert platform_name in result.results
            assert result.results[platform_name].success is True

        # State records the video as posted to all 3
        for platform_name in ("youtube", "x", "instagram"):
            assert engine.state.is_video_posted("pipeline_vid_001", platform_name)


# ---------------------------------------------------------------------------
# 2. Duplicate prevention
# ---------------------------------------------------------------------------

class TestDuplicatePrevention:
    """A video already in state should not be uploaded a second time."""

    @pytest.mark.asyncio
    async def test_duplicate_prevention(self, tmp_path):
        """Second attempt to post the same video_id is skipped."""
        engine = _setup_engine_with_platforms(tmp_path)
        video = _make_video("dup_vid_001", "Duplicate test")

        download_dir = Path(engine.config.video.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        fake_video = download_dir / "dup_vid_001.mp4"
        fake_video.write_bytes(b"\x00" * 1024)

        # --- First pass: video is new → uploaders are called ---
        with patch.object(
            engine.source_service, "fetch_new_videos",
            new_callable=AsyncMock, return_value=[video],
        ), patch.object(
            engine.source_service, "filter_new",
            return_value=[video],
        ), patch.object(
            engine._sources["tiktok"], "download",
            new_callable=AsyncMock,
            return_value=DownloadResult(success=True, video_path=fake_video),
        ), patch.object(
            engine.upload_service, "_encode_for_platform",
            new_callable=AsyncMock, return_value=fake_video,
        ):
            results_1 = await engine.check_and_post()

        assert len(results_1) == 1
        for platform_name in ("youtube", "x", "instagram"):
            assert engine._platforms[platform_name].upload.call_count == 1

        # --- Second pass: same video → filter_new filters it out ---
        # filter_new checks state.is_video_posted, which now returns True
        with patch.object(
            engine.source_service, "fetch_new_videos",
            new_callable=AsyncMock, return_value=[video],
        ):
            # Don't mock filter_new — let it use real state
            results_2 = await engine.check_and_post()

        # No new results (video was filtered out)
        assert len(results_2) == 0

        # Uploaders were NOT called a second time (still at count 1)
        for platform_name in ("youtube", "x", "instagram"):
            assert engine._platforms[platform_name].upload.call_count == 1


# ---------------------------------------------------------------------------
# 3. Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    """After N consecutive failures the circuit breaker opens."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(self, tmp_path):
        """5 failed uploads → state marks circuit_breaker_open = True."""
        engine = _setup_engine_with_platforms(tmp_path, youtube_exc=True)

        download_dir = Path(engine.config.video.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        # Process 5 different videos, each failing on youtube
        for i in range(5):
            vid = _make_video(f"cb_vid_{i:03d}", f"Circuit breaker test {i}")
            fake_video = download_dir / f"cb_vid_{i:03d}.mp4"
            fake_video.write_bytes(b"\x00" * 512)

            # Reset mock call counts for clarity
            engine._platforms["youtube"].upload.reset_mock()

            with patch.object(
                engine.source_service, "fetch_new_videos",
                new_callable=AsyncMock, return_value=[vid],
            ), patch.object(
                engine.source_service, "filter_new",
                return_value=[vid],
            ), patch.object(
                engine._sources["tiktok"], "download",
                new_callable=AsyncMock,
                return_value=DownloadResult(success=True, video_path=fake_video),
            ), patch.object(
                engine.upload_service, "_encode_for_platform",
                new_callable=AsyncMock, return_value=fake_video,
            ):
                await engine.check_and_post()

        # Verify circuit breaker is open in state
        yt_health = engine.state.get_platform_health("youtube")
        assert yt_health["circuit_breaker_open"] is True
        assert yt_health["failures"] >= 5

        # Also verify the CircuitBreakerManager agrees
        cb = engine.circuit_breakers._breakers.get("youtube")
        assert cb is not None
        assert cb.is_open


# ---------------------------------------------------------------------------
# 4. Rate limit / quota enforcement
# ---------------------------------------------------------------------------

class TestRateLimitEnforcement:
    """QuotaManager should block uploads once the daily limit is reached."""

    def test_rate_limit_enforcement(self, tmp_path):
        """After hitting the daily limit, can_upload returns False."""
        from xpst.utils.quota import QuotaManager

        qm = QuotaManager(str(tmp_path))

        # Set youtube daily limit to 2
        qm.quotas["youtube"].daily_limit = 2
        qm.quotas["youtube"].used_today = 0

        # First two uploads allowed
        assert qm.can_upload("youtube") is True
        qm.record_upload("youtube")
        assert qm.can_upload("youtube") is True
        qm.record_upload("youtube")

        # Third attempt blocked
        assert qm.can_upload("youtube") is False

    def test_rate_limit_other_platforms_unaffected(self, tmp_path):
        """Exhausting one platform's quota does not affect others."""
        from xpst.utils.quota import QuotaManager

        qm = QuotaManager(str(tmp_path))
        qm.quotas["youtube"].daily_limit = 1
        qm.quotas["youtube"].used_today = 0

        qm.record_upload("youtube")
        assert qm.can_upload("youtube") is False
        # Other platforms still available
        assert qm.can_upload("x") is True
        assert qm.can_upload("instagram") is True


# ---------------------------------------------------------------------------
# 5. Health check aggregation
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """engine.check_health() aggregates results from all platforms."""

    @pytest.mark.asyncio
    async def test_health_check_all_platforms(self, tmp_path):
        """check_health returns data for all 3 platforms."""
        engine = _setup_engine_with_platforms(tmp_path)

        # Each mock uploader already has check_health returning PlatformHealth
        health = await engine.check_health()

        # Verify platforms key exists and has all 3
        assert "platforms" in health
        for platform_name in ("youtube", "x", "instagram"):
            assert platform_name in health["platforms"]
            plat = health["platforms"][platform_name]
            assert plat["authenticated"] is True
            assert plat["session_valid"] is True

    @pytest.mark.asyncio
    async def test_health_check_handles_failures(self, tmp_path):
        """check_health gracefully handles a platform that throws."""
        engine = _setup_engine_with_platforms(tmp_path)

        # Make youtube's check_health raise
        engine._platforms["youtube"].check_health = AsyncMock(
            side_effect=ConnectionError("YouTube unreachable")
        )

        health = await engine.check_health()

        # YouTube should report error, others succeed
        assert "error" in health["platforms"]["youtube"]
        assert health["platforms"]["x"]["authenticated"] is True
        assert health["platforms"]["instagram"]["authenticated"] is True
