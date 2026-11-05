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

# ── Text limits: one number per destination, in one place ───────────────────
#: Maximum characters one post's text may carry, per destination.
#:
#: This is THE single source for the limit. The uploaders import it (so a sender
#: cannot carry a different number than preflight enforces), each provider
#: manifest reports it as ``extra["max_caption_length"]``, and
#: ``tests/test_content_contract.py`` pins both directions — a destination whose
#: sender or manifest disagrees with this table fails the suite rather than
#: silently truncating a post at a number nobody else can see.
#:
#: A destination absent from this mapping has no *verified* limit, so nothing is
#: checked for it: xPST does not invent a character count it has not confirmed.
TEXT_LIMITS: dict[str, int] = {
    "x": 280,
    "threads": 500,
    "instagram": 2200,
    "tiktok": 2200,
    "messenger": 640,
}


def text_limit(platform: str) -> int | None:
    """Return the verified maximum text length for a destination, or ``None``."""
    return TEXT_LIMITS.get(str(platform).strip().lower())


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


class IntegrationStatus(str, Enum):
    """How far a destination's integration can be trusted today.

    ``READY``
        Publishing is implemented and has been exercised against the provider.
    ``INTEGRATION_UNCERTAIN``
        Code exists, but access is gated by the provider (a partner API with no
        self-serve developer console) and no live upload has succeeded yet, so
        the capability is advertised as uncertain, never as ready.
    ``MANUAL_HANDOFF``
        The only reliable path today is exporting the rendition and handing the
        human a link; nothing is uploaded on their behalf.
    """

    READY = "ready"
    INTEGRATION_UNCERTAIN = "integration_uncertain"
    MANUAL_HANDOFF = "manual_handoff"


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
    #: The declared video format envelope (duration/size/aspect/codec), from
    #: :data:`VIDEO_ENVELOPES`. ``None`` for a destination that publishes no
    #: video, so "no numbers" and "no video" stay distinguishable.
    video_envelope: VideoEnvelope | None = None
    #: How far this destination's integration can be trusted today. Anything
    #: other than ``READY`` is stated to the user, never smoothed over.
    integration: IntegrationStatus = IntegrationStatus.READY

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
            "video_envelope": self.video_envelope.to_dict() if self.video_envelope else None,
            "integration": self.integration.value,
            "content": [item.to_dict() for item in self.support],
            "note": self.note,
        }


