"""
Base classes for platform plugins

Platform plugins handle uploading videos to specific platforms.
Each plugin must implement the PlatformUploader abstract base class.

Example plugin:
    class MyPlatformUploader(PlatformUploader):
        async def upload(self, video_path: Path, caption: str) -> UploadResult:
            # Upload logic here
            return UploadResult(success=True, post_id="123", post_url="https://...")
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class UploadResult:
    """Result of a video upload attempt"""
    success: bool
    post_id: str | None = None
    post_url: str | None = None
    error: str | None = None
    platform: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformHealth:
    """Health status of a platform"""
    platform: str
    authenticated: bool = False
    session_valid: bool = False
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class PlatformUploader(ABC):
    """
    Abstract base class for platform uploaders.

    All platform plugins must:
    1. Inherit from this class
    2. Implement the upload() method
    3. Implement the check_health() method
    4. Implement the authenticate() method if needed

    The plugin will be automatically discovered if placed in the
    xpst/platforms/ directory with the correct class name.
    """

    def __init__(self, config: XPSTConfig):
        """
        Initialize the uploader with configuration.

        Args:
            config: xPST configuration
        """
        self.config = config
        self._platform_name = self.__class__.__name__.lower().replace("uploader", "")
        self._session_manager = None  # Set by engine after init

    @property
    def platform_name(self) -> str:
        """Get the platform name"""
        return self._platform_name

    @property
    def manifest(self) -> ProviderManifest:
        """Return provider metadata for UI, CLI, MCP, and updater use."""
        return ProviderManifest(
            name=self.platform_name,
            display_name=self.platform_name.title(),
            roles=(ProviderRole.DESTINATION,),
            capabilities=(
                ProviderCapability.UPLOAD,
                ProviderCapability.HEALTH,
                ProviderCapability.RATE_LIMITS,
            ),
            auth_mode=AuthMode.UNKNOWN,
        )

    @abstractmethod
    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        """
        Upload a video to the platform.

        Args:
            video_path: Path to the video file
            caption: Caption/description for the video

        Returns:
            UploadResult with success status and metadata
        """
        pass

    @abstractmethod
    async def check_health(self) -> PlatformHealth:
        """
        Check the health/authentication status of the platform.

        Returns:
            PlatformHealth with authentication status
        """
        pass

    async def authenticate(self) -> bool:
        """
        Authenticate with the platform.

        Override this if your platform requires authentication flow.

        Returns:
            True if authentication succeeded
        """
        return True

    async def delete(self, post_id: str) -> bool:
        """Delete a post from this platform. Override in subclasses."""
        raise NotImplementedError(f"Delete not implemented for {self.__class__.__name__}")

    async def upload_carousel(self, media_paths: list[Path], caption: str) -> UploadResult:
        """
        Upload a carousel/multi-media post.

        Override in subclasses that support native carousel uploads (e.g. Instagram).
        Default: stitch all media into a single vertical video and upload normally.

        Args:
            media_paths: List of paths to images/videos
            caption: Caption/description for the post

        Returns:
            UploadResult with success status and metadata
        """
        # Default: stitch into single video and upload
        return await self._stitch_and_upload(media_paths, caption)

    async def _stitch_and_upload(self, media_paths: list[Path], caption: str) -> UploadResult:
        """
        Stitch multiple media files into a single video and upload.

        Used as fallback for platforms that don't support native carousels.
        """
        import tempfile

        from xpst.utils.video import VideoProcessor

        output_path: Path | None = None
        try:
            processor = VideoProcessor()
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                output_path = Path(tmp.name)

            processor.stitch_carousel_to_video(media_paths, output_path)
            return await self.upload(output_path, caption)
        except Exception as e:
            logger.error(f"Stitch and upload failed: {e}")
            return UploadResult(
                success=False,
                error=f"Carousel stitch failed: {str(e)[:200]}",
                platform=self.platform_name,
            )
        finally:
            # The stitched video is a temp artifact — never leak it (ISC-91)
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    def _validate_video(self, video_path: Path) -> None:
        """Validate that a video file exists, is non-empty, and within size limits.

        Called before every upload attempt. Override ``max_size_gb`` in
        subclasses for platforms with different limits.

        Args:
            video_path: Path to the video file.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file is empty or exceeds 1 GB.
        """

        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        if video_path.stat().st_size == 0:
            raise ValueError(f"Video is empty: {video_path}")

        # Check file size limits (platform-specific, override if needed)
        max_size_gb = 1  # 1 GB default
        if video_path.stat().st_size > max_size_gb * 1024 * 1024 * 1024:
            raise ValueError(f"Video exceeds {max_size_gb} GB limit: {video_path}")


class PlatformRegistry:
    """
    Registry for platform uploaders.

    Manages discovery and instantiation of platform plugins.
    """

    _registry: dict[str, type[PlatformUploader]] = {}

    @classmethod
    def register(cls, name: str, uploader_class: type[PlatformUploader]) -> None:
        """
        Register a platform uploader.

        Args:
            name: Platform name
            uploader_class: Uploader class
        """
        cls._registry[name] = uploader_class

    @classmethod
    def get(cls, name: str, config: XPSTConfig) -> PlatformUploader:
        """
        Get a platform uploader instance.

        Args:
            name: Platform name
            config: Configuration

        Returns:
            Uploader instance

        Raises:
            KeyError: If platform not found
        """
        if name not in cls._registry:
            raise KeyError(f"Platform not found: {name}. Available: {list(cls._registry.keys())}")

        return cls._registry[name](config)

    @classmethod
    def list_platforms(cls) -> list[str]:
        """List all registered platforms"""
        return list(cls._registry.keys())

    @classmethod
    def list_manifests(cls, config: XPSTConfig) -> list[ProviderManifest]:
        """Return manifests for all registered destination providers."""
        manifests: list[ProviderManifest] = []
        for name in cls.list_platforms():
            try:
                manifests.append(cls.get(name, config).manifest)
            except Exception as e:
                logger.debug(f"Could not load platform manifest for {name}: {e}")
        return manifests

    @classmethod
    def auto_discover(cls) -> None:
        """Auto-discover and register all platform modules in this package."""

        import importlib
        import pkgutil

        import xpst.platforms as platforms_pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(platforms_pkg.__path__):
            if modname.startswith("_") or modname == "base":
                continue
            try:
                module = importlib.import_module(f"xpst.platforms.{modname}")
            except ImportError as e:
                logger.debug(f"Could not import platform module {modname}: {e}")
                continue

            for value in vars(module).values():
                if (
                    isinstance(value, type)
                    and issubclass(value, PlatformUploader)
                    and value is not PlatformUploader
                ):
                    name = value.__name__.lower().replace("uploader", "")
                    cls.register(name, value)
