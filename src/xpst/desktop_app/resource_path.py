"""Frozen-aware resource path resolution, kept free of any Qt import so it is
testable without a display or PySide6 installed (mirrors splash_sizing.py).

PyInstaller-frozen apps unpack bundled data files into a temporary directory
exposed as ``sys._MEIPASS``. In a frozen build, bundled assets and QML live
under that directory; in a source checkout they live relative to the source
tree. ``resource_path`` resolves a relative path against whichever base
applies so the desktop app finds its assets in both modes.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """Return True when running inside a PyInstaller (or similar) bundle.

    Detection is based on ``sys.frozen`` plus the presence of ``sys._MEIPASS``,
    which PyInstaller sets only in a frozen build. In dev/test it is absent, so
    this returns False.
    """
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def get_base_path() -> Path:
    """Return the base directory for resolving bundled resources.

    - Frozen: ``sys._MEIPASS`` (the PyInstaller extraction dir).
    - Source: the project root (four parents up from this file, i.e. the dir
      containing ``src/`` — matching the existing ``assets/`` layout used by
      ``main.py``).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if is_frozen() and meipass:
        return Path(meipass)
    # src/xpst/desktop_app/resource_path.py -> project root is 4 parents up.
    return Path(__file__).resolve().parent.parent.parent.parent


def _frozen_resource_bases() -> list[Path]:
    """Ordered candidate resource bases for a frozen build (defensive).

    PyInstaller 6.x macOS .app bundles split their payload between
    ``Contents/Frameworks`` (the binary side — this is what ``sys._MEIPASS``
    points at) and ``Contents/Resources`` (the data side: QML, assets, Python
    packages), then hard-link the two sides so either root resolves every
    file. That linking is an implementation detail that can differ between
    PyInstaller versions, so for macOS .app layouts we probe the data side
    as a fallback. Every other frozen layout (one-folder) has exactly one
    base, returned as-is.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not (is_frozen() and meipass):
        return []
    base = Path(meipass)
    if (
        sys.platform == "darwin"
        and base.name == "Frameworks"
        and base.parent.name == "Contents"
    ):
        return [base, base.parent / "Resources"]
    return [base]


def resource_path(*relative_parts: str) -> Path:
    """Resolve a resource path relative to the frozen bundle or source tree.

    Accepts one or more path segments (e.g. ``resource_path("assets",
    "icon.png")``). The returned path is not guaranteed to exist; callers
    should check ``.exists()`` and fall back as appropriate.

    In a frozen build the path resolves against the resource base; on a macOS
    .app bundle the data side (``Contents/Resources``) is probed as fallback
    when the ``_MEIPASS`` candidate is absent, preferring whichever actually
    exists. The return value always roots at a *candidate* base so callers
    that only check ``.exists()`` behave identically in every layout.
    """
    bases = _frozen_resource_bases()
    if bases:
        candidates = [base.joinpath(*relative_parts) for base in bases]
        return first_existing(*candidates) or candidates[0]
    return get_base_path().joinpath(*relative_parts)


def first_existing(*candidates: Path) -> Path | None:
    """Return the first candidate path that exists, or None if none do."""
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def icon_candidates() -> list[Path]:
    """Candidate app-icon paths, unified under ``assets/`` (frozen-aware).

    All branding/tray icon lookups go through ``assets/`` so macOS, Windows,
    and Linux share one source of truth. Paths are returned in priority order.
    """
    return [
        resource_path("assets", "icon.png"),
        resource_path("assets", "xpst-full.png"),
        resource_path("assets", "xpst-icon.png"),
    ]


def resolve_app_icon(*, required: bool = False) -> Path | None:
    """Resolve the app icon from ``assets/`` (Qt-free).

    Returns the first existing candidate. When ``required`` is True and none
    exist, raise FileNotFoundError with an actionable message naming the
    expected location (W3-6: hard-fail with a clear error if missing).
    """
    candidates = icon_candidates()
    found = first_existing(*candidates)
    if found is not None:
        return found
    if required:
        expected = candidates[0]
        raise FileNotFoundError(
            f"Icon not found at {expected}. "
            "Ensure the app icon is in the assets/ directory "
            "(expected one of: icon.png, xpst-full.png, xpst-icon.png)."
        )
    return None
