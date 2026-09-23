"""Canonical content-type contract for everything xPST can publish.

Before this module the publish pipeline was single-video by construction:
``PlatformUploader.upload(video_path, caption)``, a request of
``{media_paths, caption, platforms}``, and a modality decision made by one
``if len(paths) > 1`` in :mod:`xpst.services.post_service`. A text post had no
way in at all, and a platform's declared ``extra["content"]`` list was never
checked against what the engine can actually do — so an agent could read
"threads supports text" and attempt an operation that cannot work.

This module is the single source of truth for three things:

1. **Vocabulary** — :class:`ContentType` (video, image, carousel, text, thread)
   is the only content-type spelling any surface may use. It is the *publish*
   vocabulary; the older source-side enum (``xpst.sources.base.ContentType``,
   which distinguishes carousel flavours) is translated onto it by
   :func:`content_type_from_source` rather than being reinvented per surface.
2. **Capability** — :data:`DESTINATION_CONTENT_PROFILES` records, per
   destination, which content types are *declared* (in the provider manifest)
   and which are *implemented* (there is real code behind them). Validation
   uses **implemented**, never declared: a declared-but-unimplemented type is
   refused with an explicit error naming the destination.
3. **Requests** — :class:`ContentRequest` is the one typed request object
   shared by the engine, the HTTP API, the CLI and MCP. Its legacy
   ``media_paths``/``caption`` accessors keep pre-contract callers working
   while new callers speak ``media``/``text``/``overrides``.

Nothing here performs I/O, uploads, or network calls; validation is pure and
can run before any uploader is touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from xpst.providers import ProviderRole

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# ── Media classification ────────────────────────────────────────────────────
# Single source for "is this file a video or an image". ``xpst.sources.local``
# re-exports these so the local-file source and the content contract can never
# disagree about what a .webp is.
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"})
ALL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS

# Upper bound on a single post's media list (Instagram's album limit; the CLI
# accepts a repeatable --video for carousels, the web UI sends one).
MAX_MEDIA_ITEMS = 10

MEDIA_KIND_VIDEO = "video"
MEDIA_KIND_IMAGE = "image"
MEDIA_KIND_UNKNOWN = "unknown"


def media_kind(path: str | Path) -> str:
    """Classify one media path as ``video``, ``image`` or ``unknown``.

    Classification is by file extension only — no ffprobe, no I/O — so it is
    safe to call in a side-effect-free preflight.
    """
    suffix = Path(str(path)).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return MEDIA_KIND_VIDEO
    if suffix in IMAGE_EXTENSIONS:
        return MEDIA_KIND_IMAGE
    return MEDIA_KIND_UNKNOWN


def is_remote_media(value: str | Path) -> bool:
    """Whether a media reference is an http(s) URL rather than a local path.

    A destination that fetches media itself can only be given a URL; every
    destination that uploads bytes itself needs a local path. One predicate for
    both directions, so no surface invents its own ``startswith("http")``.
    """
    parsed = urlparse(str(value).strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# ── Vocabulary ──────────────────────────────────────────────────────────────


class ContentType(str, Enum):
    """The content types xPST can reason about.

    ``THREAD`` is a distinct type rather than an alias for ``TEXT`` because X
    declares it by name: a text-only thread has no implementation, and saying
    so explicitly is the point of this contract.
    """

    VIDEO = "video"
    IMAGE = "image"
    CAROUSEL = "carousel"
    TEXT = "text"
    THREAD = "thread"

    @property
    def label(self) -> str:
        """Human-facing noun used in messages: ``"text posts"``."""
        return f"{self.value} posts"


#: Canonical order used by the capability matrix, tests and surfaces.
CONTENT_TYPES: tuple[ContentType, ...] = tuple(ContentType)

#: Accepted spellings mapped onto the canonical vocabulary. Aliases exist so a
#: caller saying "reel" or "photo" is understood rather than rejected, but the
#: canonical ``value`` is the only spelling xPST ever emits.
CONTENT_TYPE_ALIASES: dict[str, ContentType] = {
    "video": ContentType.VIDEO,
    "videos": ContentType.VIDEO,
    "short": ContentType.VIDEO,
    "shorts": ContentType.VIDEO,
    "reel": ContentType.VIDEO,
    "reels": ContentType.VIDEO,
    "image": ContentType.IMAGE,
    "images": ContentType.IMAGE,
    "photo": ContentType.IMAGE,
    "photos": ContentType.IMAGE,
    "picture": ContentType.IMAGE,
    "carousel": ContentType.CAROUSEL,
    "album": ContentType.CAROUSEL,
    "gallery": ContentType.CAROUSEL,
    "text": ContentType.TEXT,
    "status": ContentType.TEXT,
    "post": ContentType.TEXT,
    "tweet": ContentType.TEXT,
    "thread": ContentType.THREAD,
    "threads": ContentType.THREAD,
}


class ContentContractError(ValueError):
    """Base class for content-contract violations."""


class UnknownContentTypeError(ContentContractError):
    """A content-type value is not part of the vocabulary."""

    def __init__(self, value: Any) -> None:
        self.value = value
        super().__init__(
            f"Unknown content type {str(value)!r}: choose one of "
            f"{', '.join(item.value for item in CONTENT_TYPES)}."
        )


class UnsupportedContentTypeError(ContentContractError):
    """A destination cannot publish this content type (names the destination)."""

    def __init__(
        self,
        platform: str,
        content_type: ContentType,
        *,
        supported: Iterable[ContentType] = (),
        declared: bool = False,
        messaging: bool = False,
    ) -> None:
        self.platform = platform
        self.content_type = content_type
        self.supported = tuple(supported)
        self.declared = declared
        self.messaging = messaging
        super().__init__(unsupported_content_message(platform, content_type, supported=self.supported, declared=declared, messaging=messaging))


def unsupported_content_message(
    platform: str,
    content_type: ContentType,
    *,
    supported: Iterable[ContentType] = (),
    declared: bool = False,
    messaging: bool = False,
) -> str:
    """Build the explicit, destination-naming refusal message."""
    supported_list = ", ".join(item.value for item in supported) or "none"
    if messaging:
        return (
            f"{platform} is a messaging destination: it sends DMs and cannot publish "
            f"{content_type.label}. Use a publishing destination instead."
        )
    if declared:
        return (
            f"{platform} does not support {content_type.label}: it is declared in the provider "
            f"capabilities but has no publishing implementation yet. "
            f"Supported content types for {platform}: {supported_list}."
        )
    return (
        f"{platform} does not support {content_type.label}. "
        f"Supported content types for {platform}: {supported_list}."
    )


def coerce_content_type(value: ContentType | str) -> ContentType:
    """Return the canonical :class:`ContentType` for a caller-supplied value.

    Raises:
        UnknownContentTypeError: when the value is not part of the vocabulary.
    """
    if isinstance(value, ContentType):
        return value
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    resolved = CONTENT_TYPE_ALIASES.get(key)
    if resolved is None:
        raise UnknownContentTypeError(value)
    return resolved


def infer_content_type(media: Sequence[str | Path] | None) -> ContentType:
    """Infer the content type from a media list (the pre-contract behaviour).

    * no media -> ``TEXT``
    * one image -> ``IMAGE``
    * one other file -> ``VIDEO`` (any single non-image file was treated as a
      video before this contract existed; keeping that avoids breaking callers)
    * two or more -> ``CAROUSEL``
    """
    items = [str(item).strip() for item in (media or []) if str(item).strip()]
    if not items:
        return ContentType.TEXT
    if len(items) > 1:
        return ContentType.CAROUSEL
    return ContentType.IMAGE if media_kind(items[0]) == MEDIA_KIND_IMAGE else ContentType.VIDEO


#: Source-side classification (``xpst.sources.base.ContentType``) mapped onto the
#: publish vocabulary, keyed by value so this module never imports the sources
#: package. The source enum distinguishes carousel flavours (carousel_video /
#: carousel_image / carousel_mixed); a carousel is a carousel once published.
_SOURCE_CONTENT_TYPE_MAP: dict[str, ContentType] = {
    "video": ContentType.VIDEO,
    "image": ContentType.IMAGE,
    "carousel_video": ContentType.CAROUSEL,
    "carousel_image": ContentType.CAROUSEL,
    "carousel_mixed": ContentType.CAROUSEL,
}


def content_type_from_source(value: Any) -> ContentType:
    """Translate a source-side content type onto the publish vocabulary.

    Raises:
        UnknownContentTypeError: when the value has no publish equivalent.
    """
    key = getattr(value, "value", value)
    resolved = _SOURCE_CONTENT_TYPE_MAP.get(str(key).strip().lower())
    if resolved is None:
        raise UnknownContentTypeError(value)
    return resolved


# ── Capability: declared vs implemented ─────────────────────────────────────


class MediaTransport(str, Enum):
    """How a destination can receive the bytes of a media file.

    This is the second half of a capability question that used to be answered by
    a hard-coded ``platform == "threads"`` check in three places: a destination
    can *support* a content type and still be unable to take the file xPST has,
    because it fetches the media itself instead of accepting an upload.

    ``LOCAL_UPLOAD``
        xPST sends the bytes (multipart, resumable, chunked — whatever the API
        takes). A local file is exactly what is needed.
    ``PUBLIC_URL``
        The destination retrieves the media from a URL over the public
        internet. xPST is a local tool with no server and no CDN, so it cannot
        produce that URL: a local file can never be published to such a
        destination, and saying so *before* any network call is the only honest
        behaviour.
    """

    LOCAL_UPLOAD = "local_upload"
    PUBLIC_URL = "public_url"


#: Stable code for "this destination cannot take the local file you gave it".
#: Reported identically by the engine's content validation, the preflight plan,
#: and the uploader, because all three read it from the profile below.
MEDIA_TRANSPORT_ERROR_CODE = "content_type.media_transport"


@dataclass(frozen=True)
class ContentSupport:
    """One destination's support state for one content type."""

    content_type: ContentType
    declared: bool
    implemented: bool
    note: str = ""

    @property
    def status(self) -> str:
        """``supported`` / ``declared_only`` / ``undeclared``."""
        if self.implemented:
            return "supported"
        return "declared_only" if self.declared else "undeclared"

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_type": self.content_type.value,
            "declared": self.declared,
            "implemented": self.implemented,
            "supported": self.implemented,
            "status": self.status,
            "note": self.note,
        }


