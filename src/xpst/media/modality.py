"""Backward-compatibility shim: modality vocabulary now lives in :mod:`xpst.content`.

PR #203 made ``xpst.content`` the single source of truth for the publish
contract, including the media extension sets and the video/image kind
classification (``MEDIA_KIND_*``, ``media_kind``). The media-spec truth work
initially grew its own module here; to avoid two competing vocabularies this
module re-exports the content-contract names under their historical
``MODALITY_*`` aliases, plus the helpers the media spec and its callers need
(a ``None``-returning detector and a dotted-suffix normalizer).

New code should import from :mod:`xpst.content` (extensions, kinds) and
:mod:`xpst.media.specs` (destination capability per modality).
"""

from __future__ import annotations

from pathlib import Path

from xpst.content import (
    ALL_MEDIA_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    VIDEO_EXTENSIONS,
)

# Historical aliases — new code uses xpst.content.MEDIA_KIND_*.
MODALITY_VIDEO = MEDIA_KIND_VIDEO
MODALITY_IMAGE = MEDIA_KIND_IMAGE
MODALITIES: tuple[str, ...] = (MODALITY_VIDEO, MODALITY_IMAGE)

_KNOWN_MODALITIES = frozenset({MODALITY_VIDEO, MODALITY_IMAGE})


def normalize_suffix(value: str | Path) -> str:
    """Return a lower-cased dotted suffix for a path or a bare suffix.

    >>> normalize_suffix("clip.MP4")
    '.mp4'
    >>> normalize_suffix(Path("photo.jpeg"))
    '.jpeg'
    """
    raw = str(value).strip()
    # A bare suffix (".jpg") is a hidden filename to pathlib, so handle it first.
    if raw.startswith(".") and raw.count(".") == 1:
        return raw.lower()
    return Path(raw).suffix.lower()


def detect_modality(value: str | Path) -> str | None:
    """Classify a path (or bare suffix) as ``video``/``image``.

    Returns ``None`` for anything that is not a known media file so callers can
    decide on their own policy instead of receiving a wrong modality. (This is
    the ``None``-for-unknown variant; ``xpst.content.media_kind`` returns the
    string ``"unknown"`` instead.)
    """
    suffix = normalize_suffix(value)
    if suffix in VIDEO_EXTENSIONS:
        return MODALITY_VIDEO
    if suffix in IMAGE_EXTENSIONS:
        return MODALITY_IMAGE
    return None


def modality_is_known(value: str) -> bool:
    """Whether ``value`` is one of the canonical modality labels."""
    return value in _KNOWN_MODALITIES


__all__ = [
    "ALL_MEDIA_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MODALITIES",
    "MODALITY_IMAGE",
    "MODALITY_VIDEO",
    "VIDEO_EXTENSIONS",
    "detect_modality",
    "modality_is_known",
    "normalize_suffix",
]