#: Uploader method that must exist (and be overridden) for a content type to
#: count as implemented. Read by the contract tests, which check the real code.
#: A *messaging* adapter (Messenger) is the exception: its text path is
#: ``send_text`` and delivers a DM, so it is checked by name in
#: ``test_message_destination_implements_only_its_message_path`` instead — a
#: publishing destination implements ``post_text``.
IMPLEMENTATION_METHODS: dict[ContentType, str] = {
    ContentType.VIDEO: "upload",
    ContentType.CAROUSEL: "upload_carousel",
    ContentType.IMAGE: "upload_image",
    ContentType.TEXT: "post_text",
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
    integration: IntegrationStatus = IntegrationStatus.READY,
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
        video_envelope=VIDEO_ENVELOPES.get(platform),
        integration=integration,
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

# ── Video format envelopes: ONE registry, consumed by every surface ────────
#
# A destination's *content* capability ("can it publish video?") and its
# *format* envelope ("how much video?") used to live in three unrelated places:
# the uploader's ``manifest.extra["max_video_duration_seconds"]`` (the upload
# guard), ``xpst.media.specs.PLATFORM_SPECS[...].duration_cap_s`` (the media
# preflight) and prose in the notes. Nothing kept them equal, so changing one
# number left the other two lying — exactly how "YouTube is always a Short"
# survived in three files at once.
#
# This section is the single home for the envelope. Adapters, the media
# preflight and the capability document all read it here:
#
# * ``video_envelope(platform)`` — the declared envelope (numbers + provenance).
# * ``video_duration_limit(platform, mode=...)`` — the number a sender enforces.
# * ``video_envelope_violation(...)`` — the ONE refusal, carrying the envelope
#   and the measured value, so over-envelope input is never truncated silently.
# * ``classify_video_mode(...)`` — the stated short/long threshold.
#
# ``source`` records where each number comes from. A number nobody verified is
# ``None`` (nothing is checked) rather than an invented ceiling.

#: Product classification thresholds (explicit by design — a routing decision,
#: not a hidden heuristic). ≤ 60 s and square-or-taller is short-form;
#: ≥ 180 s or wider-than-tall is long-form; anything between is ambiguous and
#: must be asked about, never guessed.
SHORTS_CLASSIFICATION_MAX_SECONDS = 60
LONG_FORM_CLASSIFICATION_MIN_SECONDS = 180


class VideoMode(str, Enum):
    """The publishing format a video is routed as."""

    SHORTS = "shorts"
    LONG = "long"


def coerce_video_mode(value: VideoMode | str | None) -> VideoMode | None:
    """Return the canonical :class:`VideoMode` (``None`` stays ``None``)."""
    if value is None or isinstance(value, VideoMode):
        return value
    key = str(value).strip().lower()
    for mode in VideoMode:
        if key == mode.value:
            return mode
    raise ContentContractError(f"Unknown video mode {value!r}: choose one of {', '.join(m.value for m in VideoMode)}.")


#: Stable codes an envelope refusal reports, so a client branches on the code
#: instead of parsing prose.
VIDEO_ENVELOPE_DURATION_CODE = "video_envelope.duration"
VIDEO_ENVELOPE_SIZE_CODE = "video_envelope.size"
VIDEO_ENVELOPE_ASPECT_CODE = "video_envelope.aspect"
VIDEO_ENVELOPE_MODE_CODE = "video_envelope.mode"


def format_seconds(seconds: float | int | None) -> str:
    """Exact duration text for a refusal: the number in seconds plus a human form.

    A refusal must carry the number a user can act on, so 140 s is never
    rounded to "2.3 min" (``140 s (2 min 20 s)``), while an unverifiable ceiling
    stays ``unknown`` instead of becoming an invented number.
    """
    if seconds is None:
        return "unknown"
    value = int(round(float(seconds)))
    if value < 60:
        return f"{value} s"
    if value >= 3600:
        hours, remainder = divmod(value, 3600)
        human = f"{hours} h" + (f" {remainder // 60} min" if remainder else "")
    else:
        minutes, remainder = divmod(value, 60)
        human = f"{minutes} min" + (f" {remainder} s" if remainder else "")
    return f"{value} s ({human})"


def _compact_seconds(seconds: float | int | None) -> str:
    """Compact duration for prose: exact seconds under ten minutes, then h/min."""
    if seconds is None:
        return "unknown"
    value = int(round(float(seconds)))
    if value < 600:
        return f"{value} s"
    if value >= 3600:
        hours = value / 3600
        return f"{hours:g} h" if hours == int(hours) else f"{hours:.1f} h"
    minutes = value / 60
    return f"{minutes:g} min" if minutes == int(minutes) else f"{minutes:.1f} min"


def format_bytes(size: int | None) -> str:
    """Human byte ceiling (``256 GB``), binary units; ``unknown`` if None."""
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:g} {unit}" if unit == "B" else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.0f} TB"


@dataclass(frozen=True)
class VideoEnvelope:
    """One destination's declared video format envelope.

    Every field is the number a sender enforces, not a suggestion: see
    :func:`video_envelope_violation` for the refusal built from it.
    """

    platform: str
    display_name: str
    #: Absolute ceiling for any video this destination accepts (``None`` =
    #: nothing verified, so nothing is refused on duration).
    max_duration_seconds: int | None = None
    #: Ceiling when the video is published in its short-form mode (Shorts,
    #: Reels, ...). ``None`` for a destination with no separate short mode.
    shorts_max_duration_seconds: int | None = None
    max_bytes: int | None = None
    containers: tuple[str, ...] = ()
    video_codec: str = ""
    #: Allowed width/height bounds (``None`` = any aspect ratio accepted).
    aspect_min: float | None = None
    aspect_max: float | None = None
    #: Maximum width/height ratio for the short-form mode (``1.0`` = square or
    #: taller than wide, which is what YouTube treats as a Short). ``None`` when
    #: the destination has no short-form aspect rule.
    shorts_max_aspect_ratio: float | None = None
    #: Modes this destination can publish. ``LONG`` only when there is a real
    #: long-form path — not when a short path merely "also works".
    modes: tuple[VideoMode, ...] = (VideoMode.SHORTS,)
    long_form: bool = False
    #: Where the numbers come from (provider docs / measured behaviour).
    source: str = ""
    note: str = ""

    def duration_cap(self, mode: VideoMode | str | None = None) -> int | None:
        """The duration ceiling this destination enforces for ``mode``."""
        resolved = coerce_video_mode(mode)
        if resolved is VideoMode.SHORTS and self.shorts_max_duration_seconds:
            return self.shorts_max_duration_seconds
        return self.max_duration_seconds

    def accepts_duration(self, seconds: float | None, mode: VideoMode | str | None = None) -> bool:
        """Whether ``seconds`` fits; an unmeasurable duration is not refused."""
        cap = self.duration_cap(mode)
        if seconds is None or cap is None:
            return True
        return float(seconds) <= cap

    def supports_mode(self, mode: VideoMode | str) -> bool:
        """Whether this destination has a real path for ``mode``."""
        return coerce_video_mode(mode) in self.modes

    def describe(self, mode: VideoMode | str | None = None) -> str:
        """One-sentence envelope, with the numbers, for refusal messages.

        The absolute ceiling is always stated, and the short-form ceiling is
        named separately when the destination has one — so a Shorts refusal
        cannot read as if YouTube's overall limit were one minute.
        """
        parts = [f"max duration {_compact_seconds(self.max_duration_seconds)}"]
        if self.shorts_max_duration_seconds is not None:
            parts.append(f"short-form mode {_compact_seconds(self.shorts_max_duration_seconds)}")
        parts.append(f"max size {format_bytes(self.max_bytes)}")
        if self.aspect_min is not None and self.aspect_max is not None:
            parts.append(f"aspect {self.aspect_min:.2f}-{self.aspect_max:.2f} (width/height)")
        if self.shorts_max_aspect_ratio is not None:
            parts.append(
                f"short-form needs width/height ≤ {self.shorts_max_aspect_ratio:.2f} (square or vertical)"
            )
        if self.containers:
            parts.append(f"containers {', '.join(self.containers)}")
        if self.video_codec:
            parts.append(f"codec {self.video_codec}")
        parts.append(f"modes {', '.join(m.value for m in self.modes) or 'none'}")
        return f"{self.display_name} video envelope: " + "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON shape (capability document / plan / preflight)."""
        return {
            "platform": self.platform,
            "display_name": self.display_name,
            "max_duration_seconds": self.max_duration_seconds,
            "shorts_max_duration_seconds": self.shorts_max_duration_seconds,
            "max_bytes": self.max_bytes,
            "containers": list(self.containers),
            "video_codec": self.video_codec,
            "aspect_min": self.aspect_min,
            "aspect_max": self.aspect_max,
            "shorts_max_aspect_ratio": self.shorts_max_aspect_ratio,
            "modes": [mode.value for mode in self.modes],
            "long_form": self.long_form,
            "source": self.source,
            "note": self.note,
        }


