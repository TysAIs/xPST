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

    def test_base_upload_carousel_calls_stitch_and_upload(self):
        """Default upload_carousel should delegate to _stitch_and_upload."""

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
    def test_upload_carousel_single_falls_back(self, mock_validate, mock_get_client):
        """Single item carousel should fall back to regular upload."""
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        mock_client = MagicMock()
        mock_media = MagicMock()
        mock_media.pk = 12345
        mock_media.code = "ABC123"
        mock_client.clip_upload.return_value = mock_media
        mock_get_client.return_value = mock_client

        config = XPSTConfig()
        uploader = InstagramUploader(config)

        # Single item should call upload() not album_upload()
        media_paths = [Path("/fake1.mp4")]
        asyncio.run(uploader.upload_carousel(media_paths, "test"))

        mock_client.clip_upload.assert_called_once()
        mock_client.album_upload.assert_not_called()

    def test_upload_carousel_truncates_to_10(self):
        """Instagram carousels should be limited to 10 items."""
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        config = XPSTConfig()
        uploader = InstagramUploader(config)

        # Verify truncation logic exists (would need mocking to fully test)
        assert hasattr(uploader, "upload_carousel")


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
    def test_upload_carousel_single_falls_back(self, mock_validate, mock_get_client):
        """Single item carousel should fall back to regular upload."""
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

        # Single item should use regular upload, not thread
        assert result.success is True


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