@dataclass(frozen=True)
class DestinationContentProfile:
    """A destination's content capabilities: what it claims vs what it can do."""

    platform: str
    display_name: str
    role: ProviderRole
    support: tuple[ContentSupport, ...]
    #: Raw labels exactly as the provider manifest declares them (drift-checked).
    declared_labels: tuple[str, ...] = ()
    note: str = ""
    #: How this destination receives media bytes (see :class:`MediaTransport`).
    media_transport: MediaTransport = MediaTransport.LOCAL_UPLOAD
    #: The exact, user-facing requirement for a destination that fetches media
    #: itself. Empty when xPST uploads the bytes and there is nothing to state.
    media_transport_requirement: str = ""
    #: Stable, machine-readable code every surface reports when a local file is
    #: offered to a destination that cannot fetch one (preflight, engine
    #: validation, uploader). Kept stable so a client can branch on it without
    #: parsing prose.
    media_transport_error_code: str = MEDIA_TRANSPORT_ERROR_CODE

    def _types(self, *, implemented: bool) -> frozenset[ContentType]:
        return frozenset(item.content_type for item in self.support if (item.implemented if implemented else item.declared))

    @property
    def declared(self) -> frozenset[ContentType]:
        """Content types this destination declares in its manifest."""
        return self._types(implemented=False)

    @property
    def implemented(self) -> frozenset[ContentType]:
        """Content types this destination can actually publish today."""
        return self._types(implemented=True)

    @property
    def declared_but_unimplemented(self) -> frozenset[ContentType]:
        """False declarations: claimed in the manifest, no code behind them."""
        return self.declared - self.implemented

    @property
    def implemented_but_undeclared(self) -> frozenset[ContentType]:
        """Real capability the manifest under-declares."""
        return self.implemented - self.declared

    @property
    def is_publishing(self) -> bool:
        """Whether this destination publishes content (vs messaging only)."""
        return self.role is not ProviderRole.MESSAGING

    @property
    def accepts_local_media(self) -> bool:
        """Whether a local file can reach this destination at all.

        False means the destination fetches media from a URL that xPST cannot
        host, so no local file — of any content type — can ever be published
        there. Callers must refuse before any network call, not after.
        """
        return self.media_transport is MediaTransport.LOCAL_UPLOAD

    def supports(self, content_type: ContentType) -> bool:
        """Whether the engine has a real implementation for this content type."""
        return content_type in self.implemented

    def support_for(self, content_type: ContentType) -> ContentSupport | None:
        for item in self.support:
            if item.content_type is content_type:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "role": self.role.value,
            "publishing": self.is_publishing,
            "declared": sorted(item.value for item in self.declared),
            "declared_labels": list(self.declared_labels),
            "implemented": sorted(item.value for item in self.implemented),
            "declared_but_unimplemented": sorted(item.value for item in self.declared_but_unimplemented),
            "implemented_but_undeclared": sorted(item.value for item in self.implemented_but_undeclared),
            "media_transport": self.media_transport.value,
            "accepts_local_media": self.accepts_local_media,
            "media_transport_requirement": self.media_transport_requirement,
            "media_transport_error_code": None if self.accepts_local_media else self.media_transport_error_code,
            "content": [item.to_dict() for item in self.support],
            "note": self.note,
        }


