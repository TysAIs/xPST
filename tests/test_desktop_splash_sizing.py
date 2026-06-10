"""Regression test for the macOS launch flash: a large brand image must be
bounded to a sane splash size instead of being blitted full-screen.

Pure math, no Qt — runs without PySide6 installed.
"""
from xpst.desktop_app.splash_sizing import (
    MAX_SPLASH_HEIGHT,
    MAX_SPLASH_WIDTH,
    scaled_splash_size,
)


def test_giant_app_icon_is_bounded():
    # The 1024x1024 app icon must not pass through at native size.
    w, h = scaled_splash_size(1024, 1024)
    assert w <= MAX_SPLASH_WIDTH and h <= MAX_SPLASH_HEIGHT
    assert (w, h) == (360, 360)  # square -> bounded by the shorter max edge


def test_aspect_ratio_preserved_on_wide_image():
    w, h = scaled_splash_size(1200, 300)
    assert w <= MAX_SPLASH_WIDTH and h <= MAX_SPLASH_HEIGHT
    assert abs((w / h) - (1200 / 300)) < 0.01


def test_small_image_is_untouched():
    assert scaled_splash_size(400, 300) == (400, 300)


def test_never_enlarges():
    assert scaled_splash_size(100, 80) == (100, 80)


def test_non_positive_input_returns_max_box():
    assert scaled_splash_size(0, 0) == (MAX_SPLASH_WIDTH, MAX_SPLASH_HEIGHT)
