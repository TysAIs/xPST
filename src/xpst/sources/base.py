"""
Video source plugins

Source plugins handle downloading videos from platforms like TikTok, Instagram, YouTube, and X.
Each plugin must implement the VideoSource abstract base class.

Carousels (multi-media posts) are supported via the media_paths field in DownloadResult,
while single-video downloads continue using video_path for backward compatibility.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig


class ContentType(str, Enum):
    """Content type for downloaded media"""
    VIDEO = "video"
    CAROUSEL_VIDEO = "carousel_video"
    CAROUSEL_IMAGE = "carousel_image"
    CAROUSEL_MIXED = "carousel_mixed"
    IMAGE = "image"


@dataclass
class VideoMetadata:
    """Metadata for a downloaded video or media post"""
    video_id: str
    url: str
    caption: str = ""
    description: str = ""
    duration: int = 0
    width: int = 0
    height: int = 0
    view_count: int = 0
    like_count: int = 0
    timestamp: str | None = None
    author: str = ""
    thumbnail_url: str = ""
    hashtags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    # Multi-source / carousel support
    content_type: str = ContentType.VIDEO
    media_paths: list[Path] = field(default_factory=list)
    source_platform: str = ""

    @property
    def is_carousel(self) -> bool:
        """Check if this is a carousel/multi-media post"""
        return self.content_type in (
            ContentType.CAROUSEL_VIDEO,
            ContentType.CAROUSEL_IMAGE,
            ContentType.CAROUSEL_MIXED,
        )

    @property
    def primary_media_path(self) -> Path | None:
        """Get the primary media path (first item for carousels, or the single path)"""
        if self.media_paths:
            return self.media_paths[0]
        return None


@dataclass
class DownloadResult:
    """Result of a video download attempt"""
    success: bool
    video_path: Path | None = None
    metadata: VideoMetadata | None = None
    error: str | None = None
    format_used: str = ""
    # Carousel support: list of all downloaded media paths
    media_paths: list[Path] = field(default_factory=list)

    @property
    def all_paths(self) -> list[Path]:
        """Get all media paths (combines video_path + media_paths)"""
        paths = list(self.media_paths)
        if self.video_path and self.video_path not in paths:
            paths.insert(0, self.video_path)
        return paths

    @property
    def is_carousel(self) -> bool:
        """Check if this result contains multiple media items"""
        return len(self.media_paths) > 1


class VideoSource(ABC):
    """
    Abstract base class for video sources.

    All source plugins must:
    1. Inherit from this class
    2. Implement the download() method
    3. Implement the list_videos() method
    """

    def __init__(self, config: XPSTConfig):
        """
        Initialize the source with configuration.

        Args:
            config: xPST configuration
        """
        self.config = config

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Get the source platform name"""
        pass

    @abstractmethod
    async def list_videos(self, max_count: int = 10) -> list[VideoMetadata]:
        """
        List recent videos from the source.

        Args:
            max_count: Maximum number of videos to return

        Returns:
            List of video metadata
        """
        pass

    @abstractmethod
    async def download(self, video_id: str, output_dir: Path) -> DownloadResult:
        """
        Download a video by ID.

        Args:
            video_id: Video ID to download
            output_dir: Directory to save the video

        Returns:
            DownloadResult with video path and metadata
        """
        pass

    @abstractmethod
    async def check_health(self) -> dict[str, Any]:
        """
        Check the health of the source.

        Returns:
            Health status dictionary
        """
        pass


class SourceRegistry:
    """
    Registry for video sources.

    Manages discovery and instantiation of source plugins.
    """

    _registry: dict[str, type[VideoSource]] = {}

    @classmethod
    def register(cls, name: str, source_class: type[VideoSource]) -> None:
        """
        Register a video source.

        Args:
            name: Source name
            source_class: Source class
        """
        cls._registry[name] = source_class

    @classmethod
    def get(cls, name: str, config: XPSTConfig) -> VideoSource:
        """
        Get a video source instance.

        Args:
            name: Source name
            config: Configuration

        Returns:
            Source instance

        Raises:
            KeyError: If source not found
        """
        if name not in cls._registry:
            raise KeyError(f"Source not found: {name}. Available: {list(cls._registry.keys())}")

        return cls._registry[name](config)

    @classmethod
    def list_sources(cls) -> list[str]:
        """List all registered sources"""
        return list(cls._registry.keys())

    @classmethod
    def auto_discover(cls) -> None:
        """Auto-discover and register all source modules in this package.

        Scans ``xpst.sources`` for non-private modules and imports them.
        Each module should call ``SourceRegistry.register()`` at module level.
        Import failures are logged at DEBUG level (some sources have optional
        dependencies that may not be installed).
        """

        import importlib
        import pkgutil

        import xpst.sources as sources_pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(sources_pkg.__path__):
            if modname.startswith("_"):
                continue
            try:
                importlib.import_module(f"xpst.sources.{modname}")
            except ImportError as e:
                # Log but don't fail - some sources may have optional deps
                import logging
                logging.getLogger(__name__).debug(
                    f"Could not import source module {modname}: {e}"
                )