#: Uploader method that must exist (and be overridden) for a content type to
#: count as implemented. Read by the contract tests, which check the real code.
IMPLEMENTATION_METHODS: dict[ContentType, str] = {
    ContentType.VIDEO: "upload",
    ContentType.CAROUSEL: "upload_carousel",
    ContentType.IMAGE: "upload_image",
    ContentType.TEXT: "send_text",
    ContentType.THREAD: "upload_thread",
}

#: Declared manifest labels -> canonical content types. A manifest label that
#: is not here raises at import time rather than being silently ignored.
_LABEL_TO_CONTENT_TYPE: dict[str, ContentType] = {
    "video": ContentType.VIDEO,
    "image": ContentType.IMAGE,
    "carousel": ContentType.CAROUSEL,
    "image_carousel": ContentType.CAROUSEL,
    "text": ContentType.TEXT,
    "thread": ContentType.THREAD,
}


def _build_support(
    declared_labels: Sequence[str],
    implemented: Iterable[ContentType],
    notes: Mapping[ContentType, str] | None = None,
) -> tuple[ContentSupport, ...]:
    """Build a complete support row per content type (no gaps by construction)."""
    declared_types: set[ContentType] = set()
    for label in declared_labels:
        key = str(label).strip().lower()
        if key not in _LABEL_TO_CONTENT_TYPE:
            raise ContentContractError(
                f"Provider manifest declares unknown content label {label!r}; "
                f"add it to _LABEL_TO_CONTENT_TYPE with a canonical ContentType."
            )
        declared_types.add(_LABEL_TO_CONTENT_TYPE[key])
    implemented_types = set(implemented)
    notes = notes or {}
    return tuple(
        ContentSupport(
            content_type=content_type,
            declared=content_type in declared_types,
            implemented=content_type in implemented_types,
            note=notes.get(content_type, ""),
        )
        for content_type in CONTENT_TYPES
    )


