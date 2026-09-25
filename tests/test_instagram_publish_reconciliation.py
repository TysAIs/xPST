"""A published Instagram Reel must never be lost because of a post-publish error.

instagrapi publishes the Reel and only then calls `qe/expose/` (an experiment
telemetry ping, see instagrapi/mixins/clip.py) before returning the created
Media. When that call 404s the exception escapes `clip_upload` even though the
Reel is live, so the uploader must reconcile against the account instead of
reporting a plain failure: a failure means no post id is recorded, and `delete`
can then never remove the post.

These tests exercise that reconciliation with a scripted instagrapi client and
assert the read-only guarantee (no upload/publish/delete calls).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from xpst.config import XPSTConfig
from xpst.platforms.instagram import InstagramUploader, _normalize_caption

CAPTION = "xPST release verification clip - no user content\n\n#Shorts"

POST_PUBLISH_ERROR = (
    "404 Client Error: Not Found for url: https://i.instagram.com/api/v1/qe/expose/"
)


class _FakeClient:
    """Scripted instagrapi client: records every call it receives."""

    def __init__(self, *, media=None, listing_error=None) -> None:
        self.user_id = "12345678901"
        self.username = "tysn.dev"
        self._media = media if media is not None else []
        self._listing_error = listing_error
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.clip_uploads: int = 0

    def clip_upload(self, *args, **kwargs):  # pragma: no cover - always raises here
        self.clip_uploads += 1
        self.calls.append("clip_upload")
        raise RuntimeError(POST_PUBLISH_ERROR)

    def user_medias(self, user_id, amount=0):
        self.calls.append("user_medias")
        if self._listing_error is not None:
            raise RuntimeError(self._listing_error)
        return list(self._media)

    def media_delete(self, media_id):  # must never be called by reconciliation
        self.deleted.append(str(media_id))
        self.calls.append("media_delete")
        return True


def _media(*, code: str, caption: str, age_seconds: int = 60):
    return SimpleNamespace(
        pk=1234567890,
        code=code,
        caption_text=caption,
        taken_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        media_type=2,
        product_type="clips",
    )


def _uploader(tmp_path: Path, client: _FakeClient) -> InstagramUploader:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    uploader = InstagramUploader(config)
    uploader._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
    return uploader


async def _run(uploader: InstagramUploader, tmp_path: Path, caption: str = CAPTION):
    video = tmp_path / "canary.mp4"
    video.write_bytes(b"not a real video")
    with (
        patch.object(InstagramUploader, "_validate_video", lambda self, path: None),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        return await uploader._upload_instagrapi(video, caption)


@pytest.mark.asyncio
async def test_post_publish_error_is_reconciled_as_a_real_post(tmp_path: Path) -> None:
    client = _FakeClient(media=[_media(code="DdQj3rWCn66", caption=CAPTION)])
    uploader = _uploader(tmp_path, client)

    result = await _run(uploader, tmp_path)

    assert result.success is True, result.error
    assert result.post_id == "1234567890"
    assert result.post_url == "https://www.instagram.com/reel/DdQj3rWCn66/"
    assert result.metadata["reconciled"] is True
    assert "qe/expose" in result.metadata["original_error"]


@pytest.mark.asyncio
async def test_reconciliation_is_read_only(tmp_path: Path) -> None:
    client = _FakeClient(media=[_media(code="DdQj3rWCn66", caption=CAPTION)])
    uploader = _uploader(tmp_path, client)

    await _run(uploader, tmp_path)

    assert client.calls == ["clip_upload", "user_medias"]
    assert client.deleted == []


@pytest.mark.asyncio
async def test_post_publish_error_without_a_matching_reel_is_unconfirmed(tmp_path: Path) -> None:
    other = _media(code="DdPwsoDvVKx", caption="something else entirely")
    client = _FakeClient(media=[other])
    uploader = _uploader(tmp_path, client)

    result = await _run(uploader, tmp_path)

    assert result.success is False
    assert result.error.startswith("IG_UNCONFIRMED")
    assert "check the Instagram profile before retrying" in result.error


@pytest.mark.asyncio
async def test_stale_match_outside_the_window_is_not_reconciled(tmp_path: Path) -> None:
    stale = _media(code="DdQj3rWCn66", caption=CAPTION, age_seconds=60 * 60 * 6)
    client = _FakeClient(media=[stale])
    uploader = _uploader(tmp_path, client)

    result = await _run(uploader, tmp_path)

    assert result.success is False
    assert result.error.startswith("IG_UNCONFIRMED")


@pytest.mark.asyncio
async def test_listing_failure_does_not_claim_success(tmp_path: Path) -> None:
    client = _FakeClient(listing_error="401 Unauthorized")
    uploader = _uploader(tmp_path, client)

    result = await _run(uploader, tmp_path)

    assert result.success is False
    assert result.error.startswith("IG_UNCONFIRMED")


@pytest.mark.asyncio
async def test_unrelated_upload_errors_keep_their_classification(tmp_path: Path) -> None:
    """Reconciliation must not swallow errors that clearly mean 'no post'."""
    client = _FakeClient()
    uploader = _uploader(tmp_path, client)

    def invalid_format(*args, **kwargs):
        raise RuntimeError("video format not supported by codec")

    client.clip_upload = invalid_format  # type: ignore[method-assign]

    result = await _run(uploader, tmp_path)

    assert result.success is False
    assert result.error.startswith("IG_INVALID_FORMAT")
    assert "user_medias" not in client.calls


def test_caption_normalization_ignores_whitespace_and_case() -> None:
    assert _normalize_caption("  Hello\n\nWorld  ") == "hello world"
    assert _normalize_caption(None) == ""
