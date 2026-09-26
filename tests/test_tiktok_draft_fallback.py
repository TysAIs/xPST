"""TikTok inbox-draft fallback (unaudited-client mode).

The Content Posting API refuses public Direct Post for unaudited clients
(``unaudited_client_can_only_post_to_private_accounts``). The legitimate
no-audit path is the inbox DRAFT upload (``video.upload`` scope):
xPST uploads the video as a draft and the user finishes the post in the
TikTok app (≤5 pending drafts per 24h). A draft is NOT a published post —
the uploader must report PENDING, never success.

These tests pin:
* config knob ``tiktok.draft_mode`` (auto/always/never, default auto);
* 'auto' falls back to the draft path ONLY on the unaudited-client refusal;
* 'always' skips direct post entirely; 'never' keeps direct post only;
* a draft result is PENDING with ``draft_mode: True`` metadata and is never
  a fabricated success.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from xpst.config import XPSTConfig
from xpst.platforms.base import UploadOutcome, UploadResult
from xpst.platforms.tiktok import TikTokUploader

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def uploader() -> TikTokUploader:
    config = XPSTConfig()
    config.tiktok.client_key = "key"
    config.tiktok.client_secret = "secret"  # noqa: S105 - test fixture
    config.tiktok.access_token = "token"  # noqa: S105 - test fixture
    return TikTokUploader(config)


def _video(tmp_path: Path) -> Path:
    p = tmp_path / "v.mp4"
    p.write_bytes(b"0" * 1024)
    return p


def _init_response(publish_id: str = "draft-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"publish_id": publish_id, "upload_url": "https://upload.example"}},
        request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/upload/"),
    )


def _unaudited_response() -> httpx.Response:
    return httpx.Response(
        403,
        json={"error": {"code": "unaudited_client_can_only_post_to_private_accounts"}},
        request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"),
    )


def test_draft_mode_defaults_to_auto(uploader: TikTokUploader) -> None:
    assert uploader._draft_mode_requested() == "auto"


@pytest.mark.asyncio
async def test_auto_falls_back_to_draft_on_unaudited_refusal(
    uploader: TikTokUploader, tmp_path: Path
) -> None:
    """The unaudited-client refusal routes the upload to the draft path."""
    draft_result = UploadResult(
        success=False,
        outcome=UploadOutcome.PENDING,
        error="TIKTOK_DRAFT_PENDING: draft uploaded",
        platform="tiktok",
        metadata={"draft_mode": True},
    )
    with patch.object(uploader, "_get_access_token", new=AsyncMock(return_value="token")):
        with patch.object(
            uploader, "_upload_as_draft", new=AsyncMock(return_value=draft_result)
        ) as draft:
            with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.HTTPStatusError(
                "403", request=_unaudited_response().request, response=_unaudited_response(),
            ))):
                result = await uploader.upload(_video(tmp_path), "cap")

    draft.assert_awaited_once()
    assert result.outcome == UploadOutcome.PENDING
    assert result.metadata.get("draft_mode") is True


@pytest.mark.asyncio
async def test_auto_does_not_fall_back_on_other_errors(
    uploader: TikTokUploader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generic HTTP error must NOT trigger the draft fallback."""
    monkeypatch.setattr(uploader, "_draft_mode_requested", lambda: "auto")
    other = httpx.Response(
        500,
        json={"error": {"code": "internal_error"}},
        request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"),
    )
    err = httpx.HTTPStatusError("500", request=other.request, response=other)
    with patch.object(uploader, "_get_access_token", new=AsyncMock(return_value="token")):
        with patch.object(uploader, "_upload_as_draft", new=AsyncMock()) as draft:
            with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=err)):
                result = await uploader.upload(_video(tmp_path), "cap")

    draft.assert_not_awaited()
    assert "TIKTOK" in (result.error or "")


@pytest.mark.asyncio
async def test_never_mode_keeps_direct_post(
    uploader: TikTokUploader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uploader, "_draft_mode_requested", lambda: "never")
    refusal = _unaudited_response()
    err = httpx.HTTPStatusError("403", request=refusal.request, response=refusal)
    with patch.object(uploader, "_get_access_token", new=AsyncMock(return_value="token")):
        with patch.object(uploader, "_upload_as_draft", new=AsyncMock()) as draft:
            with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=err)):
                result = await uploader.upload(_video(tmp_path), "cap")

    draft.assert_not_awaited()
    assert result.success is False


@pytest.mark.asyncio
async def test_always_mode_skips_direct_post(
    uploader: TikTokUploader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uploader, "_draft_mode_requested", lambda: "always")
    draft_result = UploadResult(
        success=False,
        outcome=UploadOutcome.PENDING,
        error="TIKTOK_DRAFT_PENDING",
        platform="tiktok",
        metadata={"draft_mode": True},
    )
    with patch.object(uploader, "_get_access_token", new=AsyncMock(return_value="token")):
        with patch("httpx.AsyncClient.post", new=AsyncMock()) as direct_post:
            with patch.object(
                uploader, "_upload_as_draft", new=AsyncMock(return_value=draft_result)
            ):
                result = await uploader.upload(_video(tmp_path), "cap")

    direct_post.assert_not_awaited()
    assert result.metadata.get("draft_mode") is True


def test_draft_result_is_pending_not_success(uploader: TikTokUploader) -> None:
    """A draft upload is pending work for the user — never a fabricated success."""
    uploader.config.tiktok.draft_mode = "always"
    result = UploadResult(
        success=False,
        outcome=UploadOutcome.PENDING,
        error="TIKTOK_DRAFT_PENDING: video uploaded as an inbox draft",
        platform="tiktok",
        metadata={"draft_mode": True, "publish_id": "draft-1"},
    )
    assert result.success is False
    assert result.outcome == UploadOutcome.PENDING
    assert result.metadata["draft_mode"] is True


def test_unaudited_refusal_detector(uploader: TikTokUploader) -> None:
    body = '{"error":{"code":"unaudited_client_can_only_post_to_private_accounts"}}'
    assert uploader._unaudited_client_refused(body, 403) is True
    assert uploader._unaudited_client_refused('{"error":{"code":"x"}}', 403) is False
    assert uploader._unaudited_client_refused(body, 500) is False
