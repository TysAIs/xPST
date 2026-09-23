"""No shell execution: ``shell=True`` with user input must not exist.

Three independent proofs:

1. AST scan of every ``src/**/*.py`` module for a ``subprocess.*`` call carrying a
   truthy ``shell`` keyword, plus ``os.system``/``os.popen``/``os.exec*``.
2. A source-text grep so a literal that the AST misses (e.g. a string built at
   import time) still fails.
3. A runtime probe: a real subprocess call site is driven with a user-supplied
   value full of shell metacharacters and the capture asserts the value stayed a
   single argv element and no shell was used.

A shell would turn ``--playlist-items`` style argv into an injection point for a
config value or a URL; list-form argv cannot.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

_SHELL_ESCAPE_MODULES = {"os.system", "os.popen", "os.popen2", "os.popen3", "os.popen4"}


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_there_are_files_to_scan():
    """Guard against a scan that silently matches nothing."""
    assert len(_python_files()) > 50


class TestStaticScan:
    def test_no_shell_true_anywhere(self):
        hits: list[str] = []
        for path in _python_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "shell=True" in line.replace(" ", "") and "noqa" not in line:
                    hits.append(f"{path.relative_to(SRC)}:{lineno}")
        assert hits == [], f"shell=True found: {hits}"

    def test_no_os_system_or_popen(self):
        hits: list[str] = []
        for path in _python_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for bad in _SHELL_ESCAPE_MODULES:
                    if bad + "(" in stripped:
                        hits.append(f"{path.relative_to(SRC)}:{lineno} {bad}")
                if "os.exec" in stripped and "(" in stripped:
                    hits.append(f"{path.relative_to(SRC)}:{lineno} os.exec*")
        assert hits == [], f"shell-spawning helpers found: {hits}"

    def test_ast_finds_a_planted_violation(self, tmp_path):
        """The AST check must actually be capable of firing."""
        planted = "import subprocess\nsubprocess.run(['ls'], shell=True)\n"
        tree = ast.parse(planted)
        assert _find_shell_calls(tree), "AST detector is broken — it misses shell=True"

    def test_ast_finds_no_shell_calls_in_this_repo(self):
        offenders: list[str] = []
        for path in _python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - a broken file is a different failure
                continue
            for node in _find_shell_calls(tree):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
        assert offenders == [], f"subprocess called with a truthy shell kwarg: {offenders}"


def _find_shell_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every ``subprocess.*`` / ``Popen`` call with a truthy ``shell``."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Attribute):
            parts = []
            cursor = func
            while isinstance(cursor, ast.Attribute):
                parts.append(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name):
                parts.append(cursor.id)
            name = ".".join(reversed(parts))
        elif isinstance(func, ast.Name):
            name = func.id
        if not (name.startswith("subprocess.") or name in {"Popen", "run", "call", "check_output", "check_call"}):
            continue
        for keyword in node.keywords:
            if keyword.arg != "shell":
                continue
            value = keyword.value
            truthy = (
                (isinstance(value, ast.Constant) and bool(value.value))
                or (isinstance(value, ast.Name) and value.id != "False")
            )
            if truthy:
                found.append(node)
    return found


class TestRuntimeProbe:
    """Drive a real subprocess call site with hostile user input."""

    @pytest.mark.asyncio
    async def test_hostile_channel_value_is_one_argv_element(self, monkeypatch):
        from xpst.config import XPSTConfig
        from xpst.sources.youtube import YouTubeSource

        captured: list[tuple[list[str], dict]] = []

        def fake_run(cmd, *args, **kwargs):
            captured.append((list(cmd), kwargs))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        hostile = "evil; touch /tmp/xpst-shell-probe; echo pwned #"
        config = XPSTConfig()
        config.youtube.channel_id = f"@{hostile}"
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(YouTubeSource, "_find_yt_dlp", lambda self: "yt-dlp")

        source = YouTubeSource(config)
        await source.list_videos(max_count=1)

        assert captured, "no subprocess call was captured — the probe proved nothing"
        for cmd, kwargs in captured:
            assert not kwargs.get("shell"), "subprocess.run was called with shell truthy"
            assert "shell" not in kwargs, "subprocess.run received an explicit shell argument"
            # The hostile value must survive as ONE element, unsplit.
            assert any(hostile in element for element in cmd), (
                "the hostile value was not passed verbatim as a single argv element"
            )

    def test_subprocess_calls_use_list_argv_not_strings(self):
        """A string command implies a shell. Assert none exists."""
        offenders: list[str] = []
        for path in _python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else ""
                if not (isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "subprocess"):
                    continue
                if name not in {"run", "Popen", "call", "check_output", "check_call"}:
                    continue
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
        assert offenders == [], f"subprocess called with a string command (implies a shell): {offenders}"
