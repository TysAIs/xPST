"""Carousel correctness: a photo carousel is a carousel — or a clear refusal.

The defect this file pins down (card ``t_0f8505da``): several selected images
were routed into a "video-ish" path, and any destination without a native
carousel implementation stitched the images into one vertical video with ffmpeg
and uploaded *that* — the user asked for a photo carousel and silently got a
video. Three properties are asserted here:

1. an image-only carousel request never touches the video/ffmpeg path;
2. N images + ``carousel`` → one carousel on Instagram with all N items, in
   request order;
3. a destination that cannot publish a carousel says so, by name, instead of
   stitching — the *preflight* names it, the *engine* names it, and the
   *uploader* refuses rather than falling back to the video path.

Fixtures are generated images (no personal photos, no ffmpeg).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.config import XPSTConfig
from xpst.content import ContentRequest, ContentType
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.media.specs import image_rejection_reasons
from xpst.platforms.base import PlatformUploader, UploadResult
from xpst.platforms.instagram import InstagramUploader
from xpst.platforms.x import XUploader
from xpst.services.post_service import PostService

# ---------------------------------------------------------------------------
# Fixtures: real, generated image files (pure Python headers, no ffmpeg)
# ---------------------------------------------------------------------------


def _jpeg_bytes(width: int, height: int) -> bytes:
    """A JPEG with a valid SOI/SOF0/EOI header, carrying ``width`` x ``height``."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + bytes([8])
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes([3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1])
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _png_bytes(width: int, height: int) -> bytes:
    """A PNG header/blocks big enough for the header reader to parse."""
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _jpeg(path: Path, width: int = 1080, height: int = 1080) -> Path:
    return _write(path, _jpeg_bytes(width, height))


def _png(path: Path, width: int = 1080, height: int = 1080) -> Path:
    return _write(path, _png_bytes(width, height))


def _carousel(tmp_path: Path, count: int = 3) -> list[Path]:
    """N distinct, ordered JPEG fixtures (names differ, so order is checkable)."""
    return [_jpeg(tmp_path / f"xpst-carousel-{index}.jpg") for index in range(1, count + 1)]


def _config(tmp_path: Path) -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    for platform in (config.youtube, config.x, config.instagram, config.tiktok, config.threads):
        platform.enabled = False
    # A local Instagram session so the preflight has nothing to object to; no
    # client is ever built from it in these tests.
    config.instagram.username = "fixture-user"
    config.instagram.session_file = str(tmp_path / "ig-session.json")
    Path(config.instagram.session_file).write_text("{}", encoding="utf-8")
    return config


def _instagram_uploader(config: XPSTConfig, *, client: Any | None = None) -> InstagramUploader:
    """An InstagramUploader whose instagrapi client is a mock (never a network call)."""
    config.instagram.auth_mode = "session"
    uploader = InstagramUploader(config)
    mock_client = client if client is not None else MagicMock()
    uploader._get_client = AsyncMock(return_value=mock_client)  # type: ignore[method-assign]
    return uploader


def _album_client(code: str = "CAROUSEL1", pk: str = "99887766") -> MagicMock:
    client = MagicMock()
    media = MagicMock()
    media.code = code
    media.pk = pk
    client.album_upload.return_value = media
    return client


