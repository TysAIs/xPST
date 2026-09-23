"""Regression tests for two post/delete CLI-path bugs found by a real E2E post.

1. ``xpst post --visibility`` must reach YouTube's ``status.privacyStatus``
   (it was hardcoded to ``public``), and an invalid value must be a clear CLI
   error, not a silent fallback.
2. ``xpst delete`` must accept a platform-side post id or a full post URL, not
   only xPST's internal video id (real repro: ``delete RZ6i-0HM5dM`` returned
   ``unsupported`` because lookup matched only the internal id).

Nothing here posts or deletes from a real account: every platform client is a
fake.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from xpst.cli import main
from xpst.config import XPSTConfig
from xpst.platforms.base import DeleteOutcome, DeleteResult, UploadResult

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> XPSTConfig:
    """Minimal XPSTConfig isolated to ``tmp_path`` with YouTube enabled."""
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    config.youtube.enabled = True
    config.tiktok.enabled = True
    return config


def _extract_json(output: str):
    """Parse the JSON payload from CLI output that may have log lines prepended."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise AssertionError(f"no JSON payload in CLI output:\n{output[:800]}")


# ---------------------------------------------------------------------------
# Bug 1 — post visibility
# ---------------------------------------------------------------------------

VIDEO_ID = "RZ6i-0HM5dM"


def _fake_youtube_service(response: dict) -> MagicMock:
    service = MagicMock()
    request = MagicMock()
    request.next_chunk.return_value = (None, response)
    service.videos.return_value.insert.return_value = request
    return service


@pytest.mark.asyncio
async def test_youtube_upload_honours_unlisted_visibility(tmp_path: Path) -> None:
    """`upload(..., visibility="unlisted")` must set privacyStatus=unlisted."""
    from xpst.platforms.youtube import YouTubeUploader

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    uploader = YouTubeUploader(_config(tmp_path))
    service = _fake_youtube_service({"id": VIDEO_ID})
    uploader._service = service

    with patch("googleapiclient.http.MediaFileUpload", return_value=MagicMock()):
        result = await uploader.upload(video, "hello", visibility="unlisted")

    assert result.success is True
    body = service.videos.return_value.insert.call_args.kwargs["body"]
    assert body["status"]["privacyStatus"] == "unlisted"


@pytest.mark.asyncio
async def test_youtube_upload_default_stays_public(tmp_path: Path) -> None:
    """Omitting visibility must keep the historical public default."""
    from xpst.platforms.youtube import YouTubeUploader

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    uploader = YouTubeUploader(_config(tmp_path))
    service = _fake_youtube_service({"id": VIDEO_ID})
    uploader._service = service

    with patch("googleapiclient.http.MediaFileUpload", return_value=MagicMock()):
        result = await uploader.upload(video, "hello")

    assert result.success is True
    body = service.videos.return_value.insert.call_args.kwargs["body"]
    assert body["status"]["privacyStatus"] == "public"


@pytest.mark.asyncio
async def test_youtube_upload_rejects_invalid_visibility(tmp_path: Path) -> None:
    """An out-of-contract value must be a truthful failure, never a fallback."""
    from xpst.platforms.youtube import YouTubeUploader

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    uploader = YouTubeUploader(_config(tmp_path))

    result = await uploader.upload(video, "hello", visibility="bogus")

    assert result.success is False
    assert "Invalid YouTube visibility" in (result.error or "")


@pytest.mark.asyncio
async def test_engine_post_manual_forwards_visibility(tmp_path: Path) -> None:
    """`CrossPostEngine.post_manual(..., visibility=...)` must reach the upload service."""
    from xpst.engine import CrossPostEngine

    engine = CrossPostEngine(_config(tmp_path))
    captured: dict = {}

    async def fake_upload_to_platform(**kwargs):
        captured.update(kwargs)
        return UploadResult(success=True, platform=kwargs["platform_name"])

    engine.upload_service.upload_to_platform = fake_upload_to_platform
    engine._platforms["youtube"] = MagicMock()

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    await engine.post_manual(video, "hello", ["youtube"], visibility="private")

    assert captured.get("visibility") == "private"


