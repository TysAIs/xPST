#!/usr/bin/env python3
"""Verify version single-sourcing across the repo surfaces.

Reads every place a version string is declared and reports which disagree.
Run: python scripts/verify_version_parity.py [--json]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CHECKS: list[tuple[str, Path, re.Pattern[str]]] = [
    ("python __init__", REPO / "src/xpst/__init__.py", re.compile(r'__version__\s*=\s*"([^"]+)"')),
    ("pyproject.toml", REPO / "pyproject.toml", re.compile(r'^version\s*=\s*"([^"]+)"', re.M)),
    ("tauri.conf.json", REPO / "src-tauri/tauri.conf.json", re.compile(r'"version"\s*:\s*"([^"]+)"')),
    ("ui/package.json", REPO / "ui/package.json", re.compile(r'"version"\s*:\s*"([^"]+)"')),
]


def main() -> int:
    as_json = "--json" in sys.argv
    versions: dict[str, str | None] = {}
    for label, path, pattern in CHECKS:
        if not path.exists():
            versions[label] = None
            continue
        m = pattern.search(path.read_text(encoding="utf-8"))
        versions[label] = m.group(1) if m else None

    declared = [v for v in versions.values() if v]
    problems: list[str] = [
        f"{label}: no version declaration found" for label, v in versions.items() if v is None
    ]
    if len(set(declared)) > 1:
        problems.append(
            "version mismatch: " + ", ".join(f"{k}={v}" for k, v in versions.items() if v)
        )

    if as_json:
        print(json.dumps({"versions": versions, "problems": problems}, indent=2))
    else:
        for label, v in versions.items():
            print(f"{label:20} {v or 'MISSING'}")
        if problems:
            for p in problems:
                print(f"PROBLEM: {p}")
        else:
            print("all declared versions agree")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())