def _publish_profile(
    platform: str,
    display_name: str,
    *,
    declared_labels: Sequence[str],
    implemented: Iterable[ContentType],
    notes: Mapping[ContentType, str] | None = None,
    note: str = "",
    media_transport: MediaTransport = MediaTransport.LOCAL_UPLOAD,
    media_transport_requirement: str = "",
    media_transport_error_code: str = MEDIA_TRANSPORT_ERROR_CODE,
) -> DestinationContentProfile:
    return DestinationContentProfile(
        platform=platform,
        display_name=display_name,
        role=ProviderRole.VIDEO_DESTINATION,
        support=_build_support(declared_labels, implemented, notes),
        declared_labels=tuple(declared_labels),
        note=note,
        media_transport=media_transport,
        media_transport_requirement=media_transport_requirement,
        media_transport_error_code=media_transport_error_code,
    )


#: The exact requirement for Threads media, stated once and reported verbatim by
#: the preflight plan, the engine's content validation and the uploader, so no
#: surface can soften it into "will be added separately" or hide it until after
#: a network call has already failed.
#:
#: Grounded in Meta's Threads API docs (2026): creating a container takes
#: ``video_url``/``image_url`` and "Threads retrieves your video from the URL
#: provided, so it must be on a public server". The publishing reference lists
#: only ``POST /{threads-user-id}/threads``, ``threads_publish``, the container
#: status field, repost and delete — there is no binary/resumable upload.
THREADS_PUBLIC_URL_REQUIREMENT = (
    "Threads has no upload endpoint: its API fetches your media from a public server, and xPST is a local "
    "tool with no server to host a file for it. A local file cannot be published to Threads, and xPST "
    "cannot deliver a media URL either yet — post this content from the Threads app instead."
)

#: THE capability table. ``declared_labels`` mirror each provider manifest's
#: ``extra["content"]`` exactly (tests/test_content_contract.py fails if a
#: manifest drifts); ``implemented`` is what the engine can really do today.
DESTINATION_CONTENT_PROFILES: dict[str, DestinationContentProfile] = {
    "youtube": _publish_profile(
        "youtube",
        "YouTube Shorts",
        declared_labels=("video",),
        implemented=(ContentType.VIDEO,),
        notes={
            ContentType.VIDEO: "always forced to Shorts (#shorts appended); no long-form path",
            ContentType.CAROUSEL: "no carousel path: a carousel request is refused by name, and nothing is stitched into a video",
        },
    ),
    "x": _publish_profile(
        "x",
        "X",
        declared_labels=("video", "thread", "image"),
        implemented=(ContentType.VIDEO, ContentType.CAROUSEL, ContentType.IMAGE),
        notes={
            ContentType.THREAD: (
                "declared as `thread`; a text-only thread has no implementation. "
                "Multi-media posting works as a tweet thread and is reported as carousel."
            ),
            ContentType.CAROUSEL: (
                "published as a tweet thread, one media item per tweet (upload_carousel), in the "
                "given order; image items are checked against X's image contract before upload"
            ),
            ContentType.IMAGE: "single image post (upload_image): JPG/PNG/WEBP, ≤ 5 MB, aspect 1:3–3:1",
        },
    ),
    "instagram": _publish_profile(
        "instagram",
        "Instagram Reels",
        declared_labels=("video", "image", "carousel"),
        implemented=(ContentType.VIDEO, ContentType.CAROUSEL, ContentType.IMAGE),
        notes={
            ContentType.IMAGE: (
                "feed photo (upload_image): JPEG only, ≤ 8 MB, aspect within 4:5–1.91:1; "
                "the Graph API path needs a public image URL and refuses a local file explicitly"
            ),
            ContentType.CAROUSEL: (
                "native album upload (2-10 items, order preserved); image items are checked against "
                "Instagram's image contract before the album call; over 10 items or under 2 is refused, "
                "never truncated"
            ),
        },
    ),
    "tiktok": _publish_profile(
        "tiktok",
        "TikTok",
        declared_labels=("video",),
        implemented=(ContentType.VIDEO,),
        note="video path exists but publishing is blocked by TikTok app review; the connection is source-only",
        notes={
            ContentType.CAROUSEL: "no carousel path: a carousel request is refused by name, and nothing is stitched into a video"
        },
    ),
    "threads": _publish_profile(
        "threads",
        "Threads",
        declared_labels=("video", "text"),
        implemented=(ContentType.VIDEO,),
        notes={
            ContentType.VIDEO: (
                "published only from media that is already hosted at a public URL: the Threads API "
                "retrieves the URL itself and has no binary upload, so a local file is refused before "
                "any network call (see media_transport). xPST cannot deliver a media URL to any "
                "uploader yet either, so no media publishing path reaches Threads today"
            ),
            ContentType.TEXT: "declared as `text`; only media_type VIDEO exists, so there is no text path",
            ContentType.CAROUSEL: "no carousel path: a carousel request is refused by name, and nothing is stitched into a video",
        },
        # The Threads API is URL-fetch only (graph.threads.net/{user-id}/threads takes
        # `video_url`/`image_url`; there is no multipart or resumable upload endpoint).
        # xPST is a local tool with no server, so it cannot produce that URL.
        media_transport=MediaTransport.PUBLIC_URL,
        media_transport_requirement=THREADS_PUBLIC_URL_REQUIREMENT,
        media_transport_error_code="THREADS_NEEDS_URL",
        note=(
            "Threads is a URL-fetch destination: it publishes media it can download from a public URL, "
            "never a local file — and xPST cannot hand it a URL yet, so media publishing is refused "
            "today. Text posts would need no URL but have no implementation yet."
        ),
    ),
    "messenger": DestinationContentProfile(
        platform="messenger",
        display_name="Messenger",
        role=ProviderRole.MESSAGING,
        support=_build_support(
            ("text",),
            (ContentType.TEXT,),
            {ContentType.TEXT: "send_text delivers a direct message, not a public post"},
        ),
        declared_labels=("text",),
        note=(
            "messaging destination: DMs only, never a publishing target. Its upload() is a text-DM shim "
            "(the abstract upload contract reused to deliver a message), not a video publish."
        ),
    ),
}

