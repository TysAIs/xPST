"""Tests for XPST multi-source support"""

from pathlib import Path

import pytest

from xpst.config import XPSTConfig
from xpst.sources.base import (
    ContentType,
    DownloadResult,
    SourceRegistry,
    VideoMetadata,
    VideoSource,
)


class TestVideoMetadata:
    """Test VideoMetadata with carousel support"""

    def test_default_video_metadata(self):
        """Test default VideoMetadata creation"""
        metadata = VideoMetadata(
            video_id="test123",
            url="https://example.com/video",
        )

        assert metadata.video_id == "test123"
        assert metadata.url == "https://example.com/video"
        assert metadata.content_type == ContentType.VIDEO
        assert metadata.source_platform == ""
        assert metadata.media_paths == []
        assert metadata.is_carousel is False
        assert metadata.primary_media_path is None

    def test_carousel_metadata(self):
        """Test carousel VideoMetadata"""
        paths = [Path("/tmp/1.mp4"), Path("/tmp/2.mp4")]
        metadata = VideoMetadata(
            video_id="carousel1",
            url="https://example.com/carousel",
            content_type=ContentType.CAROUSEL_VIDEO,
            media_paths=paths,
            source_platform="instagram",
        )

        assert metadata.is_carousel is True
        assert metadata.primary_media_path == Path("/tmp/1.mp4")
        assert len(metadata.media_paths) == 2

    def test_content_types(self):
        """Test all content types"""
        for ct in ContentType:
            metadata = VideoMetadata(
                video_id="test",
                url="https://example.com",
                content_type=ct,
            )
            if ct in (ContentType.CAROUSEL_VIDEO, ContentType.CAROUSEL_IMAGE, ContentType.CAROUSEL_MIXED):
                assert metadata.is_carousel is True
            else:
                assert metadata.is_carousel is False


class TestDownloadResult:
    """Test DownloadResult with carousel support"""

    def test_single_video_result(self):
        """Test single video download result"""
        result = DownloadResult(
            success=True,
            video_path=Path("/tmp/video.mp4"),
        )

        assert result.is_carousel is False
        assert result.all_paths == [Path("/tmp/video.mp4")]

    def test_carousel_result(self):
        """Test carousel download result"""
        paths = [Path("/tmp/1.mp4"), Path("/tmp/2.mp4"), Path("/tmp/3.mp4")]
        result = DownloadResult(
            success=True,
            video_path=paths[0],
            media_paths=paths,
        )

        assert result.is_carousel is True
        assert len(result.all_paths) == 3

    def test_all_paths_no_duplicates(self):
        """Test that all_paths doesn't include duplicates"""
        path = Path("/tmp/video.mp4")
        result = DownloadResult(
            success=True,
            video_path=path,
            media_paths=[path],
        )

        assert result.all_paths == [path]


class TestSourceRegistry:
    """Test SourceRegistry"""

    def test_register_and_list(self):
        """Test registering and listing sources"""
        registry = SourceRegistry()

        class MockSource(VideoSource):
            @property
            def source_name(self):
                return "mock"

            async def list_videos(self, max_count=10):
                return []

            async def download(self, video_id, output_dir):
                return DownloadResult(success=False)

            async def check_health(self):
                return {"status": "ok"}

        registry.register("mock", MockSource)
        assert "mock" in registry.list_sources()

    def test_get_source(self):
        """Test getting a source instance"""
        registry = SourceRegistry()

        class TestSource(VideoSource):
            @property
            def source_name(self):
                return "test"

            async def list_videos(self, max_count=10):
                return []

            async def download(self, video_id, output_dir):
                return DownloadResult(success=False)

            async def check_health(self):
                return {"status": "ok"}

        registry.register("test_get", TestSource)
        config = XPSTConfig()
        source = registry.get("test_get", config)
        assert isinstance(source, TestSource)

    def test_get_missing_source(self):
        """Test getting a non-existent source raises error"""
        registry = SourceRegistry()
        config = XPSTConfig()

        with pytest.raises(KeyError, match="Source not found"):
            registry.get("nonexistent", config)


