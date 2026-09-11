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
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

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


class UploadOutcome(str, Enum):
    """Truthful outcome of a platform upload attempt.

    ``success`` remains on :class:`UploadResult` for compatibility, but it is
    now an alias for ``published``. In particular, a provider response that
    only acknowledges processing is ``pending`` and must not enter posted
    state.
    """

    PUBLISHED = "published"
    PENDING = "pending"
    FAILED = "failed"

    # Terminology aliases for integrations that use success/processing names.
    SUCCESS = "published"
    PROCESSING = "pending"
    FAILURE = "failed"


# A status name is useful to callers that prefer status terminology while the
# enum keeps the same explicit outcome vocabulary as DeleteOutcome.
UploadStatus = UploadOutcome


@dataclass
class UploadResult:
    """Result of a video upload attempt.

    Existing callers may continue to construct this with ``success=True`` or
    ``success=False``. New provider code should set ``outcome`` explicitly for
    processing responses. A successful result is only considered published at
    the upload-service boundary after :func:`normalize_upload_result` verifies
    a real post identifier or a resource-specific public URL.
    """

    success: bool
    post_id: str | None = None
    post_url: str | None = None
    error: str | None = None
    platform: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    outcome: UploadOutcome | str | None = None
    retryable: bool | None = None
    # ``status`` is an input/output alias for integrations that use status
    # terminology instead of outcome terminology.
    status: str | None = None

    def __post_init__(self) -> None:
        """Keep the legacy boolean and explicit outcome in sync."""
        if self.outcome is None and self.status is not None:
            self.outcome = self.status
        if self.outcome is None:
            self.outcome = UploadOutcome.PUBLISHED if self.success else UploadOutcome.FAILED
        elif not isinstance(self.outcome, UploadOutcome):
            status_aliases = {
                "success": UploadOutcome.PUBLISHED,
                "published": UploadOutcome.PUBLISHED,
                "posted": UploadOutcome.PUBLISHED,
                "publish_complete": UploadOutcome.PUBLISHED,
                "completed": UploadOutcome.PUBLISHED,
                "processing": UploadOutcome.PENDING,
                "processing_upload": UploadOutcome.PENDING,
                "processing_download": UploadOutcome.PENDING,
                "send_to_cdn": UploadOutcome.PENDING,
                "send_to_review": UploadOutcome.PENDING,
                "in_progress": UploadOutcome.PENDING,
                "pending": UploadOutcome.PENDING,
                "queued": UploadOutcome.PENDING,
                "publishing": UploadOutcome.PENDING,
                "failure": UploadOutcome.FAILED,
                "failed": UploadOutcome.FAILED,
            }
            try:
                outcome_value = str(self.outcome).strip().lower()
                self.outcome = status_aliases.get(outcome_value)
                if self.outcome is None:
                    self.outcome = UploadOutcome(outcome_value)
            except ValueError:
                self.outcome = UploadOutcome.PUBLISHED if self.success else UploadOutcome.FAILED
        self.success = self.outcome == UploadOutcome.PUBLISHED
        self.status = self.outcome.value

    @property
    def is_published(self) -> bool:
        """Whether the result proves a publicly published post."""
        return self.outcome == UploadOutcome.PUBLISHED

    @property
    def is_pending(self) -> bool:
        """Whether publication is still processing or unverified."""
        return self.outcome == UploadOutcome.PENDING

    @property
    def terminal(self) -> bool | None:
        """Whether an error is terminal, preserving unknown when unspecified."""
        return None if self.retryable is None else not self.retryable

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result without dropping legacy or outcome fields."""
        result: dict[str, Any] = {
            "success": self.success,
            "outcome": self.status,
            "status": self.status,
            "post_id": self.post_id,
            "post_url": self.post_url,
            "error": self.error,
            "platform": self.platform,
            "metadata": self.metadata,
        }
        if self.retryable is not None:
            result["retryable"] = self.retryable
            result["terminal"] = not self.retryable
        return result


_UPLOAD_PLACEHOLDERS = frozenset({"", "none", "null", "undefined", "unknown", "n/a", "na", "-"})
_UPLOAD_PROCESSING_STATUSES = frozenset(
    {
        "PROCESSING_UPLOAD",
        "PROCESSING_DOWNLOAD",
        "SEND_TO_CDN",
        "SEND_TO_REVIEW",
        "PROCESSING",
        "PENDING",
        "QUEUED",
        "PUBLISHING",
    }
)


def _real_identifier(value: Any) -> str | None:
    """Return a non-placeholder platform identifier, if present."""
    if value is None or isinstance(value, bool):
        return None
    identifier = str(value).strip()
    if not identifier or identifier.lower() in _UPLOAD_PLACEHOLDERS:
        return None
    if "://" in identifier or any(char.isspace() for char in identifier):
        return None
    return identifier


def _public_post_url(value: Any) -> str | None:
    """Return a structurally valid, non-homepage public URL.

    This deliberately validates shape rather than making a network request:
    provider APIs are not reliably reachable during result handling. A root
    platform URL is not a post URL and is rejected for every platform.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or url.lower() in _UPLOAD_PLACEHOLDERS or any(char.isspace() for char in url):
        return None
    try:
        parsed = urlsplit(url)
        # Force validation of malformed ports such as ``example.com:bad``.
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    # A URL with no resource path is a homepage, even when it has a query.
    path = parsed.path.rstrip("/")
    if not path:
        return None
    # Common platform routes are placeholders until their identifier is
    # appended (for example ``/shorts/``). A real watch URL may carry its id
    # in a query, so retain routes that have a query value.
    if path.endswith(("/shorts", "/i/status", "/p", "/reel", "/post", "/video", "/home", "/foryou", "/explore")) and not parsed.query:
        return None
    if path.endswith("/watch") and not any(parse_qs(parsed.query).get("v", [])):
        return None
    return url


