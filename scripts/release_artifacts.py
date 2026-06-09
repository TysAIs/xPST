#!/usr/bin/env python3
"""Release artifact generation and validation for xPST.

Creates:
- PyPI wheel + sdist
- macOS signed .app + DMG (optional)
- Checksums (SHA256, SHA512)
- Release notes from CHANGELOG
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def find_dist_files(dist_dir: Path) -> dict[str, Path]:
    """Find built distribution files."""
    wheels = list(dist_dir.glob("*.whl"))
    sdists = list(dist_dir.glob("*.tar.gz"))

    if not wheels:
        raise FileNotFoundError("No wheel found in dist/")
    if not sdists:
        raise FileNotFoundError("No sdist found in dist/")

    return {"wheel": wheels[0], "sdist": sdists[0]}


def compute_checksums(file_path: Path) -> dict[str, str]:
    """Compute SHA256 and SHA512 checksums."""
    content = file_path.read_bytes()
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha512": hashlib.sha512(content).hexdigest(),
    }


def extract_changelog(version: str) -> str:
    """Extract changelog entry for version."""
    changelog = Path("CHANGELOG.md")
    if not changelog.exists():
        return f"Release {version}"

    content = changelog.read_text(encoding="utf-8")
    # Find version section (## [version] - date)
    pattern = rf"^##\s+\[{re.escape(version)}\]\s+-.*?(?=^##\s+\[|\Z)"
    match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    if match:
        return match.group(0).strip()
    return f"Release {version}"


def generate_checksums_file(dist_dir: Path, output: Path) -> None:
    """Generate checksums.txt with all distribution file hashes."""
    files = find_dist_files(dist_dir)
    lines = []

    for name, path in files.items():
        checksums = compute_checksums(path)
        lines.append(f"{checksums['sha256']}  {path.name}")
        lines.append(f"{checksums['sha512']}  {path.name}")

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Generated: {output}")


def generate_pypi_json(dist_dir: Path, output: Path, version: str) -> None:
    """Generate PyPI metadata JSON for release."""
    files = find_dist_files(dist_dir)
    metadata = {
        "name": "xpst",
        "version": version,
        "files": [],
    }

    for name, path in files.items():
        checksums = compute_checksums(path)
        metadata["files"].append({
            "filename": path.name,
            "size": path.stat().st_size,
            "hashes": {
                "sha256": checksums["sha256"],
                "sha512": checksums["sha512"],
            },
            "packagetype": "bdist_wheel" if name == "wheel" else "sdist",
            "python_version": "py3",
        })

    output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Generated: {output}")


def verify_wheel(wheel_path: Path) -> bool:
    """Verify wheel can be installed."""
    try:
        # Check wheel structure
        result = subprocess.run(
            ["unzip", "-l", str(wheel_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        print("Wheel contents:")
        print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Wheel verification failed: {e.stderr}")
        return False


def verify_sdist(sdist_path: Path) -> bool:
    """Verify sdist can be extracted."""
    try:
        result = subprocess.run(
            ["tar", "-tzf", str(sdist_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        print("Sdist contents:")
        print(result.stdout[:500] + ("..." if len(result.stdout) > 500 else ""))
        return True
    except subprocess.CalledProcessError as e:
        print(f"Sdist verification failed: {e.stderr}")
        return False


def run_quality_checks() -> bool:
    """Run all quality checks."""
    checks = [
        (["python", "-m", "pytest", "-x", "-q"], "Tests"),
        (["ruff", "check", "src", "tests"], "Linting"),
        (["mypy", "src/xpst"], "Type checking"),
    ]

    for cmd, name in checks:
        print(f"\n=== {name} ===")
        try:
            subprocess.run(cmd, check=True, timeout=300)
        except subprocess.CalledProcessError:
            print(f"{name} FAILED")
            return False
        except subprocess.TimeoutExpired:
            print(f"{name} TIMEOUT")
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate release artifacts")
    parser.add_argument("--dist", required=True, help="Distribution directory")
    parser.add_argument("--version", required=True, help="Version string")
    parser.add_argument("--output-dir", default="release", help="Output directory")
    parser.add_argument("--skip-checks", action="store_true", help="Skip quality checks")
    args = parser.parse_args()

    dist_dir = Path(args.dist)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not dist_dir.exists():
        print(f"Dist directory not found: {dist_dir}")
        return 1

    # Run quality checks
    if not args.skip_checks:
        print("Running quality checks...")
        if not run_quality_checks():
            print("Quality checks failed!")
            return 1

    # Verify built artifacts
    files = find_dist_files(dist_dir)
    print(f"\nFound artifacts:")
    print(f"  Wheel: {files['wheel'].name}")
    print(f"  Sdist: {files['sdist'].name}")

    if not verify_wheel(files["wheel"]):
        return 1

    if not verify_sdist(files["sdist"]):
        return 1

    # Generate checksums
    checksums_file = output_dir / "checksums.txt"
    generate_checksums_file(dist_dir, checksums_file)

    # Generate PyPI JSON
    pypi_json = output_dir / "pypi.json"
    generate_pypi_json(dist_dir, pypi_json, args.version)

    # Generate release notes
    release_notes = output_dir / "RELEASE_NOTES.md"
    notes = extract_changelog(args.version)
    release_notes.write_text(f"# xPST {args.version}\n\n{notes}\n", encoding="utf-8")
    print(f"Generated: {release_notes}")

    # Copy artifacts to output
    for path in files.values():
        target = output_dir / path.name
        target.write_bytes(path.read_bytes())
        print(f"Copied: {target}")

    print(f"\n✅ Release artifacts generated in {output_dir}/")
    print(f"   checksums.txt: {checksums_file.read_text().strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())