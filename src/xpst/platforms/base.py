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
from enum import Enum
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.providers import AuthMode, ProviderCapability, ProviderManifest, ProviderRole
from xpst.utils.logger import get_logger

logger = get_logger(__name__)


class DeleteOutcome(str, Enum):
    """Explicit outcome of a platform delete/unpublish attempt (Phase-1.2 D5).

    Every platform delete MUST resolve to one of these values — there are no
    silent failures. The UI renders the matching ``DeleteResult.message``
    verbatim so the user always sees exactly what happened.
    """

    DELETED = "deleted"  # hard delete confirmed; state keeps a tombstone
    SOFT_HIDDEN = "soft_hidden"  # reversible unpublish (e.g. YouTube private/unlisted)
    PENDING = "pending"  # not confirmed; user must act (e.g. TikTok web-session fallback failed)
    UNSUPPORTED = "unsupported"  # platform has no deletable post / no delete API


# UI-facing messages per outcome. ``{platform}`` is always substituted; the
# share URL is appended by the engine for pending/unsupported results so callers
# never see a bare enum without an actionable message.
_DELETE_UI_MESSAGES: dict[DeleteOutcome, str] = {
    DeleteOutcome.DELETED: "Deleted from {platform}",
    DeleteOutcome.SOFT_HIDDEN: "Unpublished on {platform} (reversible)",
    DeleteOutcome.PENDING: "Delete pending on {platform} - remove manually",
    DeleteOutcome.UNSUPPORTED: "{platform} does not support deleting this post",
}


def delete_ui_message(outcome: DeleteOutcome, platform: str) -> str:
    """Return the UI-facing message that corresponds to ``outcome``.

    ``platform`` is substituted into the template; the share URL is appended
    separately (see :meth:`DeleteResult.with_share_url`).
    """
    return _DELETE_UI_MESSAGES[outcome].format(platform=platform)


