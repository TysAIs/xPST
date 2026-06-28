"""Build xPST wheel and source distribution without repo path shadowing."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_package(out_dir: Path = ROOT / "dist") -> int:
    """Run ``python -m build`` from outside the repo checkout.

    PyInstaller creates a local ``build/`` directory. When the command is run
    from the repository root, that directory can shadow the PyPA ``build``
    package and make ``python -m build`` fail. Running from a temporary working
    directory keeps the standard build command reliable.
    """
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xpst-build-") as work_dir:
        completed = subprocess.run(
            [sys.executable, "-m", "build", str(ROOT), "--outdir", str(out_dir)],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
    if completed.returncode == 0:
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        return 0

    if completed.returncode != 0 and shutil.which("uv") is not None:
        fallback = subprocess.run(
            ["uv", "build", "--out-dir", str(out_dir)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if fallback.stdout:
            print(fallback.stdout, end="")
        if fallback.stderr:
            print(fallback.stderr, end="", file=sys.stderr)
        if fallback.returncode != 0:
            if completed.stdout:
                print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
        return fallback.returncode

    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Build xPST wheel and source distribution")
    parser.add_argument("--outdir", default=str(ROOT / "dist"), help="Output directory for built artifacts")
    args = parser.parse_args()
    return build_package(Path(args.outdir))


if __name__ == "__main__":
    raise SystemExit(main())