class TestTikTokSource:
    """Test TikTok source with carousel detection"""

    def test_detect_video_content(self):
        """Test detecting regular video content"""
        from xpst.sources.tiktok import TikTokSource

        config = XPSTConfig()
        source = TikTokSource(config)

        data = {
            "id": "123",
            "format_note": "HD",
            "duration": 15,
        }

        content_type = source._detect_content_type(data)
        assert content_type == ContentType.VIDEO

    def test_detect_slideshow_content(self):
        """Test detecting slideshow content"""
        from xpst.sources.tiktok import TikTokSource

        config = XPSTConfig()
        source = TikTokSource(config)

        data = {
            "id": "456",
            "format_note": "slideshow",
            "entries": [
                {"url": "https://example.com/1.jpg", "ext": "jpg", "vcodec": "none"},
                {"url": "https://example.com/2.jpg", "ext": "jpg", "vcodec": "none"},
            ],
        }

        content_type = source._detect_content_type(data)
        assert content_type == ContentType.CAROUSEL_IMAGE


class TestLocalSource:
    """Test LocalSource"""

    def test_single_video_file(self, tmp_path):
        """Test listing a single video file"""
        from xpst.sources.local import LocalSource

        config = XPSTConfig()
        source = LocalSource(config)

        # Create a test video file
        video_file = tmp_path / "test_video.mp4"
        video_file.write_bytes(b"fake video content")

        source.set_path(video_file)

        import asyncio
        videos = asyncio.run(source.list_videos())

        assert len(videos) == 1
        assert videos[0].video_id == "test_video"
        assert videos[0].content_type == ContentType.VIDEO
        assert videos[0].source_platform == "local"

    def test_directory_with_distinct_videos(self, tmp_path):
        """Test scanning a directory with distinct (non-grouped) videos"""
        from xpst.sources.local import LocalSource

        config = XPSTConfig()
        source = LocalSource(config)

        # Create test video files with distinct names (no grouping pattern)
        for name in ["cat_dance", "sunset_timelapse", "cooking_tips"]:
            video_file = tmp_path / f"{name}.mp4"
            video_file.write_bytes(b"fake video content")

        source.set_path(tmp_path)

        import asyncio
        videos = asyncio.run(source.list_videos())

        assert len(videos) == 3

    def test_directory_carousel_grouping(self, tmp_path):
        """Test that numbered files are detected as carousel"""
        from xpst.sources.local import LocalSource

        config = XPSTConfig()
        source = LocalSource(config)

        # Create files with a grouping pattern (post_001, post_002, etc.)
        for i in range(3):
            img_file = tmp_path / f"post_{i:03d}.mp4"
            img_file.write_bytes(b"fake video content")

        source.set_path(tmp_path)

        import asyncio
        videos = asyncio.run(source.list_videos())

        # Grouped as one carousel
        assert len(videos) == 1
        assert videos[0].is_carousel
        assert len(videos[0].media_paths) == 3

    def test_carousel_detection(self, tmp_path):
        """Test carousel detection from grouped files"""
        from xpst.sources.local import LocalSource

        config = XPSTConfig()
        source = LocalSource(config)

        # Create carousel-like files
        for i in range(3):
            img_file = tmp_path / f"carousel_{i:03d}.jpg"
            img_file.write_bytes(b"fake image content")

        source.set_path(tmp_path)

        import asyncio
        videos = asyncio.run(source.list_videos())

        # Should detect as a carousel
        assert len(videos) == 1
        assert videos[0].is_carousel
        assert videos[0].content_type == ContentType.CAROUSEL_IMAGE

    def test_download_single_file(self, tmp_path):
        """Test downloading (copying) a single file"""
        from xpst.sources.local import LocalSource

        config = XPSTConfig()
        source = LocalSource(config)

        # Create source file
        source_file = tmp_path / "source.mp4"
        source_file.write_bytes(b"video content here")

        source.set_path(tmp_path)

        output_dir = tmp_path / "output"

        import asyncio
        result = asyncio.run(source.download("source", output_dir))

        assert result.success
        assert result.video_path.exists()
        assert result.video_path.read_bytes() == b"video content here"

    def test_health_check(self, tmp_path):
        """Test health check"""
        from xpst.sources.local import LocalSource

        config = XPSTConfig()
        source = LocalSource(config)
        source.set_path(tmp_path)

        import asyncio
        health = asyncio.run(source.check_health())

        assert health["source"] == "local"
        assert health["status"] == "ok"
        assert health["path_configured"] is True
        assert health["path_exists"] is True

    def test_health_check_no_path(self):
        """Test health check with no path configured"""
        from xpst.sources.local import LocalSource

        config = XPSTConfig()
        source = LocalSource(config)

        import asyncio
        health = asyncio.run(source.check_health())

        assert health["status"] == "error"