#: Destinations the engine may publish to (messaging providers excluded).
PUBLISH_DESTINATIONS: tuple[str, ...] = tuple(
    platform for platform, profile in DESTINATION_CONTENT_PROFILES.items() if profile.is_publishing
)

SUPPORT_SUPPORTED = "supported"
SUPPORT_UNSUPPORTED = "unsupported"
SUPPORT_UNKNOWN = "unknown"


def content_profile(platform: str) -> DestinationContentProfile | None:
    """Return the capability profile for a destination, or ``None`` if unknown.

    An unknown destination is *not* assumed capable; callers treat it as
    :data:`SUPPORT_UNKNOWN` and let the provider report the real outcome.
    """
    return DESTINATION_CONTENT_PROFILES.get(str(platform).strip().lower())


def declared_content_types(platform: str) -> frozenset[ContentType]:
    """Content types the destination's manifest declares (may be false)."""
    profile = content_profile(platform)
    return profile.declared if profile else frozenset()


def implemented_content_types(platform: str) -> frozenset[ContentType]:
    """Content types the destination can actually publish today."""
    profile = content_profile(platform)
    return profile.implemented if profile else frozenset()


def content_support_status(platform: str, content_type: ContentType) -> str:
    """``supported`` / ``unsupported`` / ``unknown`` for one destination+type."""
    profile = content_profile(platform)
    if profile is None:
        return SUPPORT_UNKNOWN
    if not profile.is_publishing:
        return SUPPORT_UNSUPPORTED
    return SUPPORT_SUPPORTED if profile.supports(content_type) else SUPPORT_UNSUPPORTED


def validate_destination_content(platform: str, content_type: ContentType) -> None:
    """Raise :class:`UnsupportedContentTypeError` unless ``platform`` can publish it.

    Unknown (third-party/plugin) destinations pass: xPST does not fabricate a
    capability it has not declared, and the uploader reports the real outcome.
    """
    profile = content_profile(platform)
    if profile is None:
        return
    if not profile.is_publishing:
        raise UnsupportedContentTypeError(platform, content_type, messaging=True)
    if profile.supports(content_type):
        return
    raise UnsupportedContentTypeError(
        platform,
        content_type,
        supported=sorted(profile.implemented, key=lambda item: item.value),
        declared=content_type in profile.declared,
    )


def media_transport_blocker(platform: str, media: Sequence[str | Path]) -> ContentIssue | None:
    """The one refusal for a local file a destination cannot fetch itself.

    Pure and side-effect free: it decides without touching the network, so it is
    safe to call in a preflight, in request validation, and inside an uploader.

    Returns ``None`` when the destination uploads the bytes itself, when there is
    no media, or when every media item is already a public URL. Otherwise it
    returns the destination profile's *stable code* and its *exact requirement*
    — the same pair the preflight plan and the uploader report, because both call
    this function instead of restating the rule.
    """
    profile = content_profile(platform)
    if profile is None or profile.accepts_local_media:
        return None
    local = [str(item).strip() for item in (media or ()) if str(item).strip() and not is_remote_media(item)]
    if not local:
        return None
    message = profile.media_transport_requirement or (
        f"{profile.display_name} cannot take a local file: it fetches media from a URL you host, "
        f"and xPST has no server to provide one."
    )
    return ContentIssue(
        code=profile.media_transport_error_code,
        message=message,
        severity="error",
        platform=profile.platform,
    )


def media_transport_requirement(platform: str) -> str:
    """The exact requirement for a destination that fetches media itself.

    Empty string for every destination xPST can upload a local file to. Surfaces
    use it to explain the constraint *before* a request is attempted.
    """
    profile = content_profile(platform)
    if profile is None or profile.accepts_local_media:
        return ""
    return profile.media_transport_requirement


