"""Single source of truth for CrossPostEngine.

Audit item 6 (AUDIT-2026-06-10): CLI, scheduler, desktop, package init, and the
MCP server must all resolve to the *same* ``CrossPostEngine`` class. The
divergent ``xpst.engine_v2`` module is retired; these tests fail if it comes
back or if any surface drifts onto a second engine implementation.
"""

import importlib
from pathlib import Path

import pytest

import xpst
from xpst.engine import CrossPostEngine

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "xpst"


def test_retired_engine_v2_module_is_gone():
    """The divergent engine_v2 module must no longer exist or be importable."""
    assert not (SRC_ROOT / "engine_v2.py").exists(), "engine_v2.py should be deleted"

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("xpst.engine_v2")


def test_no_source_imports_engine_v2():
    """No shipped source file may import the retired engine module."""
    offenders = [
        str(path.relative_to(SRC_ROOT))
        for path in SRC_ROOT.rglob("*.py")
        if "engine_v2" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"engine_v2 still referenced by: {offenders}"


def test_package_export_is_canonical_engine():
    """``from xpst import CrossPostEngine`` must be the canonical engine."""
    assert xpst.CrossPostEngine is CrossPostEngine


def test_mcp_server_uses_canonical_engine():
    """The MCP server must bind to the same CrossPostEngine class as everyone else."""
    mcp = pytest.importorskip("mcp", reason="mcp extra not installed")  # noqa: F841
    from xpst.mcp import server as mcp_server

    assert mcp_server.CrossPostEngine is CrossPostEngine


def test_all_surfaces_share_one_engine_class():
    """CLI, scheduler, and desktop backend resolve to the single engine class."""
    from xpst import cli, scheduler
    from xpst.desktop_app import backend

    assert cli.CrossPostEngine is CrossPostEngine
    assert scheduler.CrossPostEngine is CrossPostEngine
    # Desktop backend lazily binds the engine (None until the optional desktop
    # extra is importable); when bound it must be the canonical class.
    assert backend.CrossPostEngine in (None, CrossPostEngine)
