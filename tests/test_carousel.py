"""Tests for carousel/multi-media upload support."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xpst.platforms.base import PlatformUploader, UploadResult


class TestPlatformUploaderCarousel:
    """Test carousel support in base PlatformUploader."""

    def test_upload_carousel_method_exists(self):
        """PlatformUploader should have upload_carousel method."""
        assert hasattr(PlatformUploader, "upload_carousel")

    def test_base_upload_carousel_refuses_instead_of_stitching(self):
        """Default upload_carousel names the destination and refuses — no stitch."""

        class DummyUploader(PlatformUploader):
            async def upload(self, video_path, caption):
                return UploadResult(success=True, post_id="test", platform="dummy")

            async def check_health(self):
                pass

        from xpst.config import XPSTConfig

        config = XPSTConfig()
        uploader = DummyUploader(config)

        # Verify method exists and is callable
        assert asyncio.iscoroutinefunction(uploader.upload_carousel)

        # And that it refuses rather than stitching the items into a video.
        result = asyncio.run(uploader.upload_carousel([Path("/fake1.jpg"), Path("/fake2.jpg")], "caption"))
        assert result.success is False
        assert result.retryable is False
        assert "dummy cannot publish carousel posts" in (result.error or "")
        assert "will not stitch" in (result.error or "")


class TestInstagramCarousel:
    """Test Instagram carousel upload."""

    def test_upload_carousel_method_exists(self):
        """InstagramUploader should override upload_carousel."""
        from xpst.platforms.instagram import InstagramUploader

        assert hasattr(InstagramUploader, "upload_carousel")

    @patch("xpst.platforms.instagram.InstagramUploader._get_client")
    def test_upload_carousel_calls_album_upload(self, mock_get_client):
        """Instagram carousel should use album_upload."""
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        # Mock the client
        mock_client = MagicMock()
        mock_media = MagicMock()
        mock_media.pk = 12345
        mock_media.code = "ABC123"
        mock_client.album_upload.return_value = mock_media
        mock_get_client.return_value = mock_client

        config = XPSTConfig()
        config.instagram.auth_mode = "session"
        uploader = InstagramUploader(config)

        media_paths = [Path("/fake1.mp4"), Path("/fake2.mp4")]
        result = asyncio.run(uploader.upload_carousel(media_paths, "test caption"))

        mock_client.album_upload.assert_called_once()
        assert result.success is True
        assert result.post_id == "12345"
        assert result.metadata["content_type"] == "carousel"
        assert result.metadata["carousel_items"] == 2

    @patch("xpst.platforms.instagram.InstagramUploader._get_client")
    @patch.object(PlatformUploader, "_validate_video")
    def test_upload_carousel_single_is_refused_not_uploaded_as_video(self, mock_validate, mock_get_client):
        """One item is not a carousel: refuse, do not upload it as a Reel."""
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        mock_client = MagicMock()
        mock_media = MagicMock()
        mock_media.pk = 12345
        mock_media.code = "ABC123"
        mock_client.clip_upload.return_value = mock_media
        mock_get_client.return_value = mock_client

        config = XPSTConfig()
        config.instagram.auth_mode = "session"
        uploader = InstagramUploader(config)

        media_paths = [Path("/fake1.mp4")]
        result = asyncio.run(uploader.upload_carousel(media_paths, "test"))

        assert result.success is False
        assert "IG_CAROUSEL_NEEDS_TWO" in (result.error or "")
        mock_client.clip_upload.assert_not_called()
        mock_client.album_upload.assert_not_called()

    @patch("xpst.platforms.instagram.InstagramUploader._get_client")
    def test_upload_carousel_over_the_limit_is_refused_not_truncated(self, mock_get_client):
        """11 items are refused with the limit named, never silently truncated."""
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        config = XPSTConfig()
        config.instagram.auth_mode = "session"
        uploader = InstagramUploader(config)

        media_paths = [Path(f"/fake{index}.jpg") for index in range(11)]
        result = asyncio.run(uploader.upload_carousel(media_paths, "test"))

        assert result.success is False
        assert "IG_CAROUSEL_TOO_MANY_ITEMS" in (result.error or "")
        assert str(InstagramUploader.MAX_CAROUSEL_ITEMS) in (result.error or "")
        mock_client.album_upload.assert_not_called()


class TestXCarousel:
    """Test X/Twitter thread creation."""

    def test_upload_carousel_method_exists(self):
        """XUploader should override upload_carousel."""
        from xpst.platforms.x import XUploader

        assert hasattr(XUploader, "upload_carousel")

    @patch("xpst.platforms.x.XUploader._get_client")
    def test_upload_carousel_creates_thread(self, mock_get_client):
        """X carousel should create a tweet thread."""
        from xpst.config import XPSTConfig
        from xpst.platforms.x import XUploader

        # Mock the client
        mock_client = MagicMock()
        mock_tweet1 = MagicMock()
        mock_tweet1.id = "111"
        mock_tweet2 = MagicMock()
        mock_tweet2.id = "222"
        mock_client.create_tweet = AsyncMock(side_effect=[mock_tweet1, mock_tweet2])
        mock_client.upload_media = AsyncMock(return_value="media_123")
        mock_get_client.return_value = mock_client

        config = XPSTConfig()
        uploader = XUploader(config)

        media_paths = [Path("/fake1.mp4"), Path("/fake2.mp4")]
        result = asyncio.run(uploader.upload_carousel(media_paths, "test caption"))

        assert result.success is True
        assert result.post_id == "111"
        assert result.metadata["content_type"] == "thread"
        assert result.metadata["thread_items"] == 2
        # Should have created 2 tweets (one main + one reply)
        assert mock_client.create_tweet.call_count == 2

    @patch("xpst.platforms.x.XUploader._get_client")
    @patch.object(PlatformUploader, "_validate_video")
    def test_upload_carousel_single_is_refused_not_posted_as_video(self, mock_validate, mock_get_client):
        """One item is not a thread: refuse, do not fall back to a video post."""
        from xpst.config import XPSTConfig
        from xpst.platforms.x import XUploader

        mock_client = MagicMock()
        mock_tweet = MagicMock()
        mock_tweet.id = "111"
        mock_client.create_tweet = AsyncMock(return_value=mock_tweet)
        mock_client.upload_media = AsyncMock(return_value="media_123")
        mock_get_client.return_value = mock_client

        config = XPSTConfig()
        uploader = XUploader(config)

        media_paths = [Path("/fake1.mp4")]
        result = asyncio.run(uploader.upload_carousel(media_paths, "test"))

        # Single item must be refused, not uploaded as a regular video post
        assert result.success is False
        assert "X_THREAD_NEEDS_TWO" in (result.error or "")
        mock_client.create_tweet.assert_not_called()
        mock_client.upload_media.assert_not_called()


class TestVideoStitching:
    """Test carousel video stitching."""

    def test_stitch_carousel_to_video_method_exists(self):
        """VideoProcessor should have stitch_carousel_to_video method."""
        from xpst.utils.video import VideoProcessor

        assert hasattr(VideoProcessor, "stitch_carousel_to_video")

    @patch("xpst.utils.video.subprocess.run")
    def test_stitch_empty_paths_raises(self, mock_run):
        """Empty media paths should raise ValueError."""
        from xpst.utils.video import VideoProcessor

        with patch.object(VideoProcessor, "_verify_ffmpeg"):
            processor = VideoProcessor()

        with pytest.raises(ValueError, match="No media files provided"):
            processor.stitch_carousel_to_video([], Path("/output.mp4"))

    @patch("xpst.utils.video.subprocess.run")
    def test_stitch_missing_file_raises(self, mock_run):
        """Missing media file should raise FileNotFoundError."""
        from xpst.utils.video import VideoProcessor

        with patch.object(VideoProcessor, "_verify_ffmpeg"):
            processor = VideoProcessor()

        with pytest.raises(FileNotFoundError, match="Media file not found"):
            processor.stitch_carousel_to_video(
                [Path("/nonexistent.mp4")],
                Path("/output.mp4"),
            )
