"""Media preview helpers for the composer (streaming + thumbnails).

The composer must show what will be posted *before* it is posted. The naive
implementation of that (read the file, hand the bytes to the UI) breaks the
product's own promise: a 2 GB video would be copied into the engine's memory
and into the webview before the user ever pressed Post.

This module is the whole preview path, and it is built so that never happens:

* :func:`iter_file_chunks` reads the source file in bounded chunks
  (:data:`DEFAULT_CHUNK_SIZE`), so the engine holds at most one chunk of a
  video at a time no matter how large the file is.
* :func:`parse_byte_range` implements RFC 7233 for a single range, which is
  what makes the served video *playable and seekable* — the webview pulls only
  the byte ranges it needs, exactly like a remote stream.
* :func:`thumbnail_for` shells out to ffmpeg for exactly one scaled frame
  (``-ss`` before ``-i``, so a video thumbnail decodes one frame rather than
  walking the file) and caches the JPEG on disk under
  ``~/.xpst/cache/previews``. When ffmpeg is absent the caller gets ``None``
  and the UI falls back to streaming the original — a missing ffmpeg degrades
  the preview, it does not break the app.

Nothing here is read into a ``bytes`` object belonging to the *source* file:
the only whole-file read in this module is of the generated thumbnail, which
is a bounded, locally produced JPEG.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.sources.local import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from xpst.utils.logger import get_logger
from xpst.utils.platform import resolve_ffmpeg_path

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger(__name__)

#: Bytes handed to the transport per read. Bounds engine memory for a preview
#: of any file size; 1 MiB keeps a 4K stream fed without a syscall per packet.
DEFAULT_CHUNK_SIZE = 1024 * 1024

#: Longest edge of a generated thumbnail, in pixels.
DEFAULT_THUMBNAIL_WIDTH = 640

#: ffmpeg is given this long for one frame before the preview falls back.
THUMBNAIL_TIMEOUT_S = 30.0


class MediaPathError(ValueError):
    """The requested preview path is not a servable absolute media file."""


def media_content_type(path: str | Path) -> str:
    """Return the MIME type to serve for a media path (extension-driven)."""
    suffix = Path(path).suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".flv": "video/x-flv",
        ".wmv": "video/x-ms-wmv",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
    }.get(suffix, "application/octet-stream")


def is_previewable_name(path: str | Path) -> bool:
    """True when the extension is one the composer can render."""
    return Path(path).suffix.lower() in (VIDEO_EXTENSIONS | IMAGE_EXTENSIONS)


def resolve_media_path(raw: str | Path) -> Path:
    """Validate a preview request and return the resolved file path.

    The dashboard is loopback-only and already accepts arbitrary media paths
    on ``POST /api/post``, so the trust model here is the same one: any
    absolute path the user's own account can read. What is *not* accepted is
    anything that is not a real, previewable file — a directory, a missing
    path, a non-media extension, or a relative path that would be resolved
    against the server's working directory.

    Raises:
        MediaPathError: with an actionable message for the response body.
    """
    text = str(raw or "").strip()
    if not text:
        raise MediaPathError("No media path was given.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise MediaPathError("The media path must be absolute.")
    if not is_previewable_name(candidate):
        raise MediaPathError(f"Not a previewable media file: {candidate.name}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise MediaPathError(f"Media file not found: {candidate}") from None
    if not resolved.is_file():
        raise MediaPathError(f"Not a file: {resolved}")
    return resolved


def parse_byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range`` header against a file of ``size`` bytes.

    Returns an inclusive ``(start, end)`` pair, or ``None`` when the header is
    absent/unparseable/multi-range (the caller then serves the whole file).

    Raises:
        ValueError: when the range is syntactically valid but unsatisfiable
            (start beyond EOF); callers answer 416, which is what stops a
            player from silently receiving the whole file it did not ask for.
    """
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes=") :].strip()
    if "," in spec:  # multi-range: serve the whole file instead of guessing
        return None
    if "-" not in spec:
        return None
    raw_start, _, raw_end = spec.partition("-")
    raw_start, raw_end = raw_start.strip(), raw_end.strip()
    if not raw_start and not raw_end:
        return None
    if size <= 0:
        raise ValueError("empty file")
    try:
        if not raw_start:  # suffix range: last N bytes
            suffix = int(raw_end)
            if suffix <= 0:
                raise ValueError("empty suffix range")
            start = max(0, size - suffix)
            return start, size - 1
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    except ValueError:
        return None
    if start >= size or start < 0:
        raise ValueError("range start beyond end of file")
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def iter_file_chunks(
    path: str | Path,
    *,
    start: int = 0,
    length: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Yield at most ``chunk_size`` bytes per read from ``path``.

    ``length=None`` streams to EOF. The source file is never held as one
    object: peak memory is ``chunk_size`` plus whatever the transport buffers.
    """
    remaining = length
    with open(path, "rb") as handle:  # noqa: PTH123 - Path.open is the same call
        if start:
            handle.seek(start)
        while remaining is None or remaining > 0:
            want = chunk_size if remaining is None else min(chunk_size, remaining)
            if want <= 0:
                break
            chunk = handle.read(want)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def thumbnail_cache_dir() -> Path:
    """Directory holding generated preview thumbnails.

    ``XPST_PREVIEW_CACHE`` overrides it (used by tests and by a portable
    install); the default lives beside the rest of the app's cache. Generated
    previews are derived data — never committed, never uploaded.
    """
    override = os.environ.get("XPST_PREVIEW_CACHE")
    base = Path(override).expanduser() if override else Path.home() / ".xpst" / "cache" / "previews"
    return base


def thumbnail_cache_key(path: str | Path, width: int, stat: os.stat_result) -> str:
    """Stable cache key for one (file, variant) thumbnail.

    Size + mtime are part of the key, so editing or replacing the source
    invalidates the cached frame instead of showing a stale one.
    """
    digest = hashlib.sha256()
    digest.update(str(Path(path).resolve()).encode("utf-8"))
    digest.update(f"|{stat.st_size}|{stat.st_mtime_ns}|{width}".encode())
    return digest.hexdigest()[:32]


def _ffmpeg_frame_command(binary: str, source: Path, target: Path, width: int, *, video: bool) -> list[str]:
    """Build the ffmpeg argv that extracts exactly one scaled frame."""
    command = [binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        # -ss BEFORE -i: an input seek, so a long video is not decoded from the
        # start just to draw its first frame.
        command += ["-ss", "0"]
    command += ["-i", str(source), "-frames:v", "1", "-vf", f"scale={width}:-2"]
    command += ["-f", "image2", "-q:v", "4", str(target)]
    return command


def thumbnail_for(
    path: str | Path,
    *,
    width: int = DEFAULT_THUMBNAIL_WIDTH,
    cache_dir: Path | None = None,
    ffmpeg_path: str | None = None,
    timeout: float = THUMBNAIL_TIMEOUT_S,
) -> Path | None:
    """Return a cached JPEG thumbnail for ``path``, or ``None``.

    ``None`` means "no generated thumbnail available" — ffmpeg missing, ffmpeg
    failed, or the file changed under us. Callers must treat it as a fallback
    signal, never as an error page.
    """
    source = Path(path)
    try:
        stat = source.stat()
    except OSError:
        return None
    if stat.st_size <= 0:
        return None

    directory = cache_dir or thumbnail_cache_dir()
    target = directory / f"{thumbnail_cache_key(source, width, stat)}.jpg"
    if target.is_file() and target.stat().st_size > 0:
        return target

    binary = ffmpeg_path or resolve_ffmpeg_path() or shutil.which("ffmpeg")
    if not binary:
        logger.debug("No ffmpeg available; preview falls back to streaming")
        return None

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    video = source.suffix.lower() in VIDEO_EXTENSIONS
    handle, temp_name = tempfile.mkstemp(dir=str(directory), suffix=".part")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            _ffmpeg_frame_command(binary, source, temp_path, width, video=video),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0 or not temp_path.is_file() or temp_path.stat().st_size == 0:
            logger.debug("ffmpeg produced no thumbnail for %s", source.name)
            return None
        # Atomic publish so a concurrent request never sees a partial JPEG.
        temp_path.replace(target)
        return target
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Thumbnail generation failed for %s: %s", source.name, exc)
        return None
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