def capability_matrix() -> dict[str, Any]:
    """Return the whole declared/implemented matrix as JSON-serializable data.

    Surfaces (CLI/MCP/HTTP/dashboard) read this instead of hand-maintaining a
    per-surface capability list.
    """
    return {
        "content_types": [item.value for item in CONTENT_TYPES],
        "publish_destinations": list(PUBLISH_DESTINATIONS),
        "platforms": {platform: profile.to_dict() for platform, profile in DESTINATION_CONTENT_PROFILES.items()},
    }


# ── Per-destination overrides ───────────────────────────────────────────────


@dataclass(frozen=True)
class DestinationOverride:
    """What one destination gets instead of the request's shared values."""

    text: str | None = None
    content_type: ContentType | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> DestinationOverride:
        """Parse ``{"text": ..., "content_type": ...}`` (a bare string is text)."""
        if payload is None:
            return cls()
        if isinstance(payload, str):
            return cls(text=payload)
        if not isinstance(payload, Mapping):
            raise ContentContractError(f"per-destination override must be an object, got {type(payload).__name__}")
        raw_type = payload.get("content_type")
        content_type: ContentType | None = None
        if raw_type:
            try:
                content_type = coerce_content_type(raw_type)
            except UnknownContentTypeError:
                # Keep the raw value so validation can report it truthfully
                # instead of raising out of a request parser.
                content_type = None
        raw_text = payload.get("text", payload.get("caption"))
        return cls(
            text=None if raw_text is None else str(raw_text),
            content_type=content_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "content_type": self.content_type.value if self.content_type else None,
        }


def parse_destination_texts(raw: Mapping[str, Any] | None) -> dict[str, str]:
    """``{platform: caption}`` from a per-destination override payload.

    Accepts the same two shapes :meth:`DestinationOverride.from_payload` does —
    a bare string (``{"x": "short copy"}``) or an object
    (``{"x": {"text": "short copy"}}``) — and drops entries that carry no text,
    so a caller that falls back to the shared caption keeps the default.
    Raises :class:`ContentContractError` for a value that is neither, so the
    surface can report the bad payload instead of posting something else.
    """
    texts: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return texts
    for name, value in raw.items():
        override = DestinationOverride.from_payload(value)
        key = str(name).strip().lower()
        if key and override.text is not None:
            texts[key] = override.text
    return texts


# ── The one typed request object ────────────────────────────────────────────