#: Per-destination video envelopes. The ONLY home for these numbers: the media
#: preflight (``xpst.media.specs``) derives its duration caps from here and
#: every uploader reads its manifest limit from here, so a surface cannot
#: enforce a different number than the registry declares.
VIDEO_ENVELOPES: dict[str, VideoEnvelope] = {
    "youtube": VideoEnvelope(
        platform="youtube",
        display_name="YouTube",
        # YouTube's upload ceiling is 12 hours or 256 GB, whichever comes first.
        max_duration_seconds=43_200,
        shorts_max_duration_seconds=60,
        max_bytes=256 * 1024**3,
        containers=(".mp4", ".mov"),
        video_codec="h264",
        modes=(VideoMode.SHORTS, VideoMode.LONG),
        long_form=True,
        # YouTube only treats a video as a Short when it is square or taller
        # than wide; a landscape upload tagged #Shorts is an ordinary video.
        shorts_max_aspect_ratio=1.0,
        source=(
            "YouTube Data API v3 videos.insert (resumable upload); YouTube upload limits: "
            "12 hours / 256 GB; Shorts: ≤ 60 s, square or taller than wide"
        ),
        note=(
            "Both modes use the same resumable videos.insert call; the mode decides the "
            "title tag, the returned URL, and which ceiling is enforced."
        ),
    ),
    "x": VideoEnvelope(
        platform="x",
        display_name="X",
        # Standard accounts: 2 min 20 s (140 s) of video. X Premium accounts
        # allow longer, which is why the adapter lets config raise the cap.
        max_duration_seconds=140,
        max_bytes=512 * 1024**2,
        containers=(".mp4", ".mov"),
        video_codec="h264",
        aspect_min=1 / 2.39,
        aspect_max=2.39,
        modes=(VideoMode.SHORTS,),
        long_form=False,
        source=(
            "X media upload limits: 140 s of video for standard accounts, 512 MB, "
            "1:2.39-2.39:1; Premium accounts allow longer videos (raise it with "
            "x.max_video_duration_seconds)"
        ),
        note="No long-form path until the account's real cap is raised; over 140 s is refused, never truncated.",
    ),
    "instagram": VideoEnvelope(
        platform="instagram",
        display_name="Instagram Reels",
        max_duration_seconds=900,
        max_bytes=4 * 1024**3,
        containers=(".mp4", ".mov"),
        video_codec="h264",
        modes=(VideoMode.SHORTS,),
        source="Meta Reels API: Reels up to 15 minutes, 4 GB",
    ),
    "tiktok": VideoEnvelope(
        platform="tiktok",
        display_name="TikTok",
        max_duration_seconds=600,
        max_bytes=1024**3,
        containers=(".mp4", ".mov"),
        video_codec="h264",
        modes=(VideoMode.SHORTS,),
        source="TikTok Content Posting API: video up to 10 minutes, 1 GB",
    ),
    "threads": VideoEnvelope(
        platform="threads",
        display_name="Threads",
        max_duration_seconds=300,
        max_bytes=1024**3,
        containers=(".mp4", ".mov"),
        video_codec="h264",
        modes=(VideoMode.SHORTS,),
        source="Threads API: video up to 5 minutes (the container fetches it from a public URL)",
    ),
    "facebook": VideoEnvelope(
        platform="facebook",
        display_name="Facebook Page",
        max_duration_seconds=14_400,
        max_bytes=10 * 1024**3,
        containers=(".mp4", ".mov"),
        video_codec="h264",
        modes=(VideoMode.SHORTS,),
        source="Graph API /{page-id}/videos: up to 4 hours, 10 GB",
    ),
    "rumble": VideoEnvelope(
        platform="rumble",
        display_name="Rumble",
        # Rumble's documented upload limits are per-plan and the API is
        # partner-gated, so xPST refuses nothing it cannot verify.
        max_duration_seconds=None,
        max_bytes=None,
        containers=(".mp4", ".mov"),
        video_codec="h264",
        modes=(VideoMode.LONG,),
        long_form=True,
        source="Rumble video upload API is partner-gated; limits are reported by the service per plan",
        note="Integration-uncertain: no self-serve developer console, so the manual handoff is the reliable path.",
    ),
}


