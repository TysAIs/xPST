"""Single-image posts: Instagram feed photos and X image posts.

Before this, images had no publish path anywhere: ``PlatformSpec`` declared no
destination for ``image``, ``upload_image`` did not exist, and the Instagram
adapter shipped a REELS-only Graph path while X shipped chunked video upload
(``xpst-content-model-matrix.md`` section 1). A JPEG therefore reached an
adapter that would fail it, or was refused only after an upload attempt.

Three contracts are pinned here.

1. **The destination's own numbers are enforced before any upload call.** The
   needs of each destination (Instagram: JPEG only, ≤ 8 MB, aspect 4:5–1.91:1;
   X: JPG/PNG/WEBP, ≤ 5 MB, aspect 1:3–3:1) live in ``xpst.media.specs`` and are
   read by both the preflight and the adapter, so an image a platform would
   reject is refused locally, in the platform's own words, with the client never
   touched.
2. **Capability and code agree.** A destination declares the image modality only
   while it really overrides ``upload_image`` — the trap from card A2 (a surface
   offering what the code hard-rejects) cannot reappear on this path.
3. **The engine has one route for it.** ``ContentType.IMAGE`` dispatches to
   ``post_manual_image`` through the typed request contract, and a destination
   without the capability reports a destination-named failure instead of
   publishing the file as a video.

No real photo is ever used here: every fixture is a generated still image.
"""

from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.config import XPSTConfig
from xpst.content import ContentType
from xpst.media.image_header import read_image_dimensions
from xpst.media.specs import PLATFORM_SPECS, image_rejection_reasons, verify_media
from xpst.platforms.base import PlatformUploader, UploadResult

# ---------------------------------------------------------------------------
# Fixtures: real, generated image files (no personal photos, no ffmpeg needed)
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
    """A genuinely valid PNG of the requested size (CRC-checked chunks)."""

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


def _jpeg(path: Path, width: int, height: int) -> Path:
    return _write(path, _jpeg_bytes(width, height))


def _png(path: Path, width: int, height: int) -> Path:
    return _write(path, _png_bytes(width, height))


def _oversized(path: Path, width: int, height: int, size_mb: float) -> Path:
    """A real JPEG header padded past a destination's byte ceiling."""
    payload = _jpeg_bytes(width, height)
    return _write(path, payload + b"\x00" * (int(size_mb * 1024 * 1024) + 1 - len(payload)))


# ---------------------------------------------------------------------------
# Stubbed platform clients
# ---------------------------------------------------------------------------


class _FakeInstagrapiClient:
    """Scripted instagrapi client: records every call it receives."""

    def __init__(self, *, media_id: int = 987654321, code: str = "IMGp0st") -> None:
        self.user_id = "12345678901"
        self.calls: list[tuple[Any, ...]] = []
        self._media_id = media_id
        self._code = code

    def photo_upload(self, path: Any, caption: str = "") -> Any:
        self.calls.append(("photo_upload", str(path), caption))
        return SimpleNamespace(pk=self._media_id, code=self._code)


class _FakeTwikitClient:
    """Scripted twikit client for the cookie-session path."""

    def __init__(self, *, tweet_id: str = "1900000000000000001") -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._tweet_id = tweet_id

    async def upload_media(self, source: Any, **kwargs: Any) -> str:
        self.calls.append(("upload_media", str(source), kwargs))
        return "1145033854441414656"

    async def create_tweet(self, **kwargs: Any) -> Any:
        self.calls.append(("create_tweet", kwargs))
        return SimpleNamespace(id=self._tweet_id)


def _instagram_config(tmp_path: Path, *, auth_mode: str = "session") -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.instagram.auth_mode = auth_mode
    return config


def _x_config(tmp_path: Path, *, auth_mode: str = "cookies") -> XPSTConfig:
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.x.auth_mode = auth_mode
    config.x.api_key = "key"
    config.x.api_secret = "secret"
    config.x.access_token = "token"
    config.x.access_token_secret = "token-secret"
    return config


