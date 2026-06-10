"""Tests for unified app-icon sourcing under assets/ (W3-6).

Qt-free: the icon-resolution logic lives in resource_path.py (no PySide6),
so the splash and tray paths in main.py both share one source of truth.
"""

from __future__ import annotations

import sys

import pytest

from xpst.desktop_app import resource_path as rp


def test_icon_candidates_are_all_under_assets():
    candidates = rp.icon_candidates()
    assert candidates, "expected at least one icon candidate"
    for c in candidates:
        assert c.parent.name == "assets", f"{c} is not under assets/"


def test_resolve_app_icon_finds_committed_asset():
    # assets/xpst-full.png is committed; resolution must succeed in dev.
    found = rp.resolve_app_icon(required=False)
    assert found is not None
    assert found.exists()
    assert found.parent.name == "assets"


def test_resolve_app_icon_missing_returns_none_when_not_required(monkeypatch, tmp_path):
    # Point the base path at an empty dir so no icon exists.
    monkeypatch.setattr(rp, "get_base_path", lambda: tmp_path)
    assert rp.resolve_app_icon(required=False) is None


def test_resolve_app_icon_missing_hard_fails_with_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(rp, "get_base_path", lambda: tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        rp.resolve_app_icon(required=True)
    msg = str(excinfo.value)
    assert "Icon not found at" in msg
    assert "assets/" in msg
    # The message names the expected directory location concretely.
    assert str(tmp_path / "assets") in msg


def test_resolve_app_icon_prefers_icon_png(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "icon.png").write_bytes(b"x")
    (assets / "xpst-full.png").write_bytes(b"y")
    monkeypatch.setattr(rp, "get_base_path", lambda: tmp_path)
    found = rp.resolve_app_icon(required=True)
    assert found == assets / "icon.png"


def test_icon_logic_is_qt_free():
    # Resolving an icon must never require PySide6.
    import subprocess

    code = (
        "import sys; from xpst.desktop_app import resource_path as rp; "
        "rp.resolve_app_icon(required=False); "
        "assert not [n for n in sys.modules if n.split('.')[0] == 'PySide6']; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