@dataclass(frozen=True)
class ContentRequest:
    """A publish request: ``{content_type, media[], text, platforms, overrides}``.

    ``content_type`` is ``None`` when the caller did not state one; the
    effective type is then inferred from ``media`` exactly as the pre-contract
    pipeline did (one file -> video, many -> carousel).
    """

    content_type: ContentType | None = None
    media: tuple[str, ...] = ()
    text: str = ""
    platforms: tuple[str, ...] = ()
    overrides: Mapping[str, DestinationOverride] = field(default_factory=dict)
    #: The caller's raw content-type string, kept only so an unrecognized value
    #: can be reported as a truthful blocker instead of being raised at parse.
    raw_content_type: str | None = None

    # ── Derived values ──────────────────────────────────────────────────

    @property
    def effective_content_type(self) -> ContentType:
        """The content type to act on: explicit when valid, else inferred."""
        if isinstance(self.content_type, ContentType):
            return self.content_type
        if self.content_type:
            try:
                return coerce_content_type(self.content_type)
            except UnknownContentTypeError:
                return infer_content_type(self.media)
        return infer_content_type(self.media)

    @property
    def is_explicit_content_type(self) -> bool:
        """Whether the caller stated a (recognized) content type."""
        return isinstance(self.content_type, ContentType)

    @property
    def media_paths(self) -> list[str]:
        """Legacy accessor: media paths as a JSON-friendly list."""
        return list(self.media)

    @property
    def caption(self) -> str:
        """Legacy accessor: the shared text of the request."""
        return self.text

    @property
    def resolved_media(self) -> tuple[Path, ...]:
        """Media paths resolved for filesystem use."""
        return tuple(Path(item).expanduser() for item in self.media)

    @property
    def media_kinds(self) -> tuple[str, ...]:
        """Classified kind (video/image/unknown) per media item."""
        return tuple(media_kind(item) for item in self.media)

    def override_for(self, platform: str) -> DestinationOverride | None:
        """Return the override for a destination, if one was supplied."""
        key = str(platform).strip().lower()
        for name, override in self.overrides.items():
            if str(name).strip().lower() == key:
                return override
        return None

    def text_for(self, platform: str) -> str:
        """Text for one destination: its override when set, else the shared text."""
        override = self.override_for(platform)
        if override is not None and override.text is not None:
            return override.text
        return self.text

    def per_platform_texts(self, platforms: Sequence[str] | None = None) -> dict[str, str]:
        """Captions that differ from the shared text, keyed by destination.

        Destinations without a text override are *absent* from the mapping, so a
        caller that falls back to :attr:`text` keeps the shared caption for
        every destination the user did not write a specific one for. This is the
        one shape every surface hands to the engine/preflight, so the copy that
        is validated is the copy that is sent.
        """
        names = (
            [str(item) for item in platforms]
            if platforms is not None
            else [str(item) for item in self.platforms]
        )
        captions: dict[str, str] = {}
        for name in names:
            key = str(name).strip().lower()
            if not key:
                continue
            override = self.override_for(key)
            if override is not None and override.text is not None:
                captions[key] = override.text
        return captions

    def content_type_for(self, platform: str) -> ContentType:
        """Content type for one destination: its override when set, else effective."""
        override = self.override_for(platform)
        if override is not None and override.content_type is not None:
            return override.content_type
        return self.effective_content_type

    def to_dict(self) -> dict[str, Any]:
        """Serialize the request (stable keys for CLI/MCP/HTTP/dashboard)."""
        return {
            "content_type": self.content_type.value if self.content_type else None,
            "effective_content_type": self.effective_content_type.value,
            "media": list(self.media),
            "media_paths": list(self.media),
            "text": self.text,
            "caption": self.text,
            "platforms": list(self.platforms),
            "overrides": {name: override.to_dict() for name, override in self.overrides.items()},
        }

    # ── Parsing ─────────────────────────────────────────────────────────

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> ContentRequest:
        """Build a request from a JSON payload, normalizing types.

        Accepts both the typed shape (``content_type``/``media``/``text``/
        ``overrides``) and the legacy shape (``media_paths``/``media_path``/
        ``caption``). Unknown keys are ignored; platform names are lower-cased
        and deduped in request order so response rows match what was asked for.
        """
        data = payload or {}
        raw_media = data.get("media")
        if raw_media is None:
            raw_media = data.get("media_paths")
        if raw_media is None:
            single = data.get("media_path") or data.get("video_path")
            raw_media = [single] if single else []
        if isinstance(raw_media, str):
            raw_media = [raw_media]
        media = tuple(str(item).strip() for item in (raw_media or []) if str(item).strip())[:MAX_MEDIA_ITEMS]

        raw_text = data.get("text")
        if raw_text is None:
            raw_text = data.get("caption")

        seen: dict[str, None] = {}
        for item in data.get("platforms") or []:
            name = str(item).strip().lower()
            if name:
                seen.setdefault(name, None)

        overrides: dict[str, DestinationOverride] = {}
        raw_overrides = (
            data.get("overrides")
            or data.get("per_destination_overrides")
            or data.get("per_platform_captions")
            or {}
        )
        if isinstance(raw_overrides, Mapping):
            for name, value in raw_overrides.items():
                overrides[str(name)] = DestinationOverride.from_payload(value)

        content_type: ContentType | None = None
        raw_content_type: str | None = None
        raw_type = data.get("content_type")
        if raw_type is not None and str(raw_type).strip():
            raw_content_type = str(raw_type).strip()
            try:
                content_type = coerce_content_type(raw_content_type)
            except UnknownContentTypeError:
                content_type = None

        return cls(
            content_type=content_type,
            media=media,
            text="" if raw_text is None else str(raw_text),
            platforms=tuple(seen),
            overrides=overrides,
            raw_content_type=raw_content_type,
        )

    @classmethod
    def from_legacy(
        cls,
        media_paths: Sequence[str | Path] | None = None,
        caption: str = "",
        platforms: Sequence[str] | None = None,
        *,
        content_type: ContentType | str | None = None,
    ) -> ContentRequest:
        """Build a request from pre-contract arguments (``media_paths``/``caption``)."""
        resolved: ContentType | None = None
        raw: str | None = None
        if content_type is not None:
            raw = content_type.value if isinstance(content_type, ContentType) else str(content_type)
            try:
                resolved = coerce_content_type(content_type)
            except UnknownContentTypeError:
                resolved = None
        return cls(
            content_type=resolved,
            media=tuple(str(item).strip() for item in (media_paths or []) if str(item).strip())[:MAX_MEDIA_ITEMS],
            text=caption,
            platforms=tuple(str(item).strip().lower() for item in (platforms or []) if str(item).strip()),
            raw_content_type=raw,
        )


#: Backwards-compatible name: the HTTP API and tests import ``PostRequest``
#: from :mod:`xpst.services.post_service`; there is exactly one type behind it.
PostRequest = ContentRequest


# ── Validation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContentIssue:
    """A stable, machine-readable content-contract finding."""

    code: str
    message: str
    severity: str  # "error" | "warning"
    platform: str | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "platform": self.platform,
        }


#: Per content type: (min media, max media, allowed media kinds).
_MEDIA_RULES: dict[ContentType, tuple[int, int, frozenset[str]]] = {
    ContentType.VIDEO: (1, 1, frozenset({MEDIA_KIND_VIDEO, MEDIA_KIND_UNKNOWN})),
    ContentType.IMAGE: (1, 1, frozenset({MEDIA_KIND_IMAGE})),
    ContentType.CAROUSEL: (2, MAX_MEDIA_ITEMS, frozenset({MEDIA_KIND_VIDEO, MEDIA_KIND_IMAGE, MEDIA_KIND_UNKNOWN})),
    ContentType.TEXT: (0, 0, frozenset()),
    ContentType.THREAD: (0, 0, frozenset()),
}