def _instagram_uploader(tmp_path: Path, client: _FakeInstagrapiClient | None = None, **kwargs: Any):
    from xpst.platforms.instagram import InstagramUploader

    uploader = InstagramUploader(_instagram_config(tmp_path, **kwargs))
    if client is not None:
        uploader._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
    return uploader


def _x_uploader(tmp_path: Path, client: _FakeTwikitClient | None = None, **kwargs: Any):
    from xpst.platforms.x import XUploader

    uploader = XUploader(_x_config(tmp_path, **kwargs))
    if client is not None:
        uploader._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
    return uploader


async def _post_image(uploader: Any, image_path: Path, caption: str) -> UploadResult:
    """Call ``upload_image`` without the adapters' anti-ban delays."""
    with patch("asyncio.sleep", new=AsyncMock()):
        return await uploader.upload_image(image_path, caption)


# ---------------------------------------------------------------------------
# (1) Instagram: a feed photo, and every refusal before the client is touched
# ---------------------------------------------------------------------------


class TestInstagramImagePost:
    @pytest.mark.asyncio
    async def test_a_jpeg_publishes_as_a_feed_photo(self, tmp_path: Path) -> None:
        client = _FakeInstagrapiClient()
        uploader = _instagram_uploader(tmp_path, client)
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        result = await _post_image(uploader, photo, "xPST image post fixture")

        assert result.success is True, result.error
        assert result.post_id == "987654321"
        assert result.post_url == "https://www.instagram.com/p/IMGp0st/"
        assert result.metadata["content_type"] == "image"
        assert result.metadata["auth_mode"] == "session"
        assert [call[0] for call in client.calls] == ["photo_upload"]
        assert client.calls[0][1] == str(photo), "the local file is what gets uploaded"

    @pytest.mark.asyncio
    async def test_a_png_is_refused_before_the_client_is_touched(self, tmp_path: Path) -> None:
        client = _FakeInstagrapiClient()
        uploader = _instagram_uploader(tmp_path, client)
        photo = _png(tmp_path / "fixture.png", 1080, 1080)

        result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and result.error.startswith("IG_IMAGE_REJECTED")
        assert "Instagram" in result.error
        assert ".png" in result.error and ".jpg" in result.error
        assert result.retryable is False
        assert client.calls == [], "a rejected image must never reach Instagram"

    @pytest.mark.asyncio
    async def test_a_wrong_aspect_ratio_is_refused_with_the_destination_named(self, tmp_path: Path) -> None:
        client = _FakeInstagrapiClient()
        uploader = _instagram_uploader(tmp_path, client)
        photo = _jpeg(tmp_path / "wide.jpg", 3000, 1000)  # 3:1, Instagram stops at 1.91:1

        result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and result.error.startswith("IG_IMAGE_REJECTED")
        assert "Instagram" in result.error
        assert "3000x1000" in result.error
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_an_oversized_image_is_refused_by_size(self, tmp_path: Path) -> None:
        client = _FakeInstagrapiClient()
        uploader = _instagram_uploader(tmp_path, client)
        photo = _oversized(tmp_path / "big.jpg", 1080, 1080, size_mb=9)

        result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and "8 MB" in result.error
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_a_post_publish_error_is_reconciled_not_double_posted(self, tmp_path: Path) -> None:
        """A photo that is live but returned an error must be recorded, not retried."""
        caption = "xPST image fixture"
        client = _FakeInstagrapiClient()
        client.photo_upload = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("404 Client Error for url: https://i.instagram.com/api/v1/qe/expose/")
        )
        client.user_medias = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                SimpleNamespace(
                    pk=555,
                    code="DdQj3rWCn66",
                    caption_text=caption,
                    taken_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                )
            ]
        )
        uploader = _instagram_uploader(tmp_path, client)
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        result = await _post_image(uploader, photo, caption)

        assert result.success is True, result.error
        assert result.post_url == "https://www.instagram.com/p/DdQj3rWCn66/"
        assert result.metadata["reconciled"] is True
        assert client.photo_upload.call_count == 1, "reconciliation must never upload again"

    @pytest.mark.asyncio
    async def test_graph_api_refuses_a_local_file_instead_of_sending_a_broken_request(self, tmp_path: Path) -> None:
        """The official API publishes from a public URL; saying so is the honest answer."""
        uploader = _instagram_uploader(tmp_path, auth_mode="graph_api")
        uploader.config.instagram.graph_access_token = "token"
        uploader.config.instagram.graph_ig_user_id = "1"
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        with patch("httpx.AsyncClient", side_effect=AssertionError("no HTTP call may be made")):
            result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and result.error.startswith("IG_GRAPH_API_IMAGE_NEEDS_URL")
        assert "public image URL" in result.error
        assert "session" in result.error, "the message must say which mode can post a local file"

    @pytest.mark.asyncio
    async def test_graph_api_without_credentials_names_what_is_missing(self, tmp_path: Path) -> None:
        uploader = _instagram_uploader(tmp_path, auth_mode="graph_api")
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        with patch("httpx.AsyncClient", side_effect=AssertionError("no HTTP call may be made")):
            result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and result.error.startswith("IG_GRAPH_API_NOT_CONFIGURED")
        assert result.retryable is False