def _is_placeholder_post_url(value: Any) -> bool:
    """Whether a URL is a known homepage or empty platform route."""
    if not isinstance(value, str) or not value.strip():
        return False
    url = value.strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    path = parsed.path.rstrip("/")
    if not path:
        return True
    if path.endswith(("/shorts", "/i/status", "/p", "/reel", "/post", "/video", "/home", "/foryou", "/explore")) and not parsed.query:
        return True
    return path.endswith("/watch") and not any(parse_qs(parsed.query).get("v", []))


_UNSET = object()


def _copy_upload_result(
    result: UploadResult,
    *,
    success: bool | object = _UNSET,
    outcome: UploadOutcome | object = _UNSET,
    post_id: str | None | object = _UNSET,
    post_url: str | None | object = _UNSET,
    error: str | None | object = _UNSET,
    platform: str | object = _UNSET,
    metadata: dict[str, Any] | object = _UNSET,
) -> UploadResult:
    """Copy an upload result while retaining all compatibility fields."""
    new_success = result.success if success is _UNSET else bool(success)
    new_outcome = result.outcome if outcome is _UNSET else cast("UploadOutcome | str | None", outcome)
    new_post_id = result.post_id if post_id is _UNSET else cast("str | None", post_id)
    new_post_url = result.post_url if post_url is _UNSET else cast("str | None", post_url)
    new_error = result.error if error is _UNSET else cast("str | None", error)
    new_platform = result.platform if platform is _UNSET else cast("str", platform)
    new_metadata = result.metadata if metadata is _UNSET else cast("dict[str, Any]", metadata)
    return UploadResult(
        success=new_success,
        post_id=new_post_id,
        post_url=new_post_url,
        error=new_error,
        platform=new_platform,
        metadata=new_metadata,
        outcome=new_outcome,
        retryable=result.retryable,
    )


def _unverified_upload(
    result: UploadResult,
    platform: str,
    reason: str,
) -> UploadResult:
    """Turn an unverified success into an explicit failure."""
    metadata = dict(result.metadata)
    metadata.setdefault("normalization", {})
    if isinstance(metadata["normalization"], dict):
        metadata["normalization"].setdefault("reason", reason)
    return UploadResult(
        success=False,
        outcome=UploadOutcome.FAILED,
        post_id=None,
        post_url=None,
        error=result.error or f"UPLOAD_UNVERIFIED: {reason}",
        platform=platform,
        metadata=metadata,
        retryable=False if result.retryable is None else result.retryable,
    )