#: Content types that carry no media and therefore require text.
_TEXT_ONLY: frozenset[ContentType] = frozenset({ContentType.TEXT, ContentType.THREAD})


def _media_issues(content_type: ContentType, media: Sequence[str]) -> list[ContentIssue]:
    minimum, maximum, allowed_kinds = _MEDIA_RULES[content_type]
    count = len(media)
    if content_type in _TEXT_ONLY:
        if count:
            return [
                ContentIssue(
                    code="content_type.media_not_allowed",
                    message=(
                        f"{content_type.label} carry no media (got {count} file"
                        f"{'s' if count != 1 else ''}); use image, carousel or video instead."
                    ),
                    severity="error",
                )
            ]
        return []
    if count < minimum or count > maximum:
        if minimum == maximum:
            expected = f"exactly {minimum} media file"
        else:
            expected = f"between {minimum} and {maximum} media files"
        return [
            ContentIssue(
                code="content_type.media_count",
                message=f"{content_type.label} take {expected} (got {count}).",
                severity="error",
            )
        ]
    issues: list[ContentIssue] = []
    for item in media:
        kind = media_kind(item)
        if kind not in allowed_kinds:
            issues.append(
                ContentIssue(
                    code="content_type.media_kind",
                    message=(
                        f"{content_type.label} need {sorted(allowed_kinds)[0]} files; "
                        f"{Path(item).name} is {kind}."
                    ),
                    severity="error",
                )
            )
    return issues


def validate_content_request(request: ContentRequest) -> tuple[ContentIssue, ...]:
    """Validate a request against the real capability table. Pure, no I/O.

    Returns every finding, in a stable order: request shape first, then one
    entry per destination that cannot take the content type. A destination with
    no declared capability (a third-party plugin) yields a *warning*, never a
    block — xPST does not invent a capability it has not declared.
    """
    issues: list[ContentIssue] = []

    if request.raw_content_type and not isinstance(request.content_type, ContentType):
        issues.append(
            ContentIssue(
                code="content_type.unknown",
                message=(
                    f"Unknown content type {request.raw_content_type!r}: choose one of "
                    f"{', '.join(item.value for item in CONTENT_TYPES)}."
                ),
                severity="error",
            )
        )
        return tuple(issues)

    content_type = request.effective_content_type
    issues.extend(_media_issues(content_type, request.media))

    if content_type in _TEXT_ONLY and not request.text.strip():
        issues.append(
            ContentIssue(
                code="content_type.text_required",
                message=f"{content_type.label} need text: the request carried none.",
                severity="error",
            )
        )

    for name in request.overrides:
        if str(name).strip().lower() not in {platform.lower() for platform in request.platforms}:
            issues.append(
                ContentIssue(
                    code="content_type.override_destination",
                    message=(f"Per-destination overrides were given for {name}, which is not a requested destination."),
                    severity="error",
                    platform=str(name).strip().lower(),
                )
            )

    # An empty target list is the request-shape precondition owned by the
    # preflight service; there is nothing to validate a content type against.
    for platform in request.platforms:
        platform_key = str(platform).strip().lower()
        destination_type = request.content_type_for(platform_key)
        profile = content_profile(platform_key)
        if profile is None:
            issues.append(
                ContentIssue(
                    code="content_type.unknown_destination",
                    message=(
                        f"{platform_key} does not declare content capabilities; "
                        f"xPST will attempt the {destination_type.value} post and report the real outcome."
                    ),
                    severity="warning",
                    platform=platform_key,
                )
            )
            continue
        try:
            validate_destination_content(platform_key, destination_type)
        except UnsupportedContentTypeError as exc:
            issues.append(
                ContentIssue(
                    code="content_type.unsupported",
                    message=str(exc),
                    severity="error",
                    platform=platform_key,
                )
            )
            # A destination that cannot publish this content type at all is
            # already fully explained by that one error; do not stack a media
            # transport complaint on top of it.
            continue

        # Supported content type — but can this destination receive these bytes?
        # Answered from its profile, so it cannot drift from the preflight plan
        # or the uploader, and answered *before* any network call is made.
        transport_issue = media_transport_blocker(platform_key, request.media)
        if transport_issue is not None:
            issues.append(transport_issue)

    return tuple(issues)


def content_blockers(request: ContentRequest) -> list[str]:
    """Just the error messages, in order — the shape preflight consumes."""
    return [issue.message for issue in validate_content_request(request) if issue.is_error]


#: Reported per destination when a content type passes validation for a
#: destination we do not have a capability profile for, but the engine still has
#: no publishing path. Never a silent skip, never a fabricated success.
UNIMPLEMENTED_PUBLISH_ERROR = (
    "{platform} does not support {content_type} posts: the engine has no publishing path for them yet."
)


def blocking_issues(request: ContentRequest) -> list[ContentIssue]:
    """Just the error findings (with codes/platforms) for structured surfaces."""
    return [issue for issue in validate_content_request(request) if issue.is_error]
