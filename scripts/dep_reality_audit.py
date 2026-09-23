#!/usr/bin/env python3
"""Dependency-reality audit: does ``pyproject.toml`` describe what the code uses?

Advisory tool (F3 / t_555d6bc4). Prints JSON with three lists that are easy to
get wrong in a large project:

* ``imported_but_undeclared``  — third-party modules the code imports that no
  declared distribution (or its transitive requirements) provides. This is the
  one that matters: it works today only because something else dragged the
  package in.
* ``declared_but_unused``      — declared deps nothing imports. Some are
  legitimate (CVE-only transitive pins, platform extras, CLI dev tools).
* ``unbounded_runtime_deps``   — runtime deps with no upper bound, minus a
  documented allowlist.

Import names are mapped to distributions via
``importlib.metadata.packages_distributions()`` (authoritative, from installed
metadata) instead of a hand-written table, and imports guarded by
``try/except ImportError`` are reported as optional rather than undeclared.

Usage: python scripts/dep_reality_audit.py [repo_root]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, distributions, packages_distributions
from pathlib import Path

try:  # stdlib 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py3.10 CI leg
    import tomli as tomllib

# Runtime deps intentionally left uncapped, with the reason.
INTENTIONALLY_UNBOUNDED = {
    "yt-dlp": "must track platform changes; a ceiling is what breaks the product",
}

# Tools that are only ever imported by our own build/QA scripts, not by the
# product, so they are deliberately not runtime or dev dependencies.
HOST_TOOLS = {"pyinstaller"}

# Extras whose dependencies are only used by tests/CI (invoked as commands or
# imported only from tests/).
DEV_EXTRAS = {"dev"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _name_of(requirement: str) -> str:
    return normalize(re.split(r"[<>=!~;\[ (]", requirement, maxsplit=1)[0])


def declared(root: Path) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    data = tomllib.loads((root / "pyproject.toml").read_text())
    runtime = {_name_of(r): r for r in data["project"]["dependencies"]}
    extras = {
        group: {_name_of(r): r for r in reqs}
        for group, reqs in data["project"].get("optional-dependencies", {}).items()
    }
    return runtime, extras


def transitive_closure(roots: set[str]) -> set[str]:
    """Every distribution name reachable from ``roots`` through Requires-Dist."""
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requires = distributions(name=name)
        except PackageNotFoundError:
            continue
        for dist in requires:
            for raw in dist.requires or []:
                # Skip dependencies gated behind another extra: they are not
                # installed by a plain `pip install <dist>`.
                if ";" in raw and "extra ==" in raw.split(";", 1)[1]:
                    continue
                dep = _name_of(raw)
                if dep and dep not in seen:
                    queue.append(dep)
    return seen


def imported(root: Path) -> dict[str, dict]:
    """module -> {files, optional} for every top-level import in src/ and scripts/."""
    seen: dict[str, dict] = {}

    def record(name: str, path: Path, optional: bool) -> None:
        entry = seen.setdefault(name, {"files": set(), "optional": True})
        entry["files"].add(str(path.relative_to(root)))
        entry["optional"] = entry["optional"] and optional

    for folder in ("src", "scripts"):
        for path in sorted((root / folder).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue

            guarded: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                catches_import_error = any(
                    isinstance(h.type, ast.Name)
                    and h.type.id in {"ImportError", "ModuleNotFoundError"}
                    or isinstance(h.type, ast.Tuple)
                    and any(
                        isinstance(e, ast.Name) and e.id in {"ImportError", "ModuleNotFoundError"}
                        for e in h.type.elts
                    )
                    for h in node.handlers
                    if h.type is not None
                )
                if catches_import_error:
                    for child in ast.walk(node):
                        guarded.add(id(child))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]] if not node.level else []
                else:
                    continue
                for name in filter(None, names):
                    record(name, path, id(node) in guarded)
    return seen


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    runtime, extras = declared(root)
    mods = imported(root)
    dist_map = packages_distributions()

    declared_names = set(runtime) | {n for group in extras.values() for n in group}
    provided_by_declared = declared_names | transitive_closure(declared_names)

    third_party = {
        mod: info
        for mod, info in mods.items()
        if mod not in sys.stdlib_module_names
        and not mod.startswith("_")
        and mod not in {"xpst", "tests", "scripts"}
    }

    undeclared: list[dict[str, object]] = []
    optional_undeclared: list[dict[str, object]] = []
    resolved: set[str] = set()
    for mod, info in sorted(third_party.items()):
        dists = {normalize(d) for d in dist_map.get(mod, [])}
        if not dists:
            dists = {normalize(mod)}
        provider = dists & provided_by_declared
        if provider:
            resolved |= provider
            continue
        entry = {
            "module": mod,
            "provided_by": sorted(dists),
            "files": sorted(info["files"])[:3],
        }
        if dists & HOST_TOOLS:
            continue
        (optional_undeclared if info["optional"] else undeclared).append(entry)

    unused_runtime = sorted(set(runtime) - resolved)
    unused_optional = {
        group: sorted(set(reqs) - resolved)
        for group, reqs in extras.items()
        if group not in DEV_EXTRAS and set(reqs) - resolved
    }
    unbounded = sorted(
        f"{name}: {spec}"
        for name, spec in runtime.items()
        if "<" not in spec and name not in INTENTIONALLY_UNBOUNDED
    )

    report = {
        "repo_root": str(root),
        "runtime_dep_count": len(runtime),
        "third_party_modules_imported": len(third_party),
        "imported_but_undeclared": undeclared,
        "optional_imports_not_declared": optional_undeclared,
        "declared_but_unused_runtime": unused_runtime,
        "declared_but_unused_optional": unused_optional,
        "unbounded_runtime_deps": unbounded,
        "intentionally_unbounded": INTENTIONALLY_UNBOUNDED,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
