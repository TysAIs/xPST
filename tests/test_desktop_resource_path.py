"""Tests for the frozen-aware resource_path helper.

Pure Python, no Qt — runs without PySide6 installed (mirrors the
test_desktop_splash_sizing.py precedent). PySide6 is intentionally NOT a
dependency of resource_path, so importing it here must not pull in Qt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xpst.desktop_app import resource_path as rp


def test_module_does_not_import_pyside6():
    # The helper must be Qt-free so its tests run in CI without PySide6.
    # Import it in a clean subprocess and assert PySide6 never lands in
    # sys.modules. This is the authoritative Qt-free guarantee.
    import subprocess

    code = (
        "import sys; import xpst.desktop_app.resource_path as m; "
        "assert not [n for n in sys.modules if n.split('.')[0] == 'PySide6'], "
        "'resource_path pulled in PySide6'; print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_not_frozen_in_dev_environment():
    # In dev/test there is no PyInstaller bundle.
    assert rp.is_frozen() is False


def test_base_path_points_at_source_root_in_dev():
    base = rp.get_base_path()
    # The source-tree base must contain the real assets directory.
    assert (base / "assets").is_dir()
    assert (base / "src" / "xpst").is_dir()


def test_resource_path_joins_relative_segments():
    p = rp.resource_path("assets", "icon.png")
    assert p == rp.get_base_path() / "assets" / "icon.png"
    assert isinstance(p, Path)


def test_resource_path_resolves_existing_asset():
    # xpst-full.png is a committed asset; it must resolve to a real file.
    p = rp.resource_path("assets", "xpst-full.png")
    assert p.exists()


def test_is_frozen_true_when_meipass_set(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEIfake", raising=False)
    assert rp.is_frozen() is True
    assert rp.get_base_path() == Path("/tmp/_MEIfake")
    assert rp.resource_path("assets", "icon.png") == Path("/tmp/_MEIfake/assets/icon.png")


def test_is_frozen_false_when_meipass_absent(monkeypatch):
    # frozen flag set but no _MEIPASS -> treat as not frozen (defensive).
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert rp.is_frozen() is False


def test_first_existing_returns_first_match(tmp_path):
    missing = tmp_path / "nope.png"
    real = tmp_path / "real.png"
    real.write_bytes(b"x")
    assert rp.first_existing(missing, real) == real


def test_first_existing_returns_none_when_all_missing(tmp_path):
    assert rp.first_existing(tmp_path / "a", tmp_path / "b") is None
