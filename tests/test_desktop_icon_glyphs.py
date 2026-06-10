"""Tests for the Qt-free icon-glyph mapping (W4-5).

The icon set is the single source of truth shared by ThemeProvider (Python) and
Icons.qml, so it must be assertable without PySide6 or a display.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from xpst.desktop_app import icon_glyphs as ig


def test_font_family_is_lucide():
    assert ig.ICON_FONT_FAMILY == "lucide"


def test_icon_font_path_points_at_bundled_ttf():
    path = ig.icon_font_path()
    assert path.parts[-3:] == ("assets", "fonts", "lucide.ttf")
    assert path.exists(), f"bundled icon font missing at {path}"


def test_glyph_returns_single_pua_char():
    g = ig.glyph("youtube")
    assert g == chr(0xE485)
    assert len(g) == 1
    assert 0xE000 <= ord(g) <= 0xF8FF, "glyph should be in the Unicode Private Use Area"


def test_unknown_glyph_raises_keyerror():
    with pytest.raises(KeyError):
        ig.glyph("definitely-not-an-icon")


def test_glyph_map_is_complete_and_pua():
    m = ig.glyph_map()
    assert m, "glyph map must not be empty"
    assert set(m) == set(ig.ICON_CODEPOINTS)
    for name, ch in m.items():
        assert len(ch) == 1, name
        assert 0xE000 <= ord(ch) <= 0xF8FF, name
    # Returned dict is a fresh copy (mutating it must not corrupt the source).
    m.clear()
    assert ig.glyph_map(), "glyph_map must return a fresh dict each call"


def test_icon_glyphs_is_qt_free():
    code = (
        "import sys; from xpst.desktop_app import icon_glyphs as ig; "
        "ig.glyph('youtube'); ig.glyph_map(); ig.icon_font_path(); "
        "assert not [n for n in sys.modules if n.split('.')[0] == 'PySide6']; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
