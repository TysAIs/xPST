"""Regression: ``xpst build`` must select build_linux.spec on Linux.

The macOS spec emits a .app/COLLECT onedir bundle that is meaningless on Linux;
the canonical CI release lane already uses build_linux.spec. See
docs/AUDIT-2026-06-10.md item 8.
"""

from __future__ import annotations

from pathlib import Path


def test_linux_build_uses_linux_spec():
    root = Path(__file__).resolve().parents[1]
    cli_text = (root / "src" / "xpst" / "cli.py").read_text(encoding="utf-8")
    assert '"linux": "build_linux.spec"' in cli_text
    assert '"linux": "build_macos.spec"' not in cli_text
    assert (root / "build_linux.spec").exists()