def video_envelope(platform: str) -> VideoEnvelope | None:
    """Return the declared video envelope for a destination, or ``None``."""
    return VIDEO_ENVELOPES.get(str(platform).strip().lower())


def video_duration_limit(platform: str, mode: VideoMode | str | None = None) -> int | None:
    """The duration ceiling a sender must enforce for ``platform``.

    This is the number every adapter's manifest reports and the media preflight
    compares against — one source, so ``xpst capabilities``, the preflight and
    the upload guard can never disagree.
    """
    envelope = video_envelope(platform)
    return None if envelope is None else envelope.duration_cap(mode)


def video_envelope_violation(
    platform: str,
    *,
    duration_s: float | None = None,
    size_bytes: int | None = None,
    width: int | None = None,
    height: int | None = None,
    mode: VideoMode | str | None = None,
) -> ContentIssue | None:
    """The one refusal for input outside a destination's video envelope.

    Pure and side-effect free (no ffprobe, no network), so the preflight, the
    uploader and a route plan can all call it before anything is published.
    Returns ``None`` when the destination is unknown or the input fits; ``None``
    is never "assume it fits" for a value that was actually measured.
    """
    return envelope_violation(
        video_envelope(platform),
        duration_s=duration_s,
        size_bytes=size_bytes,
        width=width,
        height=height,
        mode=mode,
    )


def envelope_violation(
    envelope: VideoEnvelope | None,
    *,
    duration_s: float | None = None,
    size_bytes: int | None = None,
    width: int | None = None,
    height: int | None = None,
    mode: VideoMode | str | None = None,
) -> ContentIssue | None:
    """The refusal for one concrete envelope (``None`` envelope = no refusal).

    Split from :func:`video_envelope_violation` so an adapter that knows its
    account's REAL ceiling (a Premium X account, say) can enforce a
    ``dataclasses.replace``-adjusted envelope and still speak one vocabulary.
    """
    if envelope is None:
        return None
    resolved_mode = coerce_video_mode(mode)
    if resolved_mode is not None and not envelope.supports_mode(resolved_mode):
        return ContentIssue(
            code=VIDEO_ENVELOPE_MODE_CODE,
            message=(
                f"{envelope.display_name} has no {resolved_mode.value}-form path "
                f"(modes: {', '.join(m.value for m in envelope.modes) or 'none'}). "
                f"{envelope.describe()}"
            ),
            severity="error",
            platform=envelope.platform,
        )
    cap = envelope.duration_cap(resolved_mode)
    if duration_s is not None and cap is not None and float(duration_s) > cap:
        label = "Short-form" if resolved_mode is VideoMode.SHORTS else "Video"
        return ContentIssue(
            code=VIDEO_ENVELOPE_DURATION_CODE,
            message=(
                f"{label} is {format_seconds(duration_s)} — over {envelope.display_name}'s "
                f"{format_seconds(cap)} limit. {envelope.describe(resolved_mode)} "
                f"Refused rather than truncated."
            ),
            severity="error",
            platform=envelope.platform,
        )
    if size_bytes is not None and envelope.max_bytes is not None and int(size_bytes) > envelope.max_bytes:
        return ContentIssue(
            code=VIDEO_ENVELOPE_SIZE_CODE,
            message=(
                f"File is {format_bytes(int(size_bytes))} — over {envelope.display_name}'s "
                f"{format_bytes(envelope.max_bytes)} limit. {envelope.describe(resolved_mode)}"
            ),
            severity="error",
            platform=envelope.platform,
        )
    measured_aspect = float(width) / float(height) if (width and height) else None
    if (
        measured_aspect is not None
        and resolved_mode is VideoMode.SHORTS
        and envelope.shorts_max_aspect_ratio is not None
        and measured_aspect > envelope.shorts_max_aspect_ratio
    ):
        return ContentIssue(
            code=VIDEO_ENVELOPE_ASPECT_CODE,
            message=(
                f"{width}x{height} is aspect {measured_aspect:.2f} — a {envelope.display_name} "
                f"short must be square or vertical (width/height ≤ "
                f"{envelope.shorts_max_aspect_ratio:.2f}). Publish it as long-form, or export a "
                f"vertical rendition. {envelope.describe(resolved_mode)}"
            ),
            severity="error",
            platform=envelope.platform,
        )
    if (
        measured_aspect is not None
        and envelope.aspect_min is not None
        and envelope.aspect_max is not None
    ):
        if measured_aspect < envelope.aspect_min or measured_aspect > envelope.aspect_max:
            return ContentIssue(
                code=VIDEO_ENVELOPE_ASPECT_CODE,
                message=(
                    f"{width}x{height} is aspect {measured_aspect:.2f} — outside {envelope.display_name}'s "
                    f"{envelope.aspect_min:.2f}-{envelope.aspect_max:.2f} range. "
                    f"{envelope.describe(resolved_mode)}"
                ),
                severity="error",
                platform=envelope.platform,
            )
    return None


