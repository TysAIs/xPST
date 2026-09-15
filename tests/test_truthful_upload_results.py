"""Regression tests for truthful upload outcomes.

These tests exercise the provider-to-state boundary rather than treating an API
request acknowledgment as proof that a public post exists.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine
from xpst.platforms.base import PlatformUploader, UploadOutcome, UploadResult, normalize_upload_result


class _Response:
    def __init__(self, data: dict, status_code: int = 200, text: str = "") -> None:
        self._data = data
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


def _httpx_client(responses: list[_Response]) -> MagicMock:
    client = MagicMock()
    queue = list(responses)

    async def next_response(*_args, **_kwargs):
        return queue.pop(0)

    client.post = AsyncMock(side_effect=next_response)
    client.put = AsyncMock(side_effect=next_response)
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=client)
    context_manager.__aexit__ = AsyncMock(return_value=False)
    return context_manager


def _config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.tiktok.enabled = True
    config.tiktok.access_token = "token"
    config.tiktok.sandbox = False
    return config


@pytest.mark.asyncio
async def test_youtube_missing_video_id_is_failure_without_malformed_url(tmp_path: Path) -> None:
    from xpst.platforms.youtube import YouTubeUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    request = MagicMock()
    request.next_chunk.return_value = (None, {})
    service = MagicMock()
    service.videos.return_value.insert.return_value = request

    uploader = YouTubeUploader(_config(tmp_path))
    uploader._service = service
    with patch("googleapiclient.http.MediaFileUpload", return_value=MagicMock()):
        result = await uploader.upload(video, "caption")

    assert result.success is False
    assert result.outcome == UploadOutcome.FAILED
    assert result.post_id is None
    assert result.post_url is None
    assert "video ID" in (result.error or "")
    assert "/shorts/" not in (result.post_url or "")


@pytest.mark.asyncio
async def test_tiktok_processing_response_is_pending_not_success(tmp_path: Path) -> None:
    from xpst.platforms.tiktok import TikTokUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    responses = [
        _Response({"data": {"publish_id": "publish-1", "upload_url": "https://upload.test/video"}}),
        _Response({}),
        _Response({"data": {"status": "PROCESSING_UPLOAD"}}),
    ]
    with patch("httpx.AsyncClient", return_value=_httpx_client(responses)):
        result = await TikTokUploader(_config(tmp_path)).upload(video, "caption")

    assert result.success is False
    assert result.outcome == UploadOutcome.PENDING
    assert result.status == "pending"
    assert result.post_url is None
    assert result.metadata["publish_id"] == "publish-1"
    assert result.metadata["status"] == "PROCESSING_UPLOAD"


@pytest.mark.asyncio
async def test_tiktok_success_without_share_proof_is_failure_without_homepage(tmp_path: Path) -> None:
    from xpst.platforms.tiktok import TikTokUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    responses = [
        _Response({"data": {"publish_id": "publish-1", "upload_url": "https://upload.test/video"}}),
        _Response({}),
        _Response({"data": {"status": "SUCCESS"}}),
    ]
    with patch("httpx.AsyncClient", return_value=_httpx_client(responses)):
        result = await TikTokUploader(_config(tmp_path)).upload(video, "caption")

    assert result.success is False
    assert result.outcome == UploadOutcome.FAILED
    assert result.post_url is None
    assert result.post_id is None
    assert "tiktok.com" not in (result.post_url or "")


@pytest.mark.asyncio
async def test_tiktok_success_with_public_url_is_published(tmp_path: Path) -> None:
    from xpst.platforms.tiktok import TikTokUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    responses = [
        _Response({"data": {"publish_id": "publish-1", "upload_url": "https://upload.test/video"}}),
        _Response({}),
        _Response(
            {
                "data": {
                    "status": "SUCCESS",
                    "publicaly_available_post_url": "https://www.tiktok.com/@creator/video/123",
                }
            }
        ),
    ]
    with patch("httpx.AsyncClient", return_value=_httpx_client(responses)):
        result = await TikTokUploader(_config(tmp_path)).upload(video, "caption")

    assert result.success is True
    assert result.outcome == UploadOutcome.PUBLISHED


@pytest.mark.asyncio
async def test_tiktok_publish_complete_with_public_id_is_published(tmp_path: Path) -> None:
    from xpst.platforms.tiktok import TikTokUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    responses = [
        _Response({"data": {"publish_id": "publish-1", "upload_url": "https://upload.test/video"}}),
        _Response({}),
        _Response({"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [123]}}),
    ]
    with patch("httpx.AsyncClient", return_value=_httpx_client(responses)):
        result = await TikTokUploader(_config(tmp_path)).upload(video, "caption")

    assert result.success is True
    assert result.outcome == UploadOutcome.PUBLISHED
    assert result.post_id == "123"
    assert result.post_url is None


@pytest.mark.asyncio
async def test_tiktok_success_with_malformed_public_url_is_failure(tmp_path: Path) -> None:
    from xpst.platforms.tiktok import TikTokUploader

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    responses = [
        _Response({"data": {"publish_id": "publish-1", "upload_url": "https://upload.test/video"}}),
        _Response({}),
        _Response({"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_url": "not-a-url"}}),
    ]
    with patch("httpx.AsyncClient", return_value=_httpx_client(responses)):
        result = await TikTokUploader(_config(tmp_path)).upload(video, "caption")

    assert result.success is False
    assert result.outcome == UploadOutcome.FAILED
    assert result.post_url is None


def test_normalization_rejects_homepage_and_malformed_urls() -> None:
    homepage = normalize_upload_result(
        UploadResult(
            success=True,
            post_id="publish-1",
            post_url="https://www.tiktok.com/",
            platform="tiktok",
            metadata={"publish_id": "publish-1", "status": "SUCCESS"},
        ),
        "tiktok",
    )
    malformed = normalize_upload_result(
        UploadResult(success=True, post_url="not-a-url", platform="youtube"),
        "youtube",
    )

    for result in (homepage, malformed):
        assert result.success is False
        assert result.outcome == UploadOutcome.FAILED
        assert result.post_url is None
        assert "unverified" in (result.error or "").lower()


def test_normalization_preserves_error_retryability_and_metadata() -> None:
    raw = UploadResult(
        success=False,
        error="TIKTOK_RATE_LIMITED",
        platform="tiktok",
        metadata={"publish_id": "publish-1", "status": "FAIL"},
        retryable=True,
    )

    result = normalize_upload_result(raw, "tiktok")

    assert result is raw
    assert result.retryable is True
    assert result.terminal is False
    assert result.metadata == raw.metadata
    assert result.error == raw.error


@pytest.mark.asyncio
async def test_engine_does_not_mark_unverified_success_as_posted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.youtube.enabled = False
    config.x.enabled = False
    config.instagram.enabled = False
    config.tiktok.enabled = False
    config.threads.enabled = False
    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = "youtube"
    uploader.upload = AsyncMock(
        return_value=UploadResult(
            success=True,
            post_id=None,
            post_url="https://youtube.com/shorts/",
            platform="youtube",
        )
    )
    engine._platforms["youtube"] = uploader

    with (
        patch.object(engine.upload_service, "_encode_for_platform", new_callable=AsyncMock, return_value=video),
        patch("xpst.services.upload_service.verify_media") as verify_media,
    ):
        verify_media.return_value.ok = True
        verify_media.return_value.warnings = []
        verify_media.return_value.errors = []
        verify_media.return_value.to_dict.return_value = {}
        result = await engine.post_manual(video, "caption", ["youtube"])

    upload_result = result.results["youtube"]
    assert upload_result.success is False
    assert result.all_success is False
    assert not engine.state.is_video_posted(result.video_id, "youtube")
    assert engine.state.get_video(result.video_id)["posted_to"] == {}


# ---------------------------------------------------------------------------
# Progress reporting must agree with the upload verdict
#
# A real Instagram attempt logged
#   [ERROR] Instagram upload failed (attempt 1/4): 404 Client Error: Not Found
#   [INFO]  Instagram upload (canary-d085d18f): 100% complete (55.4s total)
# one millisecond apart: the tracker reported success for a failed upload,
# which is what made a failure look like a post.
# ---------------------------------------------------------------------------

def test_progress_tracker_fail_never_reports_100_percent() -> None:
    from xpst.utils import progress as progress_module

    with patch.object(progress_module, "logger") as fake_logger:
        tracker = progress_module.ProgressTracker("Instagram upload (canary)")
        tracker.fail("404 Client Error: Not Found")

    messages = [call.args[0] for call in fake_logger.info.call_args_list]
    errors = [call.args[0] for call in fake_logger.error.call_args_list]

    assert not any("100% complete" in message for message in messages)
    assert any("FAILED" in message for message in errors)
    assert any("404 Client Error" in message for message in errors)


def test_progress_tracker_complete_still_reports_success() -> None:
    from xpst.utils import progress as progress_module

    with patch.object(progress_module, "logger") as fake_logger:
        progress_module.ProgressTracker("YouTube upload (ok)").complete()

    messages = [call.args[0] for call in fake_logger.info.call_args_list]
    assert any("100% complete" in message for message in messages)


async def _run_manual_upload(tmp_path: Path, upload_result: UploadResult):
    """Drive one manual upload through the engine with a scripted uploader."""
    config = _config(tmp_path)
    for platform in ("youtube", "x", "instagram", "tiktok", "threads"):
        getattr(config, platform).enabled = False
    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = "youtube"
    uploader.upload = AsyncMock(return_value=upload_result)
    engine._platforms["youtube"] = uploader

    with (
        patch.object(engine.upload_service, "_encode_for_platform", new_callable=AsyncMock, return_value=video),
        patch("xpst.services.upload_service.verify_media") as verify_media,
        patch("xpst.utils.progress.logger") as fake_logger,
    ):
        verify_media.return_value.ok = True
        verify_media.return_value.warnings = []
        verify_media.return_value.errors = []
        verify_media.return_value.to_dict.return_value = {}
        result = await engine.post_manual(video, "caption", ["youtube"])

    infos = [call.args[0] for call in fake_logger.info.call_args_list]
    errors = [call.args[0] for call in fake_logger.error.call_args_list]
    return result, infos, errors


@pytest.mark.asyncio
async def test_failed_upload_does_not_report_100_percent(tmp_path: Path) -> None:
    result, infos, errors = await _run_manual_upload(
        tmp_path,
        UploadResult(
            success=False,
            error="404 Client Error: Not Found for url: https://i.instagram.com/api/v1/qe/expose/",
            platform="youtube",
        ),
    )

    assert result.results["youtube"].success is False
    assert not any("100% complete" in message for message in infos)
    assert any("FAILED" in message for message in errors)


@pytest.mark.asyncio
async def test_published_upload_reports_100_percent(tmp_path: Path) -> None:
    result, infos, _errors = await _run_manual_upload(
        tmp_path,
        UploadResult(
            success=True,
            post_id="abc123",
            post_url="https://youtube.com/shorts/abc123",
            platform="youtube",
        ),
    )

    assert result.results["youtube"].success is True
    assert any("100% complete" in message for message in infos)