# ---------------------------------------------------------------------------
# (2) X: a single image post on both auth paths
# ---------------------------------------------------------------------------


class TestXImagePost:
    @pytest.mark.asyncio
    async def test_an_image_publishes_through_the_cookie_session(self, tmp_path: Path) -> None:
        client = _FakeTwikitClient()
        uploader = _x_uploader(tmp_path, client)
        photo = _jpeg(tmp_path / "fixture.jpg", 1600, 1200)

        result = await _post_image(uploader, photo, "xPST image post fixture")

        assert result.success is True, result.error
        assert result.post_id == "1900000000000000001"
        assert result.post_url == "https://x.com/i/status/1900000000000000001"
        assert result.metadata["content_type"] == "image"
        assert [call[0] for call in client.calls] == ["upload_media", "create_tweet"]
        assert client.calls[1][1]["text"] == "xPST image post fixture"
        assert client.calls[1][1]["media_ids"] == ["1145033854441414656"]

    @pytest.mark.asyncio
    async def test_a_webp_is_accepted_by_x(self, tmp_path: Path) -> None:
        """X takes JPG/PNG/WEBP — the container set is per destination, not global."""
        client = _FakeTwikitClient()
        uploader = _x_uploader(tmp_path, client)
        photo = _write(
            tmp_path / "fixture.webp",
            b"RIFF"
            + (22).to_bytes(4, "little")
            + b"WEBP"
            + b"VP8X"
            + (10).to_bytes(4, "little")
            + b"\x00\x00\x00\x00"
            + (1023).to_bytes(3, "little")
            + (767).to_bytes(3, "little"),
        )

        report = verify_media(photo, "x", check_loudness=False)
        assert report.ok, [check.detail for check in report.errors]
        assert read_image_dimensions(photo) == (1024, 768)

        result = await _post_image(uploader, photo, "webp caption")
        assert result.success is True, result.error

    @pytest.mark.asyncio
    async def test_a_gif_is_refused_before_the_client_is_touched(self, tmp_path: Path) -> None:
        client = _FakeTwikitClient()
        uploader = _x_uploader(tmp_path, client)
        photo = _write(tmp_path / "fixture.gif", b"GIF89a" + (4).to_bytes(2, "little") * 2 + b"\x00" * 8)

        result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and result.error.startswith("X_IMAGE_REJECTED")
        assert "X (Twitter)" in result.error
        assert ".gif" in result.error
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_a_wrong_aspect_ratio_is_refused_with_the_destination_named(self, tmp_path: Path) -> None:
        client = _FakeTwikitClient()
        uploader = _x_uploader(tmp_path, client)
        photo = _jpeg(tmp_path / "tall.jpg", 400, 4000)  # 1:10, X stops at 1:3

        result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and "X (Twitter)" in result.error
        assert "400x4000" in result.error
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_an_oversized_image_is_refused_by_size(self, tmp_path: Path) -> None:
        client = _FakeTwikitClient()
        uploader = _x_uploader(tmp_path, client)
        photo = _oversized(tmp_path / "big.jpg", 1600, 1200, size_mb=6)

        result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and "5 MB" in result.error
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_the_official_api_path_publishes_an_image(self, tmp_path: Path) -> None:
        """v1.1 media upload (one segment for a still) + v2 tweet creation."""
        uploader = _x_uploader(tmp_path, auth_mode="api_v2")
        photo = _jpeg(tmp_path / "fixture.jpg", 1600, 1200)

        init = MagicMock()
        init.json.return_value = {"media_id": 1145033854441414656}
        append = MagicMock()
        finalize = MagicMock()
        tweet = MagicMock()
        tweet.json.return_value = {"data": {"id": "1900000000000000002"}}
        client = MagicMock()
        client.post = AsyncMock(side_effect=[init, append, finalize, tweet])
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("authlib.integrations.httpx_client.AsyncOAuth1Client", return_value=client):
            result = await _post_image(uploader, photo, "api v2 image")

        assert result.success is True, result.error
        assert result.post_id == "1900000000000000002"
        assert result.metadata["auth_mode"] == "api_v2"
        commands = [
            (call.kwargs.get("data") or {}).get("command", "tweet") for call in client.post.call_args_list
        ]
        assert commands == ["INIT", "APPEND", "FINALIZE", "tweet"], commands
        assert client.post.call_args_list[0].kwargs["data"]["media_category"] == "tweet_image"
        assert client.post.call_args_list[0].kwargs["data"]["media_type"] == "image/jpeg"

    @pytest.mark.asyncio
    async def test_the_official_api_path_refuses_a_bad_image_before_any_request(self, tmp_path: Path) -> None:
        uploader = _x_uploader(tmp_path, auth_mode="api_v2")
        photo = _png(tmp_path / "fixture.png", 400, 4000)  # wrong aspect for X

        with patch(
            "authlib.integrations.httpx_client.AsyncOAuth1Client",
            side_effect=AssertionError("no HTTP client may be built for a rejected image"),
        ):
            result = await _post_image(uploader, photo, "caption")

        assert result.success is False
        assert result.error is not None and result.error.startswith("X_IMAGE_REJECTED")
        assert "X (Twitter)" in result.error


