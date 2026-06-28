"""Engine correctness tests (F4 / G01-G09).

Covers the double-post defect cluster: the hardcoded download source, the
DLQ clear that erased posted history, the composite-key mirror corruption,
the cross-flow content-hash idempotency guard at the upload chokepoint,
and the ambiguous-retry double-post window.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.services.upload_service import UploadService
from xpst.state import StateManager
from xpst.utils.content_hash import compute_content_hash
from xpst.utils.retry import RetryConfig, retry_operation

# ---------------------------------------------------------------------------
# State facade (G02, G03, G05)
# ---------------------------------------------------------------------------


class TestStateCorrectness:
    def test_clear_dlq_preserves_record(self, tmp_path):
        """G02: clearing the DLQ must clear errors, never posted history."""
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted(
            "vid1", "youtube", post_id="p1",
            content_hash="h1", source_platform="tiktok",
        )
        sm.mark_video_failed("vid1", "instagram", "boom")

        cleared = sm.clear_dead_letter_queue("vid1")

        assert cleared >= 1
        assert sm.get_video("vid1") is not None, "record was deleted"
        assert sm.is_video_posted("vid1", "youtube"), "posted history erased"
        assert sm.clear_dead_letter_queue("vid1") == 0

    def test_mark_video_posted_records_source_and_hash(self, tmp_path):
        """G03: the unidirectional record path must carry source + hash."""
        sm = StateManager(str(tmp_path))
        sm.mark_video_posted(
            "vid2", "x", content_hash="abc123", source_platform="youtube"
        )
        assert sm.get_by_hash("abc123") == "vid2"
        video = sm.get_video("vid2")
        assert video is not None
        assert video.get("source_platform") == "youtube"

    def test_mark_cross_posted_composite_mirror(self, tmp_path):
        """G05: composite ids must not be double-prefixed into junk keys."""
        sm = StateManager(str(tmp_path))
        sm.mark_cross_posted("instagram:123", "youtube", post_id="p")
        cross = sm._state.get("cross_posted", {})
        assert "tiktok:instagram:123" not in cross
        assert "instagram:123" in cross

        sm.mark_cross_posted("plain456", "youtube", post_id="p")
        assert "tiktok:plain456" in sm._state.get("cross_posted", {})

    def test_mark_cross_posted_derives_source_platform(self, tmp_path):
        sm = StateManager(str(tmp_path))
        sm.mark_cross_posted("youtube:abc", "x", post_id="p")
        video = sm.get_video("youtube:abc")
        assert video is not None
        assert video.get("source_platform") == "youtube"


# ---------------------------------------------------------------------------
# Retry ambiguity window (G07)
# ---------------------------------------------------------------------------


class TestRetryAmbiguity:
    @pytest.mark.asyncio
    async def test_retry_no_double_post(self):
        """G07: ambiguous failures must NOT blind-retry when unsafe —
        the request may have succeeded server-side with the response lost."""
        calls = 0

        async def flaky_upload():
            nonlocal calls
            calls += 1
            return UploadResult(
                success=False,
                error="Connection timeout during upload",
                platform="instagram",
            )

        result = await retry_operation(
            flaky_upload,
            config=RetryConfig(max_retries=3),
            platform="instagram",
            ambiguous_safe=False,
        )
        assert calls == 1, "ambiguous failure was blind-retried"
        assert result.success is False

    @pytest.mark.asyncio
    async def test_unambiguous_errors_still_retry_when_unsafe(self):
        """Rate limits and 5xx are unambiguous — they keep retrying."""
        calls = 0

        async def rate_limited():
            nonlocal calls
            calls += 1
            if calls < 2:
                return UploadResult(
                    success=False, error="429 too many requests", platform="instagram"
                )
            return UploadResult(success=True, platform="instagram")

        result = await retry_operation(
            rate_limited,
            config=RetryConfig(max_retries=2, fixed_delays=[0.01, 0.01]),
            platform="instagram",
            ambiguous_safe=False,
        )
        assert result.success is True
        assert calls == 2

    @pytest.mark.asyncio
    async def test_ambiguous_retries_when_safe(self):
        """Platforms with server-side dedup (X) keep ambiguous retries."""
        calls = 0

        async def flaky_then_ok():
            nonlocal calls
            calls += 1
            if calls < 2:
                return UploadResult(
                    success=False, error="Connection reset", platform="x"
                )
            return UploadResult(success=True, platform="x")

        result = await retry_operation(
            flaky_then_ok,
            config=RetryConfig(max_retries=2, fixed_delays=[0.01, 0.01]),
            platform="x",
            ambiguous_safe=True,
        )
        assert result.success is True
        assert calls == 2


# ---------------------------------------------------------------------------
# Upload chokepoint idempotency guard (G03/G04/G09 cross-flow dedup)
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path, state: StateManager) -> UploadService:
    config = MagicMock()
    return UploadService(
        video_processor=MagicMock(),
        circuit_breakers=MagicMock(),
        quota_manager=MagicMock(),
        state=state,
        notifier=MagicMock(),
        shutdown_handler=MagicMock(),
        config=config,
        anti_bot=None,
    )


def _make_uploader(platform: str, success: bool = True) -> MagicMock:
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform
    uploader.upload = AsyncMock(
        return_value=UploadResult(
            success=success,
            post_id="p1" if success else None,
            post_url="https://example.com/p1" if success else None,
            platform=platform,
        )
    )
    return uploader


class TestChokepointDedup:
    @pytest.mark.asyncio
    async def test_cross_flow_dedup(self, tmp_path):
        """The same bytes posted under a DIFFERENT id (other flow) must be
        skipped: the file fingerprint is the cross-flow identity."""
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"fake video bytes" * 1024)
        state = StateManager(str(tmp_path))

        content_hash = compute_content_hash(file_path=video, filename=video.name)
        state.mark_video_posted(
            "tiktok:orig", "youtube", post_id="p0",
            content_hash=content_hash, source_platform="tiktok",
        )

        service = _make_service(tmp_path, state)
        uploader = _make_uploader("youtube")

        result = await service.upload_to_platform(
            uploader=uploader,
            video_path=video,
            caption="caption",
            platform_name="youtube",
            video_id="unidirectional-id",
            source_platform="youtube",
        )

        assert result.success is True
        assert result.metadata.get("already_posted") is True
        assert result.metadata.get("dedup") == "content_hash"
        uploader.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_manual_post_idempotent(self, tmp_path):
        """G09: posting the same file twice uploads exactly once."""
        video = tmp_path / "manual.mp4"
        video.write_bytes(b"manual bytes" * 2048)
        state = StateManager(str(tmp_path))
        service = _make_service(tmp_path, state)
        uploader = _make_uploader("instagram")

        first = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="manual-1",
            source_platform="local",
        )
        second = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="manual-1",
            source_platform="local",
        )

        assert first.success is True and not first.metadata.get("already_posted")
        assert second.success is True and second.metadata.get("already_posted") is True
        assert uploader.upload.await_count == 1

    @pytest.mark.asyncio
    async def test_same_content_different_platform_still_uploads(self, tmp_path):
        """Dedup is per-platform: posted-to-youtube must not block instagram."""
        video = tmp_path / "clip2.mp4"
        video.write_bytes(b"other bytes" * 1024)
        state = StateManager(str(tmp_path))
        content_hash = compute_content_hash(file_path=video, filename=video.name)
        state.mark_video_posted(
            "vid", "youtube", content_hash=content_hash, source_platform="tiktok"
        )

        service = _make_service(tmp_path, state)
        uploader = _make_uploader("instagram")

        result = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="instagram", video_id="vid",
            source_platform="tiktok",
        )
        assert result.success is True
        assert not result.metadata.get("already_posted")
        uploader.upload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_success_records_hash_and_source(self, tmp_path):
        """G03 end-to-end: the chokepoint records hash + source platform."""
        video = tmp_path / "clip3.mp4"
        video.write_bytes(b"record me" * 1024)
        state = StateManager(str(tmp_path))
        service = _make_service(tmp_path, state)

        await service.upload_to_platform(
            uploader=_make_uploader("youtube"), video_path=video, caption="c",
            platform_name="youtube", video_id="vid9",
            source_platform="instagram",
        )

        content_hash = compute_content_hash(file_path=video, filename=video.name)
        assert state.get_by_hash(content_hash) == "vid9"
        record = state.get_video("vid9")
        assert record is not None
        assert record.get("source_platform") == "instagram"


# ---------------------------------------------------------------------------
# Engine source threading (G01)
# ---------------------------------------------------------------------------


class TestProcessVideoSource:
    @pytest.mark.asyncio
    async def test_process_video_nontiktok(self, tmp_path):
        """G01: _process_video must download from the REQUESTED source —
        it was hardcoded to tiktok, breaking posting from any other source."""
        from xpst.config import XPSTConfig
        from xpst.engine import CrossPostEngine
        from xpst.sources.base import VideoMetadata

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.video.download_dir = str(tmp_path / "downloads")
        config.tiktok.username = "testuser"
        config.youtube.enabled = False
        config.x.enabled = False
        config.instagram.enabled = False

        engine = CrossPostEngine(config)

        fake_download = MagicMock()
        fake_download.success = False
        fake_download.error = "stop here"
        fake_download.video_path = None
        youtube_source = MagicMock()
        youtube_source.download = AsyncMock(return_value=fake_download)
        engine._sources = {"youtube": youtube_source}

        video = VideoMetadata(
            video_id="yvid", url="https://youtube.com/shorts/yvid", caption="c"
        )
        await engine._process_video(video, source="youtube")

        youtube_source.download.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_process_video_unknown_source_is_safe(self, tmp_path):
        from xpst.config import XPSTConfig
        from xpst.engine import CrossPostEngine
        from xpst.sources.base import VideoMetadata

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.video.download_dir = str(tmp_path / "downloads")
        config.youtube.enabled = False
        config.x.enabled = False
        config.instagram.enabled = False

        engine = CrossPostEngine(config)
        engine._sources = {}

        video = VideoMetadata(video_id="v", url="u", caption="c")
        result = await engine._process_video(video, source="nonexistent")
        assert result.results == {}


# ---------------------------------------------------------------------------
# Pre-flight duration caps (G08)
# ---------------------------------------------------------------------------


class TestDurationLimits:
    def _service_with_duration(self, tmp_path, duration: float) -> UploadService:
        state = StateManager(str(tmp_path))
        processor = MagicMock()
        processor.get_video_info.return_value = {"format": {"duration": str(duration)}}
        return UploadService(
            video_processor=processor, circuit_breakers=MagicMock(),
            quota_manager=MagicMock(), state=state, notifier=MagicMock(),
            shutdown_handler=MagicMock(), config=MagicMock(), anti_bot=None,
        )

    def _uploader_with_limit(self, platform: str, limit: int) -> MagicMock:
        uploader = _make_uploader(platform)
        manifest = MagicMock()
        manifest.extra = {"max_video_duration_seconds": limit} if platform == "x" \
            else {"max_duration_seconds": limit}
        uploader.manifest.return_value = manifest
        return uploader

    @pytest.mark.asyncio
    async def test_duration_limit_x(self, tmp_path):
        """G08: a 200s video must be pre-flight skipped on X (140s cap),
        not uploaded into a guaranteed rejection."""
        video = tmp_path / "long.mp4"
        video.write_bytes(b"v" * 2048)
        service = self._service_with_duration(tmp_path, 200.0)
        uploader = self._uploader_with_limit("x", 140)

        result = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="x", video_id="long1",
        )
        assert result.success is False
        assert "140" in (result.error or "")
        assert result.metadata.get("preflight") is True
        uploader.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_duration_limit_youtube_shorts(self, tmp_path):
        video = tmp_path / "long2.mp4"
        video.write_bytes(b"v" * 2048)
        service = self._service_with_duration(tmp_path, 90.0)
        uploader = self._uploader_with_limit("youtube", 60)

        result = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="youtube", video_id="long2",
        )
        assert result.success is False
        assert "60" in (result.error or "")
        uploader.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_limit_proceeds(self, tmp_path):
        video = tmp_path / "ok.mp4"
        video.write_bytes(b"v" * 2048)
        service = self._service_with_duration(tmp_path, 45.0)
        uploader = self._uploader_with_limit("youtube", 60)

        result = await service.upload_to_platform(
            uploader=uploader, video_path=video, caption="c",
            platform_name="youtube", video_id="ok1",
        )
        assert result.success is True
        uploader.upload.assert_awaited_once()