class _NoFfmpegPlease:
    """Patch target that fails the test if the video/ffmpeg path is touched."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the video/ffmpeg path was invoked for an image-only carousel request")


# ---------------------------------------------------------------------------
# (1) No stitch: the default uploader refuses instead of making a video
# ---------------------------------------------------------------------------


class TestNoStitch:
    def test_base_upload_carousel_refuses_and_never_stitches(self, tmp_path: Path) -> None:
        """The base default is a named refusal, not a stitch-to-video fallback."""

        class DummyUploader(PlatformUploader):
            async def upload(self, video_path: Path, caption: str) -> UploadResult:
                raise AssertionError("upload() must not be called for a refused carousel")

            async def check_health(self):  # noqa: ANN201 - test double
                return None

        uploader = DummyUploader(_config(tmp_path))
        with patch("xpst.utils.video.VideoProcessor.__init__", _NoFfmpegPlease):
            result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path), "caption"))

        assert result.success is False
        assert result.retryable is False
        assert result.metadata["content_type"] == "carousel"
        assert "dummy cannot publish carousel posts" in (result.error or "")
        assert "will not stitch" in (result.error or "")

    def test_base_upload_carousel_has_no_stitch_helper(self) -> None:
        """The stitch-and-upload fallback is gone, not merely unused."""
        assert not hasattr(PlatformUploader, "_stitch_and_upload")

    @pytest.mark.parametrize("platform", ["youtube", "tiktok", "threads"])
    def test_inheriting_destinations_refuse_a_photo_carousel(self, tmp_path: Path, platform: str) -> None:
        """YouTube/TikTok/Threads have no carousel path: they say so, by name."""
        config = _config(tmp_path)
        uploader = {
            "youtube": __import__("xpst.platforms.youtube", fromlist=["YouTubeUploader"]).YouTubeUploader,
            "tiktok": __import__("xpst.platforms.tiktok", fromlist=["TikTokUploader"]).TikTokUploader,
            "threads": __import__("xpst.platforms.threads", fromlist=["ThreadsUploader"]).ThreadsUploader,
        }[platform](config)

        with patch("xpst.utils.video.VideoProcessor.__init__", _NoFfmpegPlease):
            result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path), "caption"))

        assert result.success is False
        assert f"{platform} cannot publish carousel posts" in (result.error or "")
        # The refusal points at the destinations that really can.
        assert "instagram" in (result.error or "")


# ---------------------------------------------------------------------------
# (2) An image-only carousel request never invokes the video/ffmpeg path
# ---------------------------------------------------------------------------


class TestImageOnlyCarouselNeverUsesTheVideoPath:
    def test_instagram_carousel_uploads_images_without_ffmpeg(self, tmp_path: Path) -> None:
        """3 JPEGs → one album_upload call, all N in order, no video machinery."""
        config = _config(tmp_path)
        client = _album_client()
        uploader = _instagram_uploader(config, client=client)
        uploader.upload = AsyncMock(side_effect=AssertionError("the video path must not be used"))

        with patch("xpst.utils.video.VideoProcessor.__init__", _NoFfmpegPlease):
            result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 3), "fixture caption"))

        assert result.success is True
        assert result.metadata["carousel_items"] == 3
        assert result.metadata["item_order"] == [f"xpst-carousel-{index}.jpg" for index in (1, 2, 3)]
        client.album_upload.assert_called_once()
        uploaded = client.album_upload.call_args.args[0]
        assert [Path(item).name for item in uploaded] == [f"xpst-carousel-{index}.jpg" for index in (1, 2, 3)]

    @pytest.mark.asyncio
    async def test_engine_carousel_never_encodes_or_stitches(self, tmp_path: Path) -> None:
        """The engine chokepoint goes straight to the album upload."""
        engine = CrossPostEngine(_config(tmp_path))
        engine.upload_service.anti_bot = None
        client = _album_client()
        engine._platforms["instagram"] = _instagram_uploader(engine.config, client=client)

        with (
            patch("xpst.utils.video.VideoProcessor.__init__", _NoFfmpegPlease),
            patch(
                "xpst.services.upload_service.UploadService._encode_for_platform",
                AsyncMock(side_effect=AssertionError("no encode for an image-only carousel")),
            ),
        ):
            result = await engine.post_manual_carousel(_carousel(tmp_path, 3), "caption", ["instagram"])

        upload = result.results["instagram"]
        assert upload.success is True
        assert upload.metadata["carousel_items"] == 3
        client.album_upload.assert_called_once()

    def test_post_service_routes_an_image_carousel_to_the_carousel_path(self, tmp_path: Path) -> None:
        """PostService (the CLI/MCP/HTTP entry point) picks the carousel path, not video."""
        config = _config(tmp_path)
        config.instagram.enabled = True
        config.instagram.auth_mode = "session"
        fake_engine = MagicMock()
        fake_engine.post_manual_carousel = AsyncMock(
            return_value=CrossPostResult(
                video_id="carousel_fixture",
                caption="caption",
                results={
                    "instagram": UploadResult(
                        success=True,
                        post_id="1",
                        post_url="https://www.instagram.com/p/CAROUSEL1/",
                        platform="instagram",
                    )
                },
            )
        )
        fake_engine.post_manual = AsyncMock(side_effect=AssertionError("the video path must not be used"))
        service = PostService(config, engine_factory=lambda _config: fake_engine)
        request = ContentRequest(
            content_type=ContentType.CAROUSEL,
            media=tuple(str(path) for path in _carousel(tmp_path, 3)),
            text="caption",
            platforms=("instagram",),
        )

        with patch("xpst.utils.video.VideoProcessor.__init__", _NoFfmpegPlease):
            envelope = service.execute(request)

        assert envelope["content_type"] == "carousel"
        assert envelope["uploaded_count"] == 1
        fake_engine.post_manual_carousel.assert_awaited_once()
        assert fake_engine.post_manual.await_count == 0
        passed = fake_engine.post_manual_carousel.call_args.args[0]
        assert [Path(item).name for item in passed] == [f"xpst-carousel-{index}.jpg" for index in (1, 2, 3)]


# ---------------------------------------------------------------------------
# (3) A destination that cannot do a carousel says so, by name
# ---------------------------------------------------------------------------


class TestNamedRefusals:
    def test_preflight_names_the_destination_and_says_carousel(self, tmp_path: Path) -> None:
        """The preflight blocker names the destination and the content type."""
        config = _config(tmp_path)
        config.youtube.enabled = True
        service = PostService(config)
        verdict = service.preflight(
            ContentRequest(
                content_type=ContentType.CAROUSEL,
                media=tuple(str(path) for path in _carousel(tmp_path, 3)),
                text="caption",
                platforms=("youtube",),
            )
        )

        assert verdict["ready"] is False
        assert any("youtube" in blocker and "carousel" in blocker for blocker in verdict["blockers"])

    def test_preflight_accepts_an_instagram_photo_carousel(self, tmp_path: Path) -> None:
        """Instagram can publish it, so the preflight is ready and names no blocker."""
        config = _config(tmp_path)
        config.instagram.enabled = True
        config.instagram.auth_mode = "session"
        service = PostService(config)
        verdict = service.preflight(
            ContentRequest(
                content_type=ContentType.CAROUSEL,
                media=tuple(str(path) for path in _carousel(tmp_path, 3)),
                text="caption",
                platforms=("instagram",),
            )
        )

        assert verdict["ready"] is True, verdict["blockers"]

    @pytest.mark.asyncio
    async def test_engine_refuses_by_name_instead_of_uploading(self, tmp_path: Path) -> None:
        """A caller that skipped the preflight (CLI/MCP) still gets the named refusal."""
        engine = CrossPostEngine(_config(tmp_path))
        engine.upload_service.anti_bot = None
        mock_uploader = MagicMock(spec=PlatformUploader)
        mock_uploader.platform_name = "youtube"
        mock_uploader.upload_carousel = AsyncMock(
            side_effect=AssertionError("the uploader must not be reached for an unsupported carousel")
        )
        engine._platforms["youtube"] = mock_uploader

        result = await engine.post_manual_carousel(_carousel(tmp_path, 3), "caption", ["youtube"])

        upload = result.results["youtube"]
        assert upload.success is False
        assert upload.retryable is False
        assert "youtube does not support carousel posts" in (upload.error or "")
        assert "will not stitch" in (upload.error or "")
        mock_uploader.upload_carousel.assert_not_called()


# ---------------------------------------------------------------------------
# (4) Instagram: all N items, in order, and refusals that never truncate
# ---------------------------------------------------------------------------


class TestInstagramCarouselContract:
    def test_refuses_fewer_than_two_items_without_a_video_fallback(self, tmp_path: Path) -> None:
        client = _album_client()
        uploader = _instagram_uploader(_config(tmp_path), client=client)
        uploader.upload = AsyncMock(side_effect=AssertionError("single item must not become a Reel"))

        result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 1), "caption"))

        assert result.success is False
        assert "IG_CAROUSEL_NEEDS_TWO" in (result.error or "")
        client.album_upload.assert_not_called()
        uploader.upload.assert_not_called()

    def test_refuses_more_than_the_limit_instead_of_truncating(self, tmp_path: Path) -> None:
        client = _album_client()
        uploader = _instagram_uploader(_config(tmp_path), client=client)

        result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 11), "caption"))

        assert result.success is False
        assert "IG_CAROUSEL_TOO_MANY_ITEMS" in (result.error or "")
        assert str(InstagramUploader.MAX_CAROUSEL_ITEMS) in (result.error or "")
        client.album_upload.assert_not_called()

    def test_manifest_limit_is_the_class_limit(self, tmp_path: Path) -> None:
        uploader = _instagram_uploader(_config(tmp_path))
        assert uploader.manifest.extra["max_carousel_items"] == InstagramUploader.MAX_CAROUSEL_ITEMS

    def test_refuses_an_image_item_in_the_preflight_wording(self, tmp_path: Path) -> None:
        """A PNG carousel item is refused, in the preflight's own words."""
        client = _album_client()
        uploader = _instagram_uploader(_config(tmp_path), client=client)
        items = [_jpeg(tmp_path / "one.jpg"), _png(tmp_path / "two.png"), _jpeg(tmp_path / "three.jpg")]
        expected = image_rejection_reasons(items[1], "instagram")
        assert expected, "fixture must be rejectable, otherwise this test proves nothing"

        result = asyncio.run(uploader.upload_carousel(items, "caption"))

        assert result.success is False
        assert "IG_CAROUSEL_ITEM_REJECTED" in (result.error or "")
        for reason in expected:
            assert reason in (result.error or "")
        client.album_upload.assert_not_called()

    def test_refuses_the_graph_api_path_by_name(self, tmp_path: Path) -> None:
        """graph_api has no carousel implementation: refused, not attempted."""
        config = _config(tmp_path)
        config.instagram.auth_mode = "graph_api"
        uploader = InstagramUploader(config)
        uploader._get_client = AsyncMock(side_effect=AssertionError("client must not be built"))

        result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 3), "caption"))

        assert result.success is False
        assert "IG_GRAPH_API_CAROUSEL_UNSUPPORTED" in (result.error or "")
        assert "session" in (result.error or "")

    def test_a_carousel_post_reports_the_published_order(self, tmp_path: Path) -> None:
        client = _album_client(code="ORDERED9")
        uploader = _instagram_uploader(_config(tmp_path), client=client)

        result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 4), "caption"))

        assert result.post_url == "https://www.instagram.com/p/ORDERED9/"
        assert result.metadata["item_order"] == [f"xpst-carousel-{index}.jpg" for index in (1, 2, 3, 4)]