# ---------------------------------------------------------------------------
# (3) Capability and code agree — the A2 trap cannot reappear on this path
# ---------------------------------------------------------------------------


def _registered_uploader_class(platform: str):
    from xpst.platforms.base import PlatformRegistry

    PlatformRegistry.auto_discover()
    return type(PlatformRegistry.get(platform, XPSTConfig()))


class TestImageCapabilityParity:
    def test_image_capability_requires_image_containers(self) -> None:
        for name, spec in PLATFORM_SPECS.items():
            if spec.supports("image"):
                assert spec.image_containers, f"{name} declares images with no accepted format"
                assert spec.image_size_cap_mb() is not None, f"{name} declares images with no size ceiling"

    def test_every_destination_declaring_images_overrides_upload_image(self) -> None:
        """Declared capability without code is exactly the defect card A2 fixed."""
        for name, spec in PLATFORM_SPECS.items():
            uploader = _registered_uploader_class(name)
            override = getattr(uploader, "upload_image", None)
            has_path = override is not None and override is not PlatformUploader.upload_image
            assert has_path == spec.supports("image"), (
                f"{name}: upload_image override={has_path} but the spec declares "
                f"image={spec.supports('image')}"
            )

    def test_no_destination_overrides_upload_image_without_declaring_it(self) -> None:
        """The reverse direction: a real path that no surface would ever reach."""
        from xpst.content import DESTINATION_CONTENT_PROFILES

        for name in DESTINATION_CONTENT_PROFILES:
            if name not in PLATFORM_SPECS:
                continue
            spec = PLATFORM_SPECS[name]
            override = getattr(_registered_uploader_class(name), "upload_image", None)
            if override is not None and override is not PlatformUploader.upload_image:
                assert spec.supports("image"), f"{name} can publish images but does not declare it"

    @pytest.mark.parametrize("platform", ["youtube", "tiktok", "threads"])
    def test_a_video_only_destination_refuses_an_image_by_name(self, platform: str, tmp_path: Path) -> None:
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        with pytest.raises(AssertionError):
            # Sanity: the destination really is video-only in this table.
            assert PLATFORM_SPECS[platform].supports("image")

        report = verify_media(photo, platform, check_loudness=False)
        assert [check.name for check in report.errors] == ["modality"]
        assert PLATFORM_SPECS[platform].display_name in report.errors[0].detail

    def test_the_base_refusal_names_the_destination(self) -> None:
        class _VideoOnly(PlatformUploader):
            async def upload(self, video_path: Path, caption: str) -> UploadResult:  # pragma: no cover
                return UploadResult(success=True, platform="video_only")

            async def check_health(self):  # pragma: no cover
                from xpst.platforms.base import PlatformHealth

                return PlatformHealth(platform="video_only", authenticated=True, session_valid=True)

        uploader = _VideoOnly(XPSTConfig())
        result = asyncio.run(uploader.upload_image(Path("/tmp/whatever.jpg"), "caption"))

        assert result.success is False
        assert result.error is not None and "_VideoOnly" not in result.error
        assert "upload path" in result.error
        assert result.retryable is False

    def test_rejection_reasons_are_the_preflight_errors(self, tmp_path: Path) -> None:
        """One wording, two callers: the adapter refuses in the preflight's words."""
        photo = _jpeg(tmp_path / "wide.jpg", 3000, 1000)

        reasons = image_rejection_reasons(photo, "instagram")

        assert len(reasons) == 1
        assert "Instagram" in reasons[0]
        assert reasons[0] in {check.detail for check in verify_media(photo, "instagram").errors}