def normalize_upload_result(raw: Any, platform: str) -> UploadResult:
    """Normalize an adapter result before it can affect posted state.

    The normalizer is intentionally conservative:

    * ``published`` requires a real identifier or a non-homepage public URL.
    * processing results remain ``pending`` and never become success.
    * malformed/placeholder URLs are removed and cannot be emitted.
    * existing error, retryability, terminality (via ``retryable``), and
      metadata are retained for retry/reconciliation consumers.

    Legacy boolean/unsupported adapter returns remain explicit failures because
    a bare acknowledgement does not prove that a public post exists.
    """
    if isinstance(raw, UploadResult):
        result = raw
    elif isinstance(raw, Mapping):
        retryable = raw.get("retryable")
        if retryable is None and isinstance(raw.get("terminal"), bool):
            retryable = not raw["terminal"]
        result = UploadResult(
            success=bool(raw.get("success", False)),
            post_id=raw.get("post_id"),
            post_url=raw.get("post_url"),
            error=raw.get("error"),
            platform=str(raw.get("platform") or platform),
            metadata=dict(raw.get("metadata") or {}),
            outcome=raw.get("outcome", raw.get("status")),
            retryable=retryable,
        )
    elif isinstance(raw, bool):
        return UploadResult(
            success=False,
            outcome=UploadOutcome.FAILED,
            error="UPLOAD_UNVERIFIED: adapter returned no post proof",
            platform=platform,
            metadata={"legacy_result": raw},
            retryable=False,
        )
    else:
        return UploadResult(
            success=False,
            outcome=UploadOutcome.FAILED,
            error="UPLOAD_INVALID_RESULT: adapter returned an unsupported result",
            platform=platform,
            metadata={"raw_type": type(raw).__name__},
            retryable=False,
        )

    result_platform = result.platform or platform
    # Already-posted is a local, verified state shortcut and intentionally has
    # no new provider URL. Keep it compatible with existing dedup consumers.
    if result.metadata.get("already_posted"):
        return result if result_platform == result.platform else _copy_upload_result(result, platform=result_platform)

    # Defend against legacy adapters that reported ``success=True`` while
    # leaving the processing status only in metadata.
    tiktok_status = str(result.metadata.get("status") or "").strip().upper()
    if platform == "tiktok" and tiktok_status in _UPLOAD_PROCESSING_STATUSES:
        return UploadResult(
            success=False,
            outcome=UploadOutcome.PENDING,
            post_id=None,
            post_url=None,
            error=result.error or f"TIKTOK_PUBLISH_PENDING: status={tiktok_status}",
            platform=result_platform,
            metadata=result.metadata,
            retryable=result.retryable,
        )

    public_url = _public_post_url(result.post_url)
    url_was_supplied = isinstance(result.post_url, str) and bool(result.post_url.strip())
    identifier = _real_identifier(result.post_id)
    # TikTok's ``publish_id`` is a processing/container id, not proof of a
    # publicly shareable post. It remains in the result for compatibility only
    # when a valid public URL independently proves publication.
    if platform == "tiktok" and public_url is None and identifier == _real_identifier(result.metadata.get("publish_id")):
        identifier = None

    if result.outcome == UploadOutcome.PENDING:
        if result.post_url is None or public_url is not None:
            if result_platform == result.platform and public_url == result.post_url:
                return result
            return _copy_upload_result(result, platform=result_platform, post_url=public_url)
        return _copy_upload_result(result, platform=result_platform, post_url=None)

    if result.outcome != UploadOutcome.PUBLISHED:
        # Failed results can still carry a provider's diagnostic URL, but never
        # let a malformed/homepage value leak to a caller.
        if url_was_supplied and public_url is None:
            return _copy_upload_result(result, platform=result_platform, post_url=None)
        if result_platform != result.platform:
            return _copy_upload_result(result, platform=result_platform)
        return result

    if url_was_supplied and public_url is None:
        if identifier is None or _is_placeholder_post_url(result.post_url):
            return _unverified_upload(result, platform, "post_url is malformed or a platform homepage")
        # A real platform identifier is sufficient proof when a provider
        # supplies an unusable permalink. Drop the unusable URL rather than
        # allowing it to leak or making a compatible id-only success fail.
        return _copy_upload_result(
            result,
            platform=result_platform,
            post_id=identifier,
            post_url=None,
            outcome=UploadOutcome.PUBLISHED,
            success=True,
        )
    if identifier is None and public_url is None:
        return _unverified_upload(result, platform, "missing published post_id and public post_url")

    # Keep only verified URL data. A real identifier is sufficient for legacy
    # providers that do not return a permalink; callers then receive None for
    # post_url instead of a fabricated homepage.
    if result_platform == result.platform and public_url == result.post_url and identifier == result.post_id:
        return result
    return _copy_upload_result(
        result,
        platform=result_platform,
        post_id=identifier,
        post_url=public_url,
        outcome=UploadOutcome.PUBLISHED,
        success=True,
    )


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