# ---------------------------------------------------------------------------
# (5) X: the thread keeps each item's modality and never falls back to video
# ---------------------------------------------------------------------------


class TestXThreadContract:
    def _uploader(self, config: XPSTConfig, *, client: Any | None = None) -> XUploader:
        config.x.auth_mode = "cookies"
        uploader = XUploader(config)
        uploader._get_client = AsyncMock(return_value=client if client is not None else MagicMock())
        return uploader

    def test_refuses_a_single_item_without_a_video_fallback(self, tmp_path: Path) -> None:
        uploader = self._uploader(_config(tmp_path))
        uploader.upload = AsyncMock(side_effect=AssertionError("single item must not become a video post"))

        result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 1), "caption"))

        assert result.success is False
        assert "X_THREAD_NEEDS_TWO" in (result.error or "")
        uploader.upload.assert_not_called()

    def test_refuses_an_image_item_outside_x_image_contract(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.upload_media = AsyncMock(return_value="media_1")
        uploader = self._uploader(_config(tmp_path), client=client)
        tall = _jpeg(tmp_path / "tall.jpg", 200, 2000)  # 1:10 — outside X's 1:3–3:1
        items = [_jpeg(tmp_path / "one.jpg"), tall]
        expected = image_rejection_reasons(tall, "x")
        assert expected

        result = asyncio.run(uploader.upload_carousel(items, "caption"))

        assert result.success is False
        assert "X_THREAD_ITEM_REJECTED" in (result.error or "")
        client.upload_media.assert_not_called()

    def test_refuses_the_api_v2_path_by_name(self, tmp_path: Path) -> None:
        """api_v2 has no thread implementation: refused by name, not attempted."""
        config = _config(tmp_path)
        config.x.auth_mode = "api_v2"
        uploader = XUploader(config)
        uploader._get_client = AsyncMock(side_effect=AssertionError("client must not be built"))

        result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 3), "caption"))

        assert result.success is False
        assert "X_THREAD_API_V2_UNSUPPORTED" in (result.error or "")
        assert "cookies" in (result.error or "")

    def test_thread_keeps_item_order(self, tmp_path: Path) -> None:
        client = MagicMock()
        tweets = [MagicMock(id=str(100 + index)) for index in range(3)]
        client.create_tweet = AsyncMock(side_effect=tweets)
        client.upload_media = AsyncMock(side_effect=["m1", "m2", "m3"])
        uploader = self._uploader(_config(tmp_path), client=client)

        result = asyncio.run(uploader.upload_carousel(_carousel(tmp_path, 3), "caption"))

        assert result.success is True
        assert result.metadata["item_order"] == [f"xpst-carousel-{index}.jpg" for index in (1, 2, 3)]
        assert result.metadata["tweet_ids"] == ["100", "101", "102"]
        sent = [Path(call.args[0]).name for call in client.upload_media.await_args_list]
        assert sent == ["xpst-carousel-1.jpg", "xpst-carousel-2.jpg", "xpst-carousel-3.jpg"]
