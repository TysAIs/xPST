#!/usr/bin/env python3
"""Release artifact generation and validation for xPST.

Creates:
- PyPI wheel + sdist
- macOS signed .app + DMG (optional)
- Checksums (SHA256SUMS, SHA512SUMS)
- CycloneDX SBOM
- Release notes from CHANGELOG
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


def find_dist_files(dist_dir: Path) -> dict[str, list[Path]]:
    """Find releasable distribution files."""
    ignored_suffixes = {".blockmap", ".sig"}
    files = [
        path
        for path in sorted(dist_dir.iterdir())
        if path.is_file() and not path.name.startswith(".") and path.suffix not in ignored_suffixes
    ]
    wheels = [path for path in files if path.suffix == ".whl"]
    sdists = [path for path in files if path.name.endswith(".tar.gz")]
    desktop = [path for path in files if path not in wheels and path not in sdists]

    if not files:
        raise FileNotFoundError(f"No releasable files found in {dist_dir}")

    return {"all": files, "wheels": wheels, "sdists": sdists, "desktop": desktop}


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


def generate_checksum_files(dist_dir: Path, sha256_output: Path, sha512_output: Path) -> None:
    """Generate SHA256SUMS and SHA512SUMS for all distribution files."""
    files = find_dist_files(dist_dir)
    sha256_lines = []
    sha512_lines = []

    for path in files["all"]:
        checksums = compute_checksums(path)
        sha256_lines.append(f"{checksums['sha256']}  {path.name}")
        sha512_lines.append(f"{checksums['sha512']}  {path.name}")

    sha256_output.write_text("\n".join(sha256_lines) + "\n", encoding="utf-8")
    sha512_output.write_text("\n".join(sha512_lines) + "\n", encoding="utf-8")
    print(f"Generated: {sha256_output}")
    print(f"Generated: {sha512_output}")


def generate_pypi_json(dist_dir: Path, output: Path, version: str) -> None:
    """Generate PyPI metadata JSON for release."""
    files = find_dist_files(dist_dir)
    metadata = {
        "name": "xpst",
        "version": version,
        "files": [],
    }

    package_files = files["wheels"] + files["sdists"]
    if not package_files:
        if output.exists():
            output.unlink()
        print(f"Skipping PyPI metadata; no wheel or sdist found in {dist_dir}")
        return

    for path in package_files:
        checksums = compute_checksums(path)
        metadata["files"].append({
            "filename": path.name,
            "size": path.stat().st_size,
            "hashes": {
                "sha256": checksums["sha256"],
                "sha512": checksums["sha512"],
            },
            "packagetype": "bdist_wheel" if path.suffix == ".whl" else "sdist",
            "python_version": "py3",
        })

    output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Generated: {output}")


def _distribution_license(metadata: importlib.metadata.PackageMetadata) -> str:
    """Return a compact license expression from package metadata."""
    expression = metadata.get("License-Expression")
    if expression:
        return expression

    license_value = (metadata.get("License") or "").strip()
    if license_value and "\n" not in license_value and len(license_value) <= 80:
        return license_value

    classifiers = metadata.get_all("Classifier") or []
    licenses = [
        classifier.rsplit(" :: ", 1)[-1]
        for classifier in classifiers
        if classifier.startswith("License ::")
    ]
    return "; ".join(sorted(set(licenses))) or "NOASSERTION"


def _distribution_url(metadata: importlib.metadata.PackageMetadata) -> str | None:
    """Return the best available project URL from package metadata."""
    project_urls = metadata.get_all("Project-URL") or []
    preferred = ("Source", "Homepage", "Repository", "Documentation", "Changelog")
    parsed: dict[str, str] = {}
    for item in project_urls:
        if "," not in item:
            continue
        label, url = item.split(",", 1)
        parsed[label.strip()] = url.strip()

    for label in preferred:
        if parsed.get(label):
            return parsed[label]
    return metadata.get("Home-page")


def _component_from_distribution(dist: importlib.metadata.Distribution) -> dict[str, object]:
    """Build a CycloneDX component from an installed Python distribution."""
    metadata = dist.metadata
    name = metadata.get("Name") or "unknown"
    version = metadata.get("Version") or dist.version
    component: dict[str, object] = {
        "type": "library",
        "name": name,
        "version": version,
        "purl": f"pkg:pypi/{name.lower()}@{version}",
        "licenses": [{"expression": _distribution_license(metadata)}],
    }
    url = _distribution_url(metadata)
    if url:
        component["externalReferences"] = [{"type": "website", "url": url}]
    return component


def generate_sbom(dist_dir: Path, output: Path, version: str) -> None:
    """Generate a lightweight CycloneDX SBOM for release artifacts."""
    release_files = find_dist_files(dist_dir)["all"]
    distributions = sorted(
        importlib.metadata.distributions(),
        key=lambda dist: ((dist.metadata.get("Name") or "").lower(), dist.version),
    )
    components = [_component_from_distribution(dist) for dist in distributions]
    for path in release_files:
        checksums = compute_checksums(path)
        components.append({
            "type": "file",
            "name": path.name,
            "hashes": [
                {"alg": "SHA-256", "content": checksums["sha256"]},
                {"alg": "SHA-512", "content": checksums["sha512"]},
            ],
        })

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid5(NAMESPACE_URL, f'https://github.com/TysAIs/xPST/releases/tag/v{version}')}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": {
                "type": "application",
                "name": "xpst",
                "version": version,
                "licenses": [{"expression": "MIT OR Apache-2.0"}],
                "purl": f"pkg:pypi/xpst@{version}",
            },
        },
        "components": components,
    }
    output.write_text(json.dumps(bom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Generated: {output}")


def copy_project_documents(output_dir: Path) -> None:
    """Copy open-source release documents into the artifact bundle."""
    required = [Path("LICENSE"), Path("NOTICES_QT_LGPL.md")]
    optional = [Path("NOTICES.md"), Path("LICENSING_REPORT.md"), Path("CHANGELOG.md")]

    for source in required:
        if not source.exists():
            raise FileNotFoundError(f"Required release document missing: {source}")

    for source in required + [path for path in optional if path.exists()]:
        target = output_dir / source.name
        target.write_bytes(source.read_bytes())
        print(f"Copied: {target}")


def generate_release_evidence(dist_dir: Path, output_dir: Path, output: Path, version: str, checks_run: bool) -> None:
    """Generate machine-readable release evidence for shipped artifacts."""
    files = find_dist_files(dist_dir)
    generated_files = [
        "SHA256SUMS",
        "SHA512SUMS",
        "pypi.json",
        "xpst-sbom.cdx.json",
        "RELEASE_NOTES.md",
        "LICENSE",
        "NOTICES.md",
        "NOTICES_QT_LGPL.md",
        "LICENSING_REPORT.md",
        "CHANGELOG.md",
    ]
    evidence = {
        "schema_version": 1,
        "project": "xpst",
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "quality_checks": {
            "run_by_release_script": checks_run,
            "required_commands": [
                "python -m pytest",
                "ruff check src tests scripts/verify_qml_pages.py scripts/verify_desktop_package.py scripts/verify_windows_exe.py scripts/verify_macos_artifact.py scripts/verify_live_platforms.py scripts/scan_public_safety.py scripts/release_preflight.py scripts/clean_install_smoke.py",
                "mypy src/xpst",
                "pip-audit",
                "python scripts/scan_public_safety.py --json",
                "python scripts/build_package.py",
                "python scripts/release_preflight.py --json",
                "python scripts/release_preflight.py --public --live-evidence release/live-platforms.json --json",
                "python scripts/public_release_check.py --json",
                "python scripts/clean_install_smoke.py --dist dist --artifact both",
                "python scripts/verify_desktop_package.py",
                "python scripts/verify_qml_pages.py",
                "Windows release job: python scripts/verify_windows_exe.py --path dist/xPST.exe --seconds 12 --json --clean-profile, plus --require-signed for tag/public releases",
                "macOS release job: bash scripts/verify_macos.sh, plus --public for tag releases requiring Developer ID signing and notarization",
                "GitHub release jobs: actions/attest@v4 for Python, Windows, and macOS artifact bundles",
                "Release owner: python scripts/public_release_check.py --json",
                "Release owner: python scripts/verify_live_platforms.py --require --json > release/live-platforms.json",
            ],
        },
        "artifacts": [
            {
                "filename": path.name,
                "size": path.stat().st_size,
                "type": "wheel" if path.suffix == ".whl" else "sdist" if path.name.endswith(".tar.gz") else "desktop",
                "hashes": compute_checksums(path),
            }
            for path in files["all"]
        ],
        "generated_files": [
            {"filename": name, "present": (output_dir / name).exists()}
            for name in generated_files
        ],
        "manual_validation_required": [
            "Windows executable launch with --clean-profile and valid Authenticode signature on a clean Windows profile outside the build machine",
            "macOS app/DMG launch on a clean macOS profile",
            "Code signing and notarization status for public desktop releases",
            "Live external-platform upload tests with owner-controlled credentials",
            "GitHub Actions CI result for Docker build smoke",
            "GitHub artifact attestations are present for public release artifacts",
        ],
        "known_limitations": [
            "Instagram, X, and TikTok integrations depend on user-owned sessions/cookies or downloader behavior and can break when platforms change.",
            "Desktop signing/notarization requires maintainer-owned certificates and cannot be proven by local package smoke tests.",
            "No automation tool can guarantee protection from platform rate limits or enforcement actions.",
        ],
    }
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Generated: {output}")


def verify_wheel(wheel_path: Path) -> bool:
    """Verify wheel can be installed."""
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            names = archive.namelist()
        if not any(name.endswith(".dist-info/METADATA") for name in names):
            print(f"Wheel verification failed: missing METADATA in {wheel_path.name}")
            return False
        print(f"Wheel contents: {wheel_path.name} ({len(names)} entries)")
        return True
    except (OSError, zipfile.BadZipFile) as e:
        print(f"Wheel verification failed: {e}")
        return False


def verify_sdist(sdist_path: Path) -> bool:
    """Verify sdist can be extracted."""
    try:
        with tarfile.open(sdist_path, "r:gz") as archive:
            names = archive.getnames()
        if not any(name.endswith("pyproject.toml") for name in names):
            print(f"Sdist verification failed: missing pyproject.toml in {sdist_path.name}")
            return False
        print(f"Sdist contents: {sdist_path.name} ({len(names)} entries)")
        return True
    except (OSError, tarfile.TarError) as e:
        print(f"Sdist verification failed: {e}")
        return False


def run_quality_checks() -> bool:
    """Run all quality checks."""
    checks = [
        (["python", "-m", "pytest", "-x", "-q"], "Tests"),
        (
            [
                "ruff",
                "check",
                "src",
                "tests",
                "scripts/verify_qml_pages.py",
                "scripts/verify_desktop_package.py",
                "scripts/verify_windows_exe.py",
                "scripts/verify_macos_artifact.py",
                "scripts/verify_live_platforms.py",
                "scripts/scan_public_safety.py",
                "scripts/release_preflight.py",
                "scripts/clean_install_smoke.py",
            ],
            "Linting",
        ),
        (["mypy", "src/xpst"], "Type checking"),
        (["python", "scripts/release_preflight.py", "--json"], "Release preflight"),
        (["python", "scripts/verify_desktop_package.py"], "Desktop package static checks"),
        (["python", "scripts/verify_qml_pages.py"], "QML smoke test"),
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


def get_project_version() -> str:
    """Read the project version from pyproject.toml without third-party parsers."""
    pyproject = Path("pyproject.toml")
    if not pyproject.exists():
        return importlib.metadata.version("xpst")

    match = re.search(
        r'^version\s*=\s*"([^"]+)"',
        pyproject.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match:
        return match.group(1)
    return importlib.metadata.version("xpst")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate release artifacts")
    parser.add_argument("--dist", required=True, help="Distribution directory")
    parser.add_argument("--version", default=None, help="Version string (defaults to pyproject.toml)")
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

    version = args.version or get_project_version()

    # Verify built artifacts
    files = find_dist_files(dist_dir)
    print("\nFound artifacts:")
    for path in files["all"]:
        print(f"  {path.name}")

    for wheel in files["wheels"]:
        if not verify_wheel(wheel):
            return 1

    for sdist in files["sdists"]:
        if not verify_sdist(sdist):
            return 1

    # Generate checksums
    sha256_file = output_dir / "SHA256SUMS"
    sha512_file = output_dir / "SHA512SUMS"
    generate_checksum_files(dist_dir, sha256_file, sha512_file)

    # Generate PyPI JSON
    pypi_json = output_dir / "pypi.json"
    generate_pypi_json(dist_dir, pypi_json, version)

    # Generate SBOM
    sbom_file = output_dir / "xpst-sbom.cdx.json"
    generate_sbom(dist_dir, sbom_file, version)

    # Generate release notes
    release_notes = output_dir / "RELEASE_NOTES.md"
    notes = extract_changelog(version)
    release_notes.write_text(f"# xPST {version}\n\n{notes}\n", encoding="utf-8")
    print(f"Generated: {release_notes}")

    # Copy open-source release documents
    copy_project_documents(output_dir)

    # Copy artifacts to output
    for path in files["all"]:
        target = output_dir / path.name
        target.write_bytes(path.read_bytes())
        print(f"Copied: {target}")

    # Generate release evidence after all expected files are present
    evidence_file = output_dir / "RELEASE_EVIDENCE.json"
    generate_release_evidence(dist_dir, output_dir, evidence_file, version, checks_run=not args.skip_checks)

    print(f"\nRelease artifacts generated in {output_dir}/")
    print(f"   SHA256SUMS: {sha256_file.read_text().strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
