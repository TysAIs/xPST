"""Canonical modality vocabulary and content-type detection for media files.

One definition of *what kind of file this is* (``video`` vs ``image``) shared by
the media spec (:mod:`xpst.media.specs`), the post preflight
(:mod:`xpst.services.post_preflight`) and every surface that offers a file to a
user or an agent (``/api/media``, the CLI, the MCP preflight tool).

Why it exists: those surfaces previously each re-derived "is this an image?"
from their own extension sets. ``/api/media`` offered image files that
``verify_media`` then hard-rejected, so the app advertised something it could
not post. Anything that decides whether a file is offerable must ask this
module, not a private extension list.
"""

from __future__ import annotations

from pathlib import Path

# Modality labels used across the media spec, the preflight plan, and the API.
MODALITY_VIDEO = "video"
MODALITY_IMAGE = "image"
MODALITIES: tuple[str, ...] = (MODALITY_VIDEO, MODALITY_IMAGE)

# Local file extensions, single source of truth.
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"})
ALL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS


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
    decide on their own policy instead of receiving a wrong modality.
    """
    suffix = normalize_suffix(value)
    if suffix in VIDEO_EXTENSIONS:
        return MODALITY_VIDEO
    if suffix in IMAGE_EXTENSIONS:
        return MODALITY_IMAGE
    return None


__all__ = [
    "ALL_MEDIA_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MODALITIES",
    "MODALITY_IMAGE",
    "MODALITY_VIDEO",
    "VIDEO_EXTENSIONS",
    "detect_modality",
    "normalize_suffix",
]