def classify_video_mode(
    duration_s: float | None,
    width: int | None = None,
    height: int | None = None,
) -> VideoMode | None:
    """Classify a measured video as short-form, long-form, or ambiguous.

    The thresholds are the product decision stated at the top of this section
    (:data:`SHORTS_CLASSIFICATION_MAX_SECONDS`,
    :data:`LONG_FORM_CLASSIFICATION_MIN_SECONDS`). ``None`` means "cannot tell"
    — the caller must ask, never guess.
    """
    if duration_s is None:
        return None
    duration = float(duration_s)
    vertical = None if not width or not height else height >= width
    if duration <= SHORTS_CLASSIFICATION_MAX_SECONDS and vertical is not False:
        return VideoMode.SHORTS
    if duration >= LONG_FORM_CLASSIFICATION_MIN_SECONDS or vertical is False:
        return VideoMode.LONG
    return None


#: THE capability table. ``declared_labels`` mirror each provider manifest's
#: ``extra["content"]`` exactly (tests/test_content_contract.py fails if a
#: manifest drifts); ``implemented`` is what the engine can really do today.
DESTINATION_CONTENT_PROFILES: dict[str, DestinationContentProfile] = {
    "youtube": _publish_profile(
        "youtube",
        "YouTube",
        declared_labels=("video",),
        implemented=(ContentType.VIDEO,),
        notes={
            ContentType.VIDEO: (
                "two real modes: short-form (Shorts, ≤ 60 s, square-or-vertical, '#Shorts' tag, "
                "/shorts/ URL) and long-form (resumable videos.insert, watch URL, up to 12 h / 256 GB). "
                "The mode is explicit (--shorts/--long) or derived from the media probe; a video that "
                "fits neither is refused with both ceilings in the message."
            ),
            ContentType.CAROUSEL: "no carousel path: a carousel request is refused by name, and nothing is stitched into a video",
        },
    ),
    "x": _publish_profile(
        "x",
        "X",
        declared_labels=("video", "carousel", "text", "image"),
        implemented=(ContentType.VIDEO, ContentType.CAROUSEL, ContentType.TEXT, ContentType.IMAGE),
        notes={
            ContentType.TEXT: "one text post (280 characters max); no media, published via post_text",
            ContentType.THREAD: (
                "not declared: a text-only thread has no sender. Multi-media posting "
                "works as a tweet thread and is reported as carousel."
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
        implemented=(ContentType.VIDEO, ContentType.TEXT),
        notes={
            ContentType.TEXT: "one text post (500 characters max) via the TEXT container, no media needed",
            ContentType.VIDEO: (
                "published only from media that is already hosted at a public URL: the Threads API "
                "retrieves the URL itself and has no binary upload, so a local file is refused before "
                "any network call (see media_transport). xPST cannot deliver a media URL to any "
                "uploader yet either, so no media publishing path reaches Threads today"
            ),
            ContentType.CAROUSEL: "no carousel path: a carousel request is refused by name, and nothing is stitched into a video",
        },
        # The Threads API is URL-fetch only (graph.threads.net/{user-id}/threads takes
        # `video_url`/`image_url`; there is no multipart or resumable upload endpoint).
        # xPST is a local tool with no server, so it cannot produce that URL.
        media_transport=MediaTransport.PUBLIC_URL,
        media_transport_requirement=THREADS_PUBLIC_URL_REQUIREMENT,
        media_transport_error_code="THREADS_NEEDS_URL",
        note=(
            "Threads is a URL-fetch destination for media: it publishes what it can download from a "
            "public URL, never a local file — and xPST cannot hand it a URL yet, so media publishing "
            "is refused today. Text posts need no URL and are published verbatim through a "
            "media_type TEXT container."
        ),
    ),
    "facebook": _publish_profile(
        "facebook",
        "Facebook Page",
        declared_labels=("video",),
        implemented=(ContentType.VIDEO,),
        note=(
            "Page-scoped: publishes as a Facebook Page, never as a personal profile "
            "(there is no API for that). Feed video today; Reels / photo / text are "
            "the follow-up that extends this row."
        ),
    ),
    "rumble": _publish_profile(
        "rumble",
        "Rumble",
        declared_labels=("video",),
        implemented=(ContentType.VIDEO,),
        notes={
            ContentType.VIDEO: (
                "long-form only (Rumble is a video platform, not a Shorts surface). Two paths: "
                "the partner upload API when an API key is configured, and a MANUAL HANDOFF "
                "otherwise — the rendition and a caption file are written to a directory you "
                "choose, and the adapter reports the handoff instead of pretending it uploaded."
            )
        },
        note=(
            "integration-uncertain: Rumble's upload API is partner-gated with no self-serve "
            "developer console comparable to Meta/Google. xPST declares the capability and both "
            "paths, but does not advertise Rumble as ready until one live upload succeeds."
        ),
        integration=IntegrationStatus.INTEGRATION_UNCERTAIN,
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


def format_route(content_type: ContentType | str) -> tuple[str, ...]:
    """Destinations whose *implemented* capability accepts ``content_type``.

    This is the format half of the R4 route (``format route ∩ connected
    accounts``). It is derived from :data:`DESTINATION_CONTENT_PROFILES` — never
    a second capability table — so a platform that gains (or loses) a content
    type changes the route and ``xpst capabilities`` in the same edit.

    Order follows :data:`PUBLISH_DESTINATIONS`, so a plan reads the same way on
    every surface. A messaging-only destination is never a publish route.
    """
    try:
        wanted = coerce_content_type(content_type)
    except UnknownContentTypeError:
        return ()
    return tuple(
        platform
        for platform in PUBLISH_DESTINATIONS
        if (profile := content_profile(platform)) is not None and profile.supports(wanted)
    )


def format_routes() -> dict[str, list[str]]:
    """The whole format→destinations map, derived on demand (no stored copy)."""
    return {item.value: list(format_route(item)) for item in CONTENT_TYPES}


def destination_envelope(platform: str) -> dict[str, Any] | None:
    """The publishing envelope for one destination, or ``None`` if unknown.

    One object carries everything a route decision needs — accepted content
    types, caption ceiling, media transport, and (from
    :data:`xpst.media.specs.PLATFORM_SPECS`, the ingest matrix the uploader
    already enforces) video duration/container/codec/byte limits and image
    limits. It is emitted by ``xpst capabilities`` and consumed by the route
    resolver, so a plan can never advertise an envelope the uploader ignores.

    ``media.specs`` imports this module, so the spec import is function-local
    to avoid an import cycle.
    """
    name = str(platform).strip().lower()
    profile = content_profile(name)
    if profile is None:
        return None
    from xpst.media.specs import PLATFORM_SPECS

    spec = PLATFORM_SPECS.get(name)
    implemented = sorted(item.value for item in profile.implemented)
    envelope: dict[str, Any] = {
        "platform": name,
        "display_name": profile.display_name,
        "publishing": profile.is_publishing,
        "content_types": {
            "implemented": implemented,
            "declared": sorted(item.value for item in profile.declared),
        },
        "caption": {"max_characters": text_limit(name)},
        "media_transport": profile.media_transport.value,
        "media_transport_requirement": profile.media_transport_requirement or None,
        "video": None,
        "image": None,
    }
    if spec is not None:
        if ContentType.VIDEO.value in implemented:
            video_bytes: int | None = (
                spec.file_size_cap_mb * 1024 * 1024 if spec.file_size_cap_mb is not None else None
            )
            envelope["video"] = {
                "max_duration_seconds": spec.duration_cap_s,
                "containers": list(spec.containers),
                "video_codec": spec.video_codec,
                "pix_fmt": spec.pix_fmt,
                "max_bytes": video_bytes,
                "max_long_edge": spec.long_edge,
                "fps_cap": spec.fps_cap,
                "audio_codec": spec.audio_codec,
            }
        if ContentType.IMAGE.value in implemented:
            image_mb = spec.image_size_cap_mb()
            image_bytes: int | None = image_mb * 1024 * 1024 if image_mb is not None else None
            envelope["image"] = {
                "containers": list(spec.image_containers),
                "max_bytes": image_bytes,
                "aspect_min": spec.image_aspect_min,
                "aspect_max": spec.image_aspect_max,
            }
    return envelope


def capability_document() -> dict[str, Any]:
    """THE capability document, from one source, for every surface.

    The CLI (``xpst capabilities``), the MCP tool (``xpst_capabilities``) and
    the HTTP API (``GET /api/capabilities``) all return this mapping verbatim —
    so the list a human reads and the list an agent plans against cannot drift,
    and there is exactly one place to add a capability.
    """
    matrix = capability_matrix()
    return {
        "contract_version": 2,
        "content_types": matrix["content_types"],
        "publish_destinations": matrix["publish_destinations"],
        "platforms": matrix["platforms"],
        # The format→destination route (R4) and the per-destination envelope
        # (duration/aspect/codec/bytes/caption) are emitted here so the CLI, the
        # MCP tool, the UI and the route resolver read the same numbers. The
        # resolver imports this module; nothing re-declares an envelope.
        "format_routes": format_routes(),
        "envelopes": {
            platform: destination_envelope(platform)
            for platform in DESTINATION_CONTENT_PROFILES
        },
        # The publishing path each content type takes once its media requirement
        # is met. A type that is absent here has no path yet, which is why it is
        # absent from every destination's ``implemented`` list too.
        "publish_routes": {
            item.value: _ROUTE_BY_CONTENT_TYPE.get(item, PUBLISH_ROUTE_UNIMPLEMENTED) for item in CONTENT_TYPES
        },
        # No destination may declare a content type it cannot publish. This is
        # the assertion the surfaces (and the test suite) read as data.
        "declared_but_unimplemented": {
            platform: sorted(item.value for item in profile.declared_but_unimplemented)
            for platform, profile in DESTINATION_CONTENT_PROFILES.items()
            if profile.declared_but_unimplemented
        },
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
        # Whether the caller used the *typed* spelling of a body. `caption` is the
        # legacy spelling (a caption for media) and keeps its old meaning below.
        typed_text = raw_text is not None and str(raw_text).strip()
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
        elif typed_text and not media:
            # The typed spelling of a text post: `text` with no file and no stated
            # type *is* a text post, not a file-less video request. Resolved here,
            # in the one request parser, so no surface has to guess it (the legacy
            # `caption` key keeps its old meaning: a caption for media).
            content_type = ContentType.TEXT

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
        overrides: Mapping[str, Any] | None = None,
    ) -> ContentRequest:
        """Build a request from pre-contract arguments (``media_paths``/``caption``).

        ``overrides`` is the per-destination copy a surface parsed from
        ``--caption-for`` (CLI) or ``overrides`` (MCP/HTTP). It must be carried
        on the request: the copy that is *validated* has to be the copy that is
        *sent*, or a valid short X caption is refused because the shared
        YouTube-length body is what the gate read (the 1.3.0 release post could
        not be published to X and YouTube together).
        """
        resolved: ContentType | None = None
        raw: str | None = None
        if content_type is not None:
            raw = content_type.value if isinstance(content_type, ContentType) else str(content_type)
            try:
                resolved = coerce_content_type(content_type)
            except UnknownContentTypeError:
                resolved = None
        typed_overrides: dict[str, DestinationOverride] = {}
        for name, value in (overrides or {}).items():
            key = str(name).strip().lower()
            if not key:
                continue
            override = (
                value
                if isinstance(value, DestinationOverride)
                else DestinationOverride.from_payload(value)
            )
            if override.text is not None or override.content_type is not None:
                typed_overrides[key] = override
        return cls(
            content_type=resolved,
            media=tuple(str(item).strip() for item in (media_paths or []) if str(item).strip())[:MAX_MEDIA_ITEMS],
            text=caption,
            platforms=tuple(str(item).strip().lower() for item in (platforms or []) if str(item).strip()),
            overrides=typed_overrides,
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


def _text_issues(request: ContentRequest) -> list[ContentIssue]:
    """Per-destination text findings: missing text, or text past the limit.

    The limit comes from :data:`TEXT_LIMITS` — the same number the sender
    imports — so a post is *refused with the destination and the limit named*
    instead of being silently truncated to a length nobody can see. A messaging
    destination is skipped here: it is refused for what it is, not for its size.
    """
    issues: list[ContentIssue] = []
    for platform in request.platforms:
        key = str(platform).strip().lower()
        profile = content_profile(key)
        if profile is not None and not profile.is_publishing:
            continue
        content_type = request.content_type_for(key)
        body = request.text_for(key)
        if content_type in _TEXT_ONLY and not body.strip():
            issues.append(
                ContentIssue(
                    code="content_type.text_required",
                    message=f"{content_type.label} for {key} need text: the request carried none.",
                    severity="error",
                    platform=key,
                )
            )
            continue
        limit = text_limit(key)
        if limit is not None and len(body) > limit:
            advice = (
                "shorten the text"
                if request.override_for(key) is not None
                else f"shorten the text, or give {key} its own shorter text with a per-destination override"
            )
            issues.append(
                ContentIssue(
                    code="content_type.text_too_long",
                    message=(
                        f"{key} accepts at most {limit} characters per post; this one has "
                        f"{len(body)}. xPST will not truncate it — {advice}."
                    ),
                    severity="error",
                    platform=key,
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
    issues.extend(_text_issues(request))

    for name in request.overrides:
        override_key = str(name).strip().lower()
        if override_key not in {platform.lower() for platform in request.platforms}:
            issues.append(
                ContentIssue(
                    code="content_type.override_destination",
                    message=(f"Per-destination overrides were given for {name}, which is not a requested destination."),
                    severity="error",
                    platform=override_key,
                )
            )
            continue
        # Per-destination copy is honoured on every route that can carry it: the
        # text route reads its own text per destination (engine.post_text), and
        # the media routes pass the per-destination caption down to each
        # uploader (engine.post_manual / post_manual_carousel). There is
        # therefore nothing to warn about — an override that reached this point
        # is validated and sent.

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


# ── The surface-facing verdict and route ────────────────────────────────────


def content_verdict(request: ContentRequest) -> dict[str, Any]:
    """The ONE content answer every surface reports for a request.

    CLI, MCP, HTTP and the dashboard all embed this mapping (or the fields it
    carries) instead of re-deriving a content type, re-checking a destination
    or re-writing a refusal message. Identical inputs therefore produce
    byte-identical blockers on every surface; ``tests/test_content_surface_parity.py``
    asserts exactly that.

    Returns:
        ``{content_type, effective_content_type, ok, blockers, warnings,
        content_issues, overrides, route}`` — JSON-serializable, no I/O.
    """
    issues = validate_content_request(request)
    return {
        "content_type": request.content_type.value if isinstance(request.content_type, ContentType) else None,
        "effective_content_type": request.effective_content_type.value,
        "ok": not any(issue.is_error for issue in issues),
        "blockers": [issue.message for issue in issues if issue.is_error],
        "warnings": [issue.message for issue in issues if not issue.is_error],
        "content_issues": [issue.to_dict() for issue in issues],
        "overrides": {name: override.to_dict() for name, override in request.overrides.items()},
        "route": publish_route(request),
    }


#: Publishing paths a validated request can take. ``UNIMPLEMENTED`` is a real
#: answer, not an error: it is how a request that passes capability validation
#: (a plugin destination, or a type no uploader implements yet) is reported
#: instead of being uploaded "as a video just in case".
PUBLISH_ROUTE_VIDEO = "video"
PUBLISH_ROUTE_IMAGE = "image"
PUBLISH_ROUTE_CAROUSEL = "carousel"
PUBLISH_ROUTE_TEXT = "text"
PUBLISH_ROUTE_UNIMPLEMENTED = "unimplemented"

#: The publishing path a content type takes once its media requirement is met.
#: A content type that is absent has no publishing path at all.
_ROUTE_BY_CONTENT_TYPE: dict[ContentType, str] = {
    ContentType.VIDEO: PUBLISH_ROUTE_VIDEO,
    ContentType.IMAGE: PUBLISH_ROUTE_IMAGE,
    ContentType.CAROUSEL: PUBLISH_ROUTE_CAROUSEL,
    ContentType.TEXT: PUBLISH_ROUTE_TEXT,
}


def publish_route(request: ContentRequest) -> str:
    """The ONE modality→publishing-path decision shared by every surface.

    Before the content contract this decision was ``if len(paths) > 1`` inside
    the posting service, with the CLI and MCP each re-deriving it from their own
    argument shapes. It is now a function of the request's effective content
    type, so all surfaces route a request the same way.
    """
    content_type = request.effective_content_type
    media = [str(item) for item in request.media if str(item).strip()]
    route = _ROUTE_BY_CONTENT_TYPE.get(content_type)
    if route == PUBLISH_ROUTE_VIDEO and len(media) == 1:
        return route
    if route == PUBLISH_ROUTE_IMAGE and len(media) == 1:
        return route
    if route == PUBLISH_ROUTE_CAROUSEL and len(media) >= 2:
        return route
    if route == PUBLISH_ROUTE_TEXT and not media:
        return route
    return PUBLISH_ROUTE_UNIMPLEMENTED
