"""Dependency reality: pyproject must describe what the code actually imports.

Context (F3 / t_555d6bc4, plan MASTER-PLAN-2026-09-15):
``PIL`` and ``lxml`` have no direct reference anywhere in ``src/`` or
``scripts/`` — they reach the engine bundle only as transitives of the
unofficial session libraries (``instagrapi`` → Pillow, ``twikit`` → lxml).
Those two paths are being demoted to an explicit opt-in fallback (plan B5), and
once that lands the modules must leave the bundle entirely.

These tests are the CI-side guard for that boundary:

* module-scope imports of the unofficial stack are forbidden in ``xpst`` — the
  demotion only holds while every use is a lazy, in-function import;
* the engine's boot graph must not contain them either (runtime proof, not
  source inspection);
* declared runtime dependencies must be bounded, so a dependency bump cannot
  silently change the shipped payload.

The tests deliberately do **not** assert on the bundle bytes — that is
``scripts/check_bundle_size.py``, which needs a built bundle.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

try:  # stdlib 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py3.10 CI leg
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The unofficial session stack, plus the image/HTML transitives it drags in.
UNOFFICIAL_STACK = ("twikit", "instagrapi", "PIL", "lxml")

# Dependencies intentionally left without an upper bound. yt-dlp moves with the
# platforms it scrapes, so pinning a ceiling is the thing that breaks the
# product; every other runtime dependency must be capped.
INTENTIONALLY_UNPINNED = {"yt-dlp"}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _module_scope_imports(path: Path) -> list[str]:
    """Top-level module names imported at module scope (not inside functions)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append(node.module.split(".")[0])
    return found


def test_xpst_never_imports_the_unofficial_stack_at_module_scope() -> None:
    """A module-scope import would make the demotion a lie: importing any xpst
    module (or the engine entrypoint) would pull the unofficial library — and,
    with it, PIL/lxml — back into the bundle."""
    offenders: list[str] = []
    for path in sorted((SRC / "xpst").rglob("*.py")):
        for module in _module_scope_imports(path):
            if module in UNOFFICIAL_STACK:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module} at module scope")
    assert not offenders, "\n".join(offenders)


def test_engine_boot_graph_does_not_import_the_unofficial_stack() -> None:
    """Importing the engine's entrypoint module must not pull the unofficial
    stack (or PIL/lxml) into ``sys.modules``.

    Run in a subprocess: pytest's own process may already have imported these
    via unrelated tests, which would make an in-process assertion order
    dependent.
    """
    probe = (
        "import json, sys;"
        "import xpst.dashboard.server;"
        "mods = sorted(sys.modules);"
        f"stack = {list(UNOFFICIAL_STACK)!r};"
        "bad = [m for m in stack if any(k == m or k.startswith(m + '.') for k in mods)];"
        "print(json.dumps({'bad': bad, 'count': len(mods)}))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"engine import failed:\n{result.stderr[-2000:]}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["bad"] == [], (
        "the engine boot graph now imports the unofficial stack: "
        f"{payload['bad']} — every use must stay a lazy, opt-in import"
    )
    assert payload["count"] > 100, "the probe imported suspiciously little; check the harness"


def test_declared_runtime_dependencies_are_bounded_and_real() -> None:
    """Every ``[project].dependencies`` entry must be importable (i.e. the name
    resolves to an installed distribution in CI's ``.[full,dev]`` install) and
    must carry an upper bound unless it is a documented exception."""
    from importlib.metadata import PackageNotFoundError, version

    problems: list[str] = []
    for raw in _pyproject()["project"]["dependencies"]:
        name = raw.split(";")[0]
        for sep in (">=", "==", "~=", "<=", ">", "<", "[", " "):
            name = name.split(sep)[0]
        name = name.strip()
        if not name:
            continue
        try:
            version(name)
        except PackageNotFoundError:
            problems.append(f"{name}: declared in pyproject but not installed")
        if name.lower() not in INTENTIONALLY_UNPINNED and "<" not in raw:
            problems.append(f"{name}: no upper bound ({raw!r})")
    assert not problems, "\n".join(problems)


def test_full_extra_only_references_declared_extras() -> None:
    """``full`` must not name an extra that does not exist — a typo there is a
    silent install-time no-op."""
    data = _pyproject()
    extras = data["project"].get("optional-dependencies", {})
    referenced: list[str] = []
    for spec in extras.get("full", []):
        if spec.startswith("xpst[") and spec.endswith("]"):
            referenced += [part.strip() for part in spec[5:-1].split(",") if part.strip()]
    missing = [name for name in referenced if name not in extras]
    assert not missing, f"extras referenced by 'full' but not declared: {missing}"
    assert referenced, "'full' no longer references any extra; update this test"
