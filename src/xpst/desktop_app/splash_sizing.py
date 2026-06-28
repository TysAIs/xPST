"""Pure splash-sizing math, kept free of any Qt import so it is testable
without a display or PySide6 installed.

The launch splash loads a brand image. If that image is large (the app icon
is 1024x1024), QSplashScreen would blit it at native size, frameless and
centered, producing a giant image flashed across the screen on launch. This
helper bounds the splash to a sane box while preserving aspect ratio.
"""
from __future__ import annotations

#: Maximum splash dimensions in logical pixels.
MAX_SPLASH_WIDTH = 480
MAX_SPLASH_HEIGHT = 360


def scaled_splash_size(
    width: int,
    height: int,
    max_width: int = MAX_SPLASH_WIDTH,
    max_height: int = MAX_SPLASH_HEIGHT,
) -> tuple[int, int]:
    """Return (width, height) bounded to the max box, preserving aspect ratio.

    Only shrinks; never enlarges. Returns the max box for non-positive input.
    """
    if width <= 0 or height <= 0:
        return (max_width, max_height)
    if width <= max_width and height <= max_height:
        return (width, height)
    scale = min(max_width / width, max_height / height)
    return (max(1, round(width * scale)), max(1, round(height * scale)))
