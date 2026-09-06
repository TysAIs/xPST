"""Regression tests for the xPST runtime version source of truth.

Covers the P1 defect "UI/About shows v1.0.0 while Info.plist=1.1.0": the
runtime ``xpst.__version__`` must never fall back to a stale hard-coded
literal. In a PyInstaller one-folder app there is no xpst dist-info, so
``importlib.metadata`` raises PackageNotFoundError; the only way the About
page can still report the real version is via the build-time generated
``_build_version`` module and, in a bare source checkout, pyproject.toml.
"""

import importlib
import importlib.metadata
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_build_version():
    """Isolate the test from any build.sh-generated _build_version module."""
    sys.modules.pop("xpst._build_version", None)
    yield
    sys.modules.pop("xpst._build_version", None)


def _pyproject_version():
    root = Path(__file__).parents[1]
    import re
    m = re.search(
        r'^version = "([^"]+)"',
        (root / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert m, "pyproject.toml must declare a project version"
    return m.group(1)


def test_version_matches_pyproject_when_build_version_present(monkeypatch):
    """Frozen-bundle path: _build_version (baked at build) is authoritative."""
    from xpst import _resolve_version

    ver = _pyproject_version()
    mod = types.ModuleType("xpst._build_version")
    mod.__version__ = ver
    monkeypatch.setitem(sys.modules, "xpst._build_version", mod)

    assert _resolve_version() == ver


def test_version_uses_installed_metadata_when_available(monkeypatch):
    """Installed/editable distribution metadata is a valid source."""
    from xpst import _resolve_version

    monkeypatch.setattr(
        importlib.metadata, "version", lambda name: "9.9.9"
    )
    assert _resolve_version() == "9.9.9"


def test_version_reads_pyproject_when_metadata_missing(monkeypatch):
    """Bare source checkout: no dist-info -> fall back to pyproject.toml.

    This is precisely the frozen-bundle failure mode; the returned version
    must be the real one (matching Info.plist), not a stale literal.
    """
    from xpst import _resolve_version

    def boom(_name):
        raise importlib.metadata.PackageNotFoundError("xpst")

    monkeypatch.setattr(importlib.metadata, "version", boom)
    assert _resolve_version() == _pyproject_version()


def test_version_never_falls_back_to_stale_literal(monkeypatch):
    """Last-resort marker is neutral, never a possibly-stale release number."""
    from xpst import _resolve_version

    def boom(_name):
        raise importlib.metadata.PackageNotFoundError("xpst")

    monkeypatch.setattr(importlib.metadata, "version", boom)
    # Force the pyproject read to fail too, so we exercise the final fallback.
    import pathlib
    monkeypatch.setattr(
        pathlib.Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("no file"))
    )
    assert _resolve_version() == "0.0.0"


def test_package_version_is_never_legacy_100():
    """xpst.__version__ must never report the old hard-coded v1.0.0."""
    from xpst import __version__

    assert __version__ == _pyproject_version()
