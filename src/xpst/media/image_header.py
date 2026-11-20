"""Still-image dimensions read straight from the file header (no ffmpeg).

Image preflight needs the pixel size of a photo to enforce a destination's
aspect-ratio and dimension rules. Doing that through ffprobe made an image post
depend on a media binary xPST deliberately no longer bundles (-87 MB), and it
made the rule *silently skippable*: a missing ffprobe degraded to a warning, so
an out-of-range photo was offered and posted anyway.

JPEG, PNG, GIF and WebP all carry their dimensions in a fixed, well-documented
header fragment, so they are read here in pure Python (a few dozen bytes of
I/O, no subprocess). ``ffprobe`` stays the fallback for any other container.

Everything is defensive: a truncated or malformed file yields ``None`` (the
caller degrades to its probe warning) rather than raising.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: File suffixes this module can size without a subprocess.
HEADER_READABLE_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".gif", ".webp")

_JPEG_SOF_MARKERS = frozenset(
    # SOF0..SOF15 minus DHT (0xC4), JPG (0xC8) and DAC (0xCC) — those are not
    # frame headers.
    {code for code in range(0xC0, 0xD0)} - {0xC4, 0xC8, 0xCC}
)


def _jpeg_dimensions(handle) -> tuple[int, int] | None:
    """Walk JPEG segments to the start-of-frame header (both byte orders)."""
    if handle.read(2) != b"\xff\xd8":
        return None
    while True:
        byte = handle.read(1)
        if not byte:
            return None
        if byte != b"\xff":
            continue  # fill byte between segments
        marker = handle.read(1)
        while marker == b"\xff":  # legal padding before a marker
            marker = handle.read(1)
        if not marker:
            return None
        code = marker[0]
        if code == 0x01 or 0xD0 <= code <= 0xD9:  # standalone markers, no payload
            continue
        length_bytes = handle.read(2)
        if len(length_bytes) < 2:
            return None
        segment_length = int.from_bytes(length_bytes, "big")
        if segment_length < 2:
            return None
        if code in _JPEG_SOF_MARKERS:
            body = handle.read(5)
            if len(body) < 5:
                return None
            height = int.from_bytes(body[1:3], "big")
            width = int.from_bytes(body[3:5], "big")
            if width <= 0 or height <= 0:
                return None
            return width, height
        if code == 0xDA:  # start of scan — no frame header left to find
            return None
        handle.seek(segment_length - 2, 1)


def _png_dimensions(handle) -> tuple[int, int] | None:
    header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return (width, height) if width > 0 and height > 0 else None


def _gif_dimensions(handle) -> tuple[int, int] | None:
    header = handle.read(10)
    if len(header) < 10 or header[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", header[6:10])
    return (width, height) if width > 0 and height > 0 else None


def _webp_dimensions(handle) -> tuple[int, int] | None:
    header = handle.read(40)
    if len(header) < 16 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        return None
    chunk = header[12:16]
    if chunk == b"VP8X":  # extended format: 24-bit canvas size, minus one
        if len(header) < 30:
            return None
        width = int.from_bytes(header[24:27], "little") + 1
        height = int.from_bytes(header[27:30], "little") + 1
        return (width, height) if width > 0 and height > 0 else None
    if chunk == b"VP8 ":  # lossy: 3-byte frame tag, then the 0x9d012a sync code
        if len(header) < 30 or header[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(header[26:28], "little") & 0x3FFF
        height = int.from_bytes(header[28:30], "little") & 0x3FFF
        return (width, height) if width > 0 and height > 0 else None
    if chunk == b"VP8L":  # lossless: 14-bit width-1 / height-1 after the signature
        if len(header) < 25 or header[20] != 0x2F:
            return None
        bits = int.from_bytes(header[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height) if width > 0 and height > 0 else None
    return None


def read_image_dimensions(path: Path) -> tuple[int, int] | None:
    """Pixel size ``(width, height)`` of a still image, or None if unreadable.

    Supports JPEG, PNG, GIF and WebP from the file header alone. Never raises:
    an unknown, truncated or malformed file returns ``None``.
    """
    try:
        with open(path, "rb") as handle:
            magic = handle.read(12)
            if magic[:2] == b"\xff\xd8":
                handle.seek(0)
                return _jpeg_dimensions(handle)
            if magic[:8] == b"\x89PNG\r\n\x1a\n":
                handle.seek(0)
                return _png_dimensions(handle)
            if magic[:6] in (b"GIF87a", b"GIF89a"):
                handle.seek(0)
                return _gif_dimensions(handle)
            if magic[:4] == b"RIFF" and magic[8:12] == b"WEBP":
                handle.seek(0)
                return _webp_dimensions(handle)
    except OSError:
        return None
    return None


__all__ = ["HEADER_READABLE_SUFFIXES", "read_image_dimensions"]