class TestYouTubeSource:
    """Test YouTube source"""

    def test_source_name(self):
        """Test source name property"""
        from xpst.sources.youtube import YouTubeSource

        config = XPSTConfig()
        source = YouTubeSource(config)
        assert source.source_name == "youtube"

    def test_build_command(self):
        """Test building yt-dlp command"""
        from xpst.sources.youtube import YouTubeSource

        config = XPSTConfig()
        source = YouTubeSource(config)
        cmd = source._build_base_command()

        assert cmd[0] == source._yt_dlp_path
        assert "--no-warnings" in cmd


class TestXSource:
    """Test X source"""

    def test_source_name(self):
        """Test source name property"""
        from xpst.sources.x import XSource

        config = XPSTConfig()
        source = XSource(config)
        assert source.source_name == "x"

    def test_build_command_with_cookies(self, tmp_path):
        """Test building command with cookies file"""
        from xpst.sources.x import XSource

        cookies_file = tmp_path / "cookies.json"
        cookies_file.write_text("{}")

        config = XPSTConfig()
        config.x.cookies_file = str(cookies_file)
        source = XSource(config)

        cmd = source._build_base_command()
        assert "--cookies" in cmd


class TestInstagramSource:
    """Test Instagram source"""

    def test_source_name(self):
        """Test source name property"""
        from xpst.sources.instagram import InstagramSource

        config = XPSTConfig()
        source = InstagramSource(config)
        assert source.source_name == "instagram"


class TestAutoDiscovery:
    """Test source auto-discovery"""

    def test_sources_discovered(self):
        """Test that sources are discovered via __init__"""
        # The __init__.py should register available sources
        # At minimum, local source should always be available
        sources = SourceRegistry.list_sources()
        assert "local" in sources

    def test_content_type_enum(self):
        """Test ContentType enum values"""
        assert ContentType.VIDEO == "video"
        assert ContentType.CAROUSEL_VIDEO == "carousel_video"
        assert ContentType.CAROUSEL_IMAGE == "carousel_image"
        assert ContentType.CAROUSEL_MIXED == "carousel_mixed"
        assert ContentType.IMAGE == "image"


class TestConfigUpdates:
    """Test config updates for new sources"""

    def test_default_config_has_new_fields(self):
        """Test that default config includes new fields"""
        config = XPSTConfig()

        assert hasattr(config, 'local')
        assert config.local.path == ""
        assert config.instagram.username == ""
        assert config.x.username == ""
        assert config.youtube.channel_id == ""

    def test_config_from_yaml_with_new_fields(self, tmp_path):
        """Test loading config with new fields from YAML"""
        config_data = {
            "accounts": {
                "tiktok": {"username": "test"},
                "youtube": {"channel_id": "UC123", "username": "@test"},
                "x": {"username": "xuser"},
                "instagram": {"username": "iguser"},
                "local": {"path": "/tmp/videos"},
            },
        }

        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            import yaml
            yaml.dump(config_data, f)

        config = XPSTConfig.load(str(config_file))

        assert config.youtube.channel_id == "UC123"
        assert config.youtube.username == "@test"
        assert config.x.username == "xuser"
        assert config.instagram.username == "iguser"
        assert config.local.path == "/tmp/videos"

    def test_env_vars_for_new_fields(self, monkeypatch):
        """Test environment variables for new config fields"""
        monkeypatch.setenv("XPST_X_USERNAME", "env_x_user")
        monkeypatch.setenv("XPST_INSTAGRAM_USERNAME", "env_ig_user")
        monkeypatch.setenv("XPST_YOUTUBE_CHANNEL_ID", "UC_env")
        monkeypatch.setenv("XPST_LOCAL_PATH", "/env/path")

        config = XPSTConfig()
        config = XPSTConfig._apply_env_vars(config)

        assert config.x.username == "env_x_user"
        assert config.instagram.username == "env_ig_user"
        assert config.youtube.channel_id == "UC_env"
        assert config.local.path == "/env/path"
