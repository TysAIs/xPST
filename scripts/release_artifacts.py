"""Prepare release support artifacts for xPST.

Generates SHA-256 checksums and, when pip-audit is available, a CycloneDX SBOM.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(dist_dir: Path, output: Path) -> None:
    artifacts = [
        path
        for path in sorted(dist_dir.iterdir())
        if path.is_file()
        and path.name != output.name
        and not path.name.endswith(".cdx.json")
        and not path.name.endswith(".sha256")
    ]
    lines = [f"{_sha256(path)}  {path.name}" for path in artifacts]
    output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def write_sbom(dist_dir: Path, output: Path) -> bool:
    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "--format",
        "cyclonedx-json",
        "--output",
        str(output),
    ]
    result = subprocess.run(cmd, cwd=dist_dir.parent, text=True)
    return result.returncode == 0 and output.exists()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate xPST release checksums and SBOM.")
    parser.add_argument("--dist", default="dist", help="Directory containing release artifacts.")
    args = parser.parse_args()

    dist_dir = Path(args.dist).resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)

    checksums = dist_dir / "SHA256SUMS"
    write_checksums(dist_dir, checksums)
    print(f"Wrote {checksums}")

    sbom = dist_dir / "xpst-sbom.cdx.json"
    if write_sbom(dist_dir, sbom):
        print(f"Wrote {sbom}")
    else:
        print("SBOM generation skipped or failed; install pip-audit to generate it.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

