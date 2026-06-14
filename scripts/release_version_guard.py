"""Guard release tags against publishing the wrong Python package version."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINAL_TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
RC_TAG_RE = re.compile(r"^v(?P<base>\d+\.\d+\.\d+)-rc(?P<num>\d+)$")


def read_pyproject_version(root: Path = ROOT) -> str:
    try:
        import tomllib

        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except ImportError:  # pragma: no cover - Python 3.10 fallback
        match = re.search(
            r'^version\s*=\s*"([^"]+)"',
            (root / "pyproject.toml").read_text(encoding="utf-8"),
            re.M,
        )
        if match:
            return match.group(1)
        raise RuntimeError("Could not read project.version from pyproject.toml")


def read_init_version(root: Path = ROOT) -> str:
    text = (root / "src" / "xpst" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise RuntimeError("Could not read __version__ from src/xpst/__init__.py")
    return match.group(1)


def expected_version_for_tag(tag: str) -> str:
    if match := FINAL_TAG_RE.match(tag):
        return match.group("version")
    if match := RC_TAG_RE.match(tag):
        return f"{match.group('base')}rc{int(match.group('num'))}"
    raise ValueError(f"Unsupported release tag {tag!r}; expected vX.Y.Z or vX.Y.Z-rcN")


def validate_release_version(tag: str | None, *, allow_untagged: bool = False, root: Path = ROOT) -> dict[str, object]:
    if not tag:
        if allow_untagged:
            return {"ok": True, "tag": None, "mode": "untagged"}
        raise ValueError("A release tag is required.")
    if not tag.startswith("v"):
        if allow_untagged:
            return {"ok": True, "tag": tag, "mode": "untagged"}
        raise ValueError(f"Not a release tag: {tag!r}")

    expected = expected_version_for_tag(tag)
    pyproject_version = read_pyproject_version(root)
    init_version = read_init_version(root)
    ok = pyproject_version == expected and init_version == expected
    return {
        "ok": ok,
        "tag": tag,
        "mode": "release",
        "expected_version": expected,
        "pyproject_version": pyproject_version,
        "init_version": init_version,
        "error": ""
        if ok
        else (
            f"Release tag {tag} requires package version {expected}; "
            f"pyproject has {pyproject_version}, __init__ has {init_version}."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate release tag/package version consistency")
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME"), help="Release tag, such as v0.1.0 or v0.1.0-rc1")
    parser.add_argument("--allow-untagged", action="store_true", help="Allow branch/workflow_dispatch runs to skip tag validation")
    args = parser.parse_args()

    result = validate_release_version(args.tag, allow_untagged=args.allow_untagged)
    if result["ok"]:
        if result["mode"] == "release":
            print(f"Release version ok: {result['tag']} -> {result['expected_version']}")
        else:
            print("Release version check skipped for untagged run.")
        return 0
    print(result["error"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