@dataclass
class DeleteResult:
    """Result of a platform delete/unpublish attempt (Phase-1.2 D5 contract).

    Every platform ``delete()`` MUST return a ``DeleteResult`` whose
    ``outcome`` is an explicit :class:`DeleteOutcome` — never ``None`` and
    never a bare bool. ``message`` is the UI-facing text matching the outcome.
    """

    outcome: DeleteOutcome
    platform: str
    post_id: str
    message: str = ""
    share_url: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.message:
            self.message = delete_ui_message(self.outcome, self.platform)

    @property
    def ok(self) -> bool:
        """True when the post is no longer publicly visible (deleted or hidden)."""
        return self.outcome in (DeleteOutcome.DELETED, DeleteOutcome.SOFT_HIDDEN)

    def with_share_url(self, share_url: str) -> "DeleteResult":
        """Return a copy carrying ``share_url`` and an actionable message.

        Used by the engine to surface a manual-removal link on
        pending/unsupported results — the adapter itself never needs the URL.
        """
        message = self.message
        if share_url and self.outcome in (DeleteOutcome.PENDING, DeleteOutcome.UNSUPPORTED):
            message = f"{message}: {share_url}"
        return DeleteResult(
            outcome=self.outcome,
            platform=self.platform,
            post_id=self.post_id,
            message=message,
            share_url=share_url or self.share_url,
            detail=self.detail,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result for CLI/MCP/UI consumers."""
        return {
            "outcome": self.outcome.value,
            "platform": self.platform,
            "post_id": self.post_id,
            "message": self.message,
            "share_url": self.share_url,
            "detail": self.detail,
            "deleted": self.ok,
        }


def normalize_delete_result(raw: Any, platform: str, post_id: str) -> DeleteResult:
    """Coerce an adapter's ``delete()`` return into the DeleteResult contract.

    Accepts a proper :class:`DeleteResult` (the contract), and tolerates legacy
    bare ``bool``/``None`` returns from third-party adapters so the engine and
    UI never see an unexpected type (a ``True`` legacy return is treated as a
    confirmed hard delete; anything else is ``pending``).
    """
    if isinstance(raw, DeleteResult):
        return raw
    if raw is True:
        return DeleteResult(outcome=DeleteOutcome.DELETED, platform=platform, post_id=post_id)
    return DeleteResult(
        outcome=DeleteOutcome.PENDING,
        platform=platform,
        post_id=post_id,
        detail="adapter did not return a DeleteResult",
    )


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

    async def delete(
        self,
        post_id: str,
        *,
        soft: bool = False,
        visibility: str | None = None,
    ) -> DeleteResult:
        """Delete (or unpublish) a post from this platform.

        Subclasses MUST override this and return a :class:`DeleteResult` with
        an explicit outcome — the engine and UI rely on the contract and there
        are no silent failures. The default reports the platform as not
        supporting deletion.

        Args:
            post_id: The platform-side id of the post to delete/unpublish.
            soft: If True, request a reversible unpublish/hide instead of a
                hard delete where the platform offers one (e.g. YouTube
                ``status.privacyStatus=private``). Ignored on platforms that
                only support hard deletes.
            visibility: Optional target visibility for soft hides (platform
                specific, e.g. ``private``/``unlisted`` for YouTube).

        Returns:
            DeleteResult with an explicit outcome and UI-facing message.
        """
        return DeleteResult(
            outcome=DeleteOutcome.UNSUPPORTED,
            platform=self.platform_name,
            post_id=post_id,
        )

    async def get_followers(self) -> int:
        """Return the current follower count for this platform's account.

        Override in subclasses that support follower count retrieval.
        Returns 0 if not supported or on error.
        """
        return 0

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

    # Legacy/mangled names that must resolve to the canonical platform key.
    # Previously, auto_discover registered every PlatformUploader subclass under
    # its mangled class name IN ADDITION to the explicit module-level register()
    # calls, so MessengerAdapter was exposed both as "messenger" and
    # "messengeradapter". Existing user data (config/state) may still reference
    # the legacy name — resolve it via this alias, no data migration required.
    _ALIASES: dict[str, str] = {"messengeradapter": "messenger"}

    @classmethod
    def _canonical_name(cls, name: str) -> str:
        """Resolve a legacy/mangled name to the canonical platform key."""
        return cls._ALIASES.get(name, name)

    @classmethod
    def register(cls, name: str, uploader_class: type[PlatformUploader]) -> None:
        """
        Register a platform uploader.

        Args:
            name: Platform name
            uploader_class: Uploader class
        """
        # Canonicalize so a legacy alias can never introduce a duplicate key.
        cls._registry[cls._canonical_name(name)] = uploader_class

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
        # Backward-compat: legacy/mangled names (e.g. "messengeradapter") must
        # still resolve to the canonical platform.
        name = cls._canonical_name(name)
        if name not in cls._registry:
            raise KeyError(f"Platform not found: {name}. Available: {list(cls._registry.keys())}")

        return cls._registry[name](config)

    @classmethod
    def list_platforms(cls) -> list[str]:
        """List all registered platforms (canonical names only, deduplicated)."""
        result: list[str] = []
        seen: set[str] = set()
        for name in cls._registry:
            canonical = cls._canonical_name(name)
            if canonical not in seen:
                seen.add(canonical)
                result.append(canonical)
        return result

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
        """Auto-discover and register all platform modules in this package.

        Explicit module-level ``PlatformRegistry.register(...)`` calls are the
        single source of truth for built-in platforms; this method guarantees
        those modules are imported (triggering their explicit registration).
        Any ``PlatformUploader`` subclass that does not self-register (e.g.
        third-party plugin modules) is registered under the *module name* as
        its canonical key — never a mangled class name — so a physical platform
        can never appear more than once (see audit: ``MessengerAdapter`` was
        previously exposed as both ``messenger`` and ``messengeradapter``).
        """

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
                    # Skip subclasses already explicitly registered (module-level
                    # register() calls are the source of truth).
                    and value not in cls._registry.values()
                ):
                    cls.register(modname, value)
