"""The GitHub Pages site must serve the SAME icon TikTok reviewers see in the portal.

Context (2026-10-01): TikTok rejected the xPST dev app production review with the
verbatim note "The app icon submitted in Basic Info does not match the icon displayed
on the website or browser tab (favicon), please use the same app icon consistently in
Basic Info, on the website and in the browser tab (favicon)." The submitted app icon is
a solid green square (1024x1024, sRGB #1F6F3C == rgb(31, 111, 60)); this test pins that
the site ships exactly that icon as its favicon so a future page rewrite cannot regress
the reviewer-visible state that got us rejected.

Stdlib-only (zlib + struct): the CI matrix runs this on every OS without Pillow.
"""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"

# The exact icon TikTok's reviewer sees in Basic Info: solid green square.
APP_ICON_GREEN = (31, 111, 60)
ICON_PAGES = [
    "index.html",
    "terms/index.html",
    "privacy/index.html",
    "terms.html",
    "privacy.html",
]
ICON_ASSETS = [
    "app-icon-1024.png",
    "favicon-16.png",
    "favicon-32.png",
    "favicon-48.png",
    "apple-touch-icon.png",
    "android-chrome-192.png",
    "android-chrome-512.png",
]


def _png_decode(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB/RGBA PNG into (width, height, raw RGBX-ish bytes)."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name}: not a PNG"
    pos = 8
    idat = bytearray()
    width = height = colortype = bitdepth = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, bitdepth, colortype = struct.unpack(">IIBB", chunk[:10])
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
        pos += 12 + length
    assert bitdepth == 8, f"{path.name}: expected 8-bit PNG, got {bitdepth}"
    assert colortype in (2, 6), f"{path.name}: expected RGB/RGBA, got {colortype}"
    channels = 3 if colortype == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise AssertionError(f"{path.name}: bad PNG filter {ftype}")
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return width, height, bytes(out)


def test_app_icon_master_is_the_exact_portal_square() -> None:
    """assets/app-icon-1024.png is the 1024x1024 solid green square TikTok has on file."""
    master = ASSETS / "app-icon-1024.png"
    assert master.is_file(), "site favicon master missing — TikTok review flagged the icon mismatch"
    width, height, px = _png_decode(master)
    assert (width, height) == (1024, 1024), "TikTok requires a 1024x1024 app icon"
    # Sampled at 5 points, all must be the exact reviewer-visible green.
    stride = width * 3 if len(px) == width * height * 3 else width * 4
    for x, y in [(0, 0), (1023, 0), (512, 512), (0, 1023), (1023, 1023)]:
        off = y * stride + x * (stride // width)
        assert (px[off], px[off + 1], px[off + 2]) == APP_ICON_GREEN, (
            f"app icon at ({x},{y}) is {(px[off], px[off + 1], px[off + 2])}, "
            f"expected {APP_ICON_GREEN} — portal icon and site favicon must match"
        )


def test_every_favicon_asset_is_the_same_green() -> None:
    for name in ICON_ASSETS:
        path = ASSETS / name
        assert path.is_file(), f"missing favicon asset {name}"
        w, h, px = _png_decode(path)
        assert w == h, f"{name} must be square"
        cx, cy = w // 2, h // 2
        stride = w * 3 if len(px) == w * h * 3 else w * 4
        off = cy * stride + cx * (stride // w)
        assert (px[off], px[off + 1], px[off + 2]) == APP_ICON_GREEN, (
            f"{name} centre is not the portal green {APP_ICON_GREEN}"
        )


def test_every_site_page_declares_favicon_and_touch_icon() -> None:
    for rel in ICON_PAGES:
        page = REPO / rel
        assert page.is_file(), f"{rel} vanished"
        html = page.read_text(encoding="utf-8")
        assert re.search(r'<link[^>]+rel="icon"[^>]+href="[^"]*assets/favicon', html), (
            f"{rel}: no favicon <link> — the TikTok reviewer checks the browser-tab icon"
        )
        assert 'rel="apple-touch-icon"' in html, f"{rel}: no apple-touch-icon"


def test_declared_favicon_assets_exist_on_disk() -> None:
    """Every favicon href the pages point at must actually ship (404 = blank tab icon)."""
    for rel in ICON_PAGES:
        html = (REPO / rel).read_text(encoding="utf-8")
        base = (REPO / rel).parent
        for href in re.findall(r'<link[^>]+rel="(?:apple-touch-)?icon"[^>]+href="([^"]+)"', html):
            target = (base / href).resolve()
            assert target.is_file(), f"{rel} references missing favicon asset {href}"
