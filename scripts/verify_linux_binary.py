"""Smoke-test a Linux desktop binary artifact.

Mirrors scripts/verify_windows_exe.py for the Linux release lane (W3-2):
launch the built binary headless (QT_QPA_PLATFORM=offscreen), confirm it does
not crash on startup, and report a sha256 digest for the artifact (the CI
attestation step signs it separately).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def sha256_digest(path: Path) -> str:
    """Return the hex sha256 digest of a file (streamed, constant memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def verify_linux_binary(path: Path, seconds: int = 12) -> dict[str, Any]:
    """Launch a Linux binary headless and verify it does not crash immediately.

    The artifact-not-found and wrong-OS branches return a result dict WITHOUT
    launching anything, so they are safe to unit-test on any platform.
    """
    path = path.expanduser().resolve()
    if not path.exists():
        return {
            "ok": False,
            "artifact": str(path),
            "error": f"Artifact not found: {path}",
        }
    if platform.system() != "Linux":
        return {
            "ok": False,
            "artifact": str(path),
            "error": "Linux binary smoke tests must run on Linux.",
        }

    digest = sha256_digest(path)
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Capture output so a failing launch is diagnosable from the JSON
    # instead of a blind exit code.
    process = subprocess.Popen(
        [str(path)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + seconds
    exit_code: int | None = None

    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            break
        time.sleep(0.25)

    output_tail = ""
    if exit_code is None:
        _stop_process(process)
    try:
        captured, _ = process.communicate(timeout=10)
        output_tail = (captured or "")[-2000:]
    except Exception:
        pass

    stayed_alive = exit_code is None
    clean_exit = exit_code == 0
    launch_ok = stayed_alive or clean_exit
    return {
        "ok": launch_ok,
        "artifact": str(path),
        "seconds": seconds,
        "stayed_alive": stayed_alive,
        "exit_code": exit_code,
        "sha256": digest,
        "output_tail": output_tail,
        "error": "" if launch_ok else f"Binary exited with code {exit_code} before {seconds}s.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a Linux xPST binary")
    parser.add_argument("--path", default=str(ROOT / "dist" / "xPST"), help="Binary path")
    parser.add_argument("--seconds", type=int, default=12, help="Seconds the app must survive before termination")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = verify_linux_binary(Path(args.path), seconds=args.seconds)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "ok" if result["ok"] else "failed"
        print(f"{Path(result['artifact']).name}: {status}")
        if result.get("error"):
            print(result["error"])
        elif result.get("stayed_alive"):
            print(f"Stayed alive for {result['seconds']} seconds.")
        else:
            print(f"Exited with code {result.get('exit_code')}.")
        if result.get("sha256"):
            print(f"sha256: {result['sha256']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
