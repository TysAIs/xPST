"""Duplicate-post prevention for the manual posting path.

``check_and_post`` checks ``state.is_video_posted(video_id, platform)`` before
uploading, but ``post_manual`` (the ``xpst post`` / agent path) did not: running
the same command twice with the same file posted twice to the same platform.
These tests pin the guarantee that a repeat call is a no-op that reports
``already_posted`` instead of creating a duplicate post.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from xpst.engine import CrossPostEngine

from tests.test_engine import _make_config, _make_mock_uploader


def _engine_with_platform(tmp_path: Path, platform: str = "youtube"):
    config = _make_config(tmp_path)
    (Path(config.video.download_dir)).mkdir(parents=True, exist_ok=True)
    engine = CrossPostEngine(config)
    engine.upload_service.anti_bot = None
    uploader = _make_mock_uploader(platform, success=True)
    engine._platforms[platform] = uploader
    return engine, uploader


async def _post(engine, video_path: Path, caption: str, platform: str):
    with patch.object(
        engine.upload_service,
        "_encode_for_platform",
        new_callable=AsyncMock,
        return_value=video_path,
    ):
        return await engine.post_manual(video_path, caption, [platform])


class TestManualPostDuplicatePrevention:
    @pytest.mark.asyncio
    async def test_second_manual_post_to_same_platform_is_a_noop(self, tmp_path):
        engine, uploader = _engine_with_platform(tmp_path)
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"fake video data")

        first = await _post(engine, video_path, "Same caption", "youtube")
        assert first.results["youtube"].success is True
        assert uploader.upload.await_count == 1

        second = await _post(engine, video_path, "Same caption", "youtube")

        assert uploader.upload.await_count == 1, "duplicate post was uploaded again"
        second_result = second.results["youtube"]
        assert second_result.success is True
        assert second_result.metadata.get("already_posted") is True

    @pytest.mark.asyncio
    async def test_state_records_the_post_so_repeat_calls_can_be_deduped(self, tmp_path):
        engine, _ = _engine_with_platform(tmp_path)
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"fake video data")

        await _post(engine, video_path, "Same caption", "youtube")

        from xpst.utils.content_hash import compute_content_hash

        video_id = (
            f"{video_path.stem}-"
            f"{compute_content_hash(file_path=video_path, filename=video_path.name)[:8]}"
        )
        assert engine.state.is_video_posted(video_id, "youtube") is True

    @pytest.mark.asyncio
    async def test_different_platform_still_posts_after_dedupe(self, tmp_path):
        """Deduping one platform must not block a different destination."""
        engine, yt = _engine_with_platform(tmp_path, "youtube")
        x_uploader = _make_mock_uploader("x", success=True)
        engine._platforms["x"] = x_uploader
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"fake video data")

        await _post(engine, video_path, "Same caption", "youtube")
        second = await _post(engine, video_path, "Same caption", "x")

        assert x_uploader.upload.await_count == 1
        assert second.results["x"].success is True
