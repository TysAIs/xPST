"""Logical-name -> icon-font glyph mapping, kept free of any Qt import so it is
testable without a display or PySide6 installed (mirrors splash_sizing.py and
resource_path.py).

The desktop UI previously used emoji / geometric / ASCII characters as icons,
many of which render as tofu boxes when the platform lacks the glyph. This
module is the single source of truth for the icon set: it maps stable logical
names to codepoints in the bundled Lucide icon font (``assets/fonts/lucide.ttf``,
ISC licensed). Both the live ``ThemeProvider`` (Python) and ``Icons.qml`` route
their glyphs through this mapping so there is exactly one place that defines
which glyph each logical name resolves to.

Every codepoint below was verified to exist in the bundled font's cmap. The
``ThemeProvider.icon*`` properties expose these glyphs to QML; the font family
name (``ICON_FONT_FAMILY``) is the family the bundled TTF registers as, so
``font.family`` bindings can target it.
"""

from __future__ import annotations

from pathlib import Path

#: Family name the bundled Lucide TTF registers as. QML ``font.family`` bindings
#: must use this exact string for the glyphs below to resolve.
ICON_FONT_FAMILY = "lucide"

#: Path segments (under the resource base) to the bundled icon font.
ICON_FONT_RELATIVE_PARTS: tuple[str, ...] = ("assets", "fonts", "lucide.ttf")


def icon_font_path() -> Path:
    """Return the bundled icon-font path resolved against the project root.

    The font lives under ``assets/fonts/`` at the project root (the dir that
    contains ``src/``), matching the existing ``assets/`` layout used by the
    desktop app. This is Qt-free so the path can be asserted in tests without a
    display; the caller checks ``.exists()`` and registers it with Qt's font
    database at startup.
    """
    # src/xpst/desktop_app/icon_glyphs.py -> project root is 4 parents up.
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    return project_root.joinpath(*ICON_FONT_RELATIVE_PARTS)

#: Logical icon name -> codepoint (int) in the bundled Lucide font.
#:
#: Platform icons (youtube/instagram/x/tiktok) use brand-neutral glyphs because
#: Lucide removed trademarked brand logos from its set; the platform *colour*
#: (ThemeProvider.youtube/instagram/xtwitter/tiktok) carries brand identity, so
#: these glyphs only need to render meaningfully, not be the official mark.
ICON_CODEPOINTS: dict[str, int] = {
    # Platform icons (neutral; colour carries the brand)
    "youtube": 0xE485,    # monitor-play
    "instagram": 0xE064,  # camera
    "x": 0xE0EF,          # hash
    "tiktok": 0xE122,     # music
    # Sidebar chrome
    "logo": 0xE1B4,       # zap
    "bell": 0xE059,       # bell
    "moon": 0xE11E,       # moon
    "sun": 0xE178,        # sun
    "stats": 0xE2A3,      # chart-column
    # Navigation
    "dashboard": 0xE1C1,  # layout-dashboard
    "content": 0xE0FF,    # layout-grid
    "analytics": 0xE2A5,  # chart-line
    "connect": 0xE37F,    # plug
    "schedule": 0xE087,   # clock
    "settings": 0xE154,   # settings
    "about": 0xE0F9,      # info
    # Status / actions
    "check": 0xE06C,      # check
    "error": 0xE077,      # circle-alert
    "warning": 0xE193,    # triangle-alert
    "close": 0xE1B2,      # x
    "edit": 0xE1F9,       # pencil
    "web": 0xE0E8,        # globe
    "users": 0xE1A4,      # users
    "trophy": 0xE373,     # trophy
    "calendar": 0xE063,   # calendar
    "video": 0xE1A5,      # video
    "trash": 0xE18E,      # trash-2
    "retry": 0xE149,      # rotate-cw
    "plus": 0xE13D,       # plus
    "search": 0xE151,     # search
}


def glyph(name: str) -> str:
    """Return the font glyph (a single character) for a logical icon name.

    Raises KeyError for an unknown name so a typo fails loudly in tests rather
    than silently producing a blank icon at runtime.
    """
    return chr(ICON_CODEPOINTS[name])


def glyph_map() -> dict[str, str]:
    """Return a logical-name -> glyph-character mapping (a fresh dict)."""
    return {name: chr(cp) for name, cp in ICON_CODEPOINTS.items()}