# ---------------------------------------------------------------------------
# (4) The engine route: ContentType.IMAGE → post_manual_image
# ---------------------------------------------------------------------------


def _engine(tmp_path: Path):
    from xpst.engine import CrossPostEngine

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.video.download_dir = str(tmp_path / "downloads")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    for platform in (config.youtube, config.x, config.instagram, config.tiktok, config.threads):
        platform.enabled = False
    return CrossPostEngine(config)


def _image_config_dir(tmp_path: Path) -> str:
    """A config directory with an enabled image destination and a real fixture."""
    import yaml

    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    _jpeg(media / "fixture.jpg", 1080, 1080)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    token = tmp_path / "ig-token.json"
    token.write_text("{}", encoding="utf-8")
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 4,
                "accounts": {
                    "local": {"path": str(media)},
                    "instagram": {
                        "enabled": True,
                        "auth_mode": "graph_api",
                        "graph_access_token": "fixture-token",
                        "graph_ig_user_id": "1",
                    },
                },
                "video": {"download_dir": str(tmp_path / "downloads")},
                "monitoring": {},
            }
        ),
        encoding="utf-8",
    )
    return str(cfg_dir)


def _mock_uploader(platform: str, *, success: bool = True):
    from xpst.platforms.base import PlatformHealth

    uploader = MagicMock(spec=PlatformUploader)
    uploader.platform_name = platform
    uploader.upload_image = AsyncMock(
        return_value=UploadResult(
            success=success,
            post_id="img1" if success else None,
            post_url="https://example.invalid/img1" if success else None,
            error=None if success else f"{platform} refused the image",
            platform=platform,
        )
    )
    uploader.check_health = AsyncMock(
        return_value=PlatformHealth(platform=platform, authenticated=True, session_valid=True)
    )
    return uploader


