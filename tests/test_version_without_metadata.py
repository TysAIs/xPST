"""The CLI and the dashboard must start when the distribution is not installed.

`importlib.metadata.version("xpst")` raises `PackageNotFoundError` for a source
checkout driven through `PYTHONPATH=src`, and for a `pip install --user` whose
user site moved with `HOME`. Both surfaces build their version at import time,
so an unguarded call there turns "not installed" into "the CLI cannot start at
all" — which is exactly how the Rust parity lane went red: every `config`
scenario exited 1 with a `PackageNotFoundError` traceback while the rest of the
suite passed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import xpst

REPO = Path(__file__).resolve().parents[1]

_MISSING_METADATA = textwrap.dedent(
    """
    import importlib.metadata as metadata

    def _missing(name):
        raise metadata.PackageNotFoundError(name)

    metadata.version = _missing
    """
)


def test_package_version_falls_back_to_the_source_tree(monkeypatch):
    import importlib.metadata as metadata

    import xpst.version as version_module

    def _missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(version_module.importlib.metadata, "version", _missing)
    assert version_module.package_version() == xpst.__version__


def test_package_version_prefers_the_installed_distribution(monkeypatch):
    import xpst.version as version_module

    monkeypatch.setattr(version_module.importlib.metadata, "version", lambda name: "9.9.9")
    assert version_module.package_version() == "9.9.9"


@pytest.mark.parametrize("module", ["xpst.cli", "xpst.dashboard.server"])
def test_surface_imports_without_installed_metadata(module):
    """Import the real module in a fresh interpreter with the metadata gone."""
    code = _MISSING_METADATA + f"import {module}\nprint('imported')\n"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "imported" in proc.stdout


def test_cli_reports_the_source_version_when_metadata_is_missing():
    """`xpst --version` still answers instead of dying at import."""
    code = _MISSING_METADATA + textwrap.dedent(
        """
        from click.testing import CliRunner

        from xpst.cli import main

        result = CliRunner().invoke(main, ["--version"])
        print(result.exit_code)
        print(result.output.strip())
        """
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0] == "0", proc.stdout
    assert xpst.__version__ in lines[1], proc.stdout
