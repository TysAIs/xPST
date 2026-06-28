"""The import-linter contracts in pyproject.toml must hold: the KB heavy-dep
lazy-load wall and the one-way cross-poster -> knowledge dependency direction.

These are the static-graph complement to the runtime `sys.modules` wall tests
(tests/test_knowledge_wall.py, tests/test_knowledge_organize_wall.py). Skipped
gracefully if import-linter is not installed so a minimal env still collects.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _lint_imports_cmd() -> list[str] | None:
    """Locate the `lint-imports` console script. Prefer the one next to the
    running interpreter (the active venv), then PATH."""
    candidate = Path(sys.executable).with_name("lint-imports")
    if candidate.exists():
        return [str(candidate)]
    found = shutil.which("lint-imports")
    return [found] if found else None


@pytest.mark.skipif(_lint_imports_cmd() is None,
                    reason="import-linter (dev extra) not installed")
def test_import_linter_contracts_pass():
    cmd = _lint_imports_cmd()
    assert cmd is not None
    result = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "import-linter contracts broken:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "Contracts: 2 kept, 0 broken." in result.stdout, result.stdout


def test_new_kb_modules_stay_off_the_runtime_wall():
    """Importing the new Phase 5 cold-path modules (queue, course, doctor) and
    the CLI that attaches them must not load faster-whisper / fastembed /
    lancedb into sys.modules."""
    code = (
        "import sys; "
        "import xpst.cli; "
        "import xpst.knowledge.queue; "
        "import xpst.knowledge.course.assemble; "
        "import xpst.knowledge.doctor; "
        "assert 'faster_whisper' not in sys.modules, 'queue/course/doctor wall: faster_whisper'; "
        "assert 'fastembed' not in sys.modules, 'queue/course/doctor wall: fastembed'; "
        "assert 'lancedb' not in sys.modules, 'queue/course/doctor wall: lancedb'; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