class TestEngineImageRoute:
    @pytest.mark.asyncio
    async def test_post_manual_image_uploads_once_per_destination(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.upload_service.anti_bot = None
        engine._platforms["instagram"] = _mock_uploader("instagram")
        engine._platforms["x"] = _mock_uploader("x")
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        result = await engine.post_manual_image(photo, "caption", ["instagram", "x"])

        assert result.results["instagram"].success is True
        assert result.results["x"].success is True
        assert engine._platforms["instagram"].upload_image.await_count == 1
        assert engine._platforms["x"].upload_image.await_count == 1
        engine._platforms["instagram"].upload.assert_not_called()
        engine._platforms["x"].upload_carousel.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_publishable_image_never_goes_through_video_encoding(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.upload_service.anti_bot = None
        engine._platforms["x"] = _mock_uploader("x")
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        with patch.object(
            engine.upload_service, "_encode_for_platform", side_effect=AssertionError("an image is not encoded")
        ):
            result = await engine.post_manual_image(photo, "caption", ["x"])

        assert result.results["x"].success is True

    @pytest.mark.asyncio
    async def test_an_identical_image_and_caption_is_not_posted_twice(self, tmp_path: Path) -> None:
        """Idempotency: the content hash the video path uses covers images too."""
        engine = _engine(tmp_path)
        engine.upload_service.anti_bot = None
        uploader = _mock_uploader("x")
        engine._platforms["x"] = uploader
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        first = await engine.post_manual_image(photo, "same caption", ["x"])
        second = await engine.post_manual_image(photo, "same caption", ["x"])

        assert first.results["x"].success is True
        assert second.results["x"].success is True
        assert second.results["x"].metadata.get("already_posted") is True
        assert uploader.upload_image.await_count == 1, "an identical image must not be posted twice"

    @pytest.mark.asyncio
    async def test_a_missing_file_is_reported_not_uploaded(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine._platforms["x"] = _mock_uploader("x")

        with pytest.raises(FileNotFoundError):
            await engine.post_manual_image(tmp_path / "nope.jpg", "caption", ["x"])

        engine._platforms["x"].upload_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_typed_request_dispatches_an_image_to_the_image_path(self, tmp_path: Path) -> None:
        from xpst.content import ContentRequest

        engine = _engine(tmp_path)
        engine.upload_service.anti_bot = None
        engine._platforms["instagram"] = _mock_uploader("instagram")
        photo = _jpeg(tmp_path / "fixture.jpg", 1080, 1080)

        request = ContentRequest(content_type=ContentType.IMAGE, media=(str(photo),), text="caption", platforms=("instagram",))
        result = await engine.post_request(request)

        assert result.results["instagram"].success is True
        assert engine._platforms["instagram"].upload_image.await_count == 1

    @pytest.mark.asyncio
    async def test_the_post_service_routes_an_image_without_the_video_encoder(self, tmp_path: Path) -> None:
        from xpst.services.post_service import PostRequest, PostService

        engine = MagicMock()
        engine.post_manual_image = AsyncMock(
            return_value=SimpleNamespace(
                results={"instagram": UploadResult(success=True, post_id="1", post_url="https://e.invalid/1")},
                video_id="image_1",
                caption="hi",
            )
        )
        engine.post_manual = AsyncMock()
        engine.post_manual_carousel = AsyncMock()
        config_dir = _image_config_dir(tmp_path)
        config = XPSTConfig.load(str(Path(config_dir) / "config.yaml"))
        photo = tmp_path / "media" / "fixture.jpg"
        service = PostService(config, config_dir, engine_factory=lambda _cfg: engine)

        envelope = service.execute(
            PostRequest.from_payload(
                {"content_type": "image", "media": [str(photo)], "caption": "hi", "platforms": ["instagram"]}
            )
        )

        assert envelope["content_type"] == "image"
        assert envelope["ok"] is True, envelope.get("blockers")
        assert engine.post_manual_image.await_count == 1
        assert engine.post_manual.await_count == 0
        assert engine.post_manual_carousel.await_count == 0