def test_upload_service_only_forwards_visibility_to_accepting_uploaders(tmp_path: Path) -> None:
    """The shared service forwards visibility only to uploaders that accept it."""
    from xpst.services.upload_service import UploadService

    class _Accepts:
        async def upload(self, video_path, caption, *, visibility=None):  # noqa: ANN001
            return None

    class _Rejects:
        async def upload(self, video_path, caption):  # noqa: ANN001
            return None

    assert UploadService._accepts_visibility(_Accepts()) is True
    assert UploadService._accepts_visibility(_Rejects()) is False


def test_cli_rejects_invalid_visibility(tmp_path: Path) -> None:
    """An unknown --visibility value must be a clear CLI error (exit 2)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["post", "-v", str(video), "-c", "hello", "--visibility", "bogus", "--dry-run", "--json"],
    )
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid choice" in result.output.lower()


def test_cli_dry_run_reports_requested_visibility(tmp_path: Path) -> None:
    """The dry run must echo the visibility that a real post would use."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "post", "-v", str(video), "-c", "hello",
            "--visibility", "unlisted", "--dry-run", "--json",
        ],
    )
    assert result.exit_code == 0, result.output[:800]
    payload = _extract_json(result.output)
    assert payload["dry_run"] is True
    assert payload["visibility"] == "unlisted"


# ---------------------------------------------------------------------------
# Bug 2 — delete by platform-side id or post URL
# ---------------------------------------------------------------------------

INTERNAL_ID = "e2e-test-b122a6e7"


def _seeded_engine(tmp_path: Path):
    from xpst.engine import CrossPostEngine

    engine = CrossPostEngine(_config(tmp_path))
    mock = MagicMock()
    mock.delete = AsyncMock(
        return_value=DeleteResult(DeleteOutcome.SOFT_HIDDEN, "youtube", VIDEO_ID)
    )
    engine._platforms["youtube"] = mock
    engine.state.mark_video_posted(
        INTERNAL_ID,
        "youtube",
        post_id=VIDEO_ID,
        post_url=f"https://youtube.com/shorts/{VIDEO_ID}",
    )
    return engine, mock


@pytest.mark.asyncio
async def test_delete_accepts_platform_post_id(tmp_path: Path) -> None:
    """`delete RZ6i-0HM5dM` must resolve to the internal record and unpublish."""
    engine, mock = _seeded_engine(tmp_path)

    result = await engine.delete_post(VIDEO_ID, "youtube", soft=True, visibility="private")

    assert result.outcome == DeleteOutcome.SOFT_HIDDEN
    mock.delete.assert_awaited_once_with(VIDEO_ID, soft=True, visibility="private")
    # The internal record's visibility was updated — proof it resolved the right row.
    entry = engine.state.get_post_data(INTERNAL_ID, "youtube")
    assert entry is not None and entry.get("visibility") == "private"


@pytest.mark.asyncio
async def test_delete_accepts_full_post_url(tmp_path: Path) -> None:
    """`delete https://youtube.com/shorts/RZ6i-0HM5dM` must extract the id and resolve."""
    engine, mock = _seeded_engine(tmp_path)

    result = await engine.delete_post(
        f"https://youtube.com/shorts/{VIDEO_ID}", "youtube", soft=True
    )

    assert result.outcome == DeleteOutcome.SOFT_HIDDEN
    mock.delete.assert_awaited_once_with(VIDEO_ID, soft=True, visibility=None)


@pytest.mark.asyncio
async def test_delete_unknown_ref_stays_truthful_unsupported(tmp_path: Path) -> None:
    """An unresolvable id/URL must keep a truthful not-found outcome, not fake success."""
    engine, mock = _seeded_engine(tmp_path)

    result = await engine.delete_post("nonexistent-id", "youtube", soft=True)

    assert result.outcome == DeleteOutcome.UNSUPPORTED
    mock.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# post_refs — pure URL/id normalization (no I/O)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"https://youtube.com/shorts/{VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
        (f"https://youtu.be/{VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/embed/{VIDEO_ID}", VIDEO_ID),
        (VIDEO_ID, VIDEO_ID),
        (f"  {VIDEO_ID}  ", VIDEO_ID),
        ("", None),
        ("   ", None),
        ("https://example.com/nothing", None),
    ],
)
def test_post_refs_extract_post_id(ref: str, expected: str | None) -> None:
    from xpst.utils.post_refs import extract_post_id

    assert extract_post_id(ref) == expected
