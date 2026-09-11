#!/usr/bin/env python3
"""Verify the one release version used by Python, Rust, Tauri, and Svelte."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_RELEASE_VERSION = "1.1.0"


def _read_python_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib

        return str(tomllib.loads(text)["project"]["version"])
    except ImportError:
        match = re.search(r"(?m)^version\s*=\s*\"([^\"]+)\"", text)
        if not match:
            raise ValueError("pyproject.toml has no project version")
        return match.group(1)


def _read_python_runtime_version(root: Path) -> str:
    text = (root / "src" / "xpst" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"(?m)^__version__\s*=\s*\"([^\"]+)\"", text)
    if not match:
        raise ValueError("src/xpst/__init__.py has no runtime version")
    return match.group(1)


def _read_cargo_version(root: Path) -> str:
    text = (root / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[package\].*?^version\s*=\s*\"([^\"]+)\"", text)
    if not match:
        raise ValueError("src-tauri/Cargo.toml has no package version")
    return match.group(1)


def _read_json_version(root: Path, relative_path: str) -> str:
    data: dict[str, Any] = json.loads((root / relative_path).read_text(encoding="utf-8"))
    return str(data["version"])


def release_versions(root: Path = ROOT) -> dict[str, str]:
    """Return every release-facing version, with Python as the source of truth."""
    return {
        "python": _read_python_version(root),
        "python-runtime": _read_python_runtime_version(root),
        "cargo": _read_cargo_version(root),
        "tauri": _read_json_version(root, "src-tauri/tauri.conf.json"),
        "ui": _read_json_version(root, "ui/package.json"),
        "ui-lock": str(
            json.loads((root / "ui" / "package-lock.json").read_text(encoding="utf-8"))["packages"][""]["version"]
        ),
    }


def verify_release_version(expected: str = REQUIRED_RELEASE_VERSION, root: Path = ROOT) -> dict[str, str]:
    """Raise ``ValueError`` unless all release surfaces match ``expected``."""
    versions = release_versions(root)
    mismatches = {name: version for name, version in versions.items() if version != expected}
    if mismatches:
        details = ", ".join(f"{name}={version}" for name, version in sorted(mismatches.items()))
        raise ValueError(f"release version mismatch (expected {expected}): {details}")
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", default=REQUIRED_RELEASE_VERSION)
    args = parser.parse_args()
    try:
        versions = verify_release_version(args.expected)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"release version verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"release version {args.expected} verified: {', '.join(sorted(versions))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
