"""Smoke-test a Windows desktop executable artifact."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _authenticode_status(path: Path) -> dict[str, Any]:
    if platform.system() != "Windows":
        return {"available": False, "status": "unsupported", "message": "Authenticode is only checked on Windows."}

    escaped_path = str(path).replace("'", "''")
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            f"$sig = Get-AuthenticodeSignature -LiteralPath '{escaped_path}'; "
            "[Console]::Out.Write(($sig.Status.ToString()) + \"`n\" + ($sig.StatusMessage.ToString()))"
        ),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "status": "error", "message": str(exc)}

    lines = result.stdout.splitlines()
    status = lines[0].strip() if lines else "unknown"
    message = "\n".join(lines[1:]).strip() if len(lines) > 1 else result.stderr.strip()
    return {
        "available": True,
        "status": status,
        "message": message,
        "signed": status == "Valid",
    }


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if platform.system() == "Windows":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _clean_profile_env(base_env: dict[str, str], profile_dir: Path) -> dict[str, str]:
    """Return an environment that redirects user-writable app data to a temp profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    appdata = profile_dir / "AppData" / "Roaming"
    local_appdata = profile_dir / "AppData" / "Local"
    temp_dir = profile_dir / "Temp"
    for directory in (appdata, local_appdata, temp_dir):
        directory.mkdir(parents=True, exist_ok=True)

    env = base_env.copy()
    env["USERPROFILE"] = str(profile_dir)
    env["HOME"] = str(profile_dir)
    env["APPDATA"] = str(appdata)
    env["LOCALAPPDATA"] = str(local_appdata)
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["XPST_CLEAN_PROFILE_SMOKE"] = "1"
    return env


def _remove_tree_best_effort(path: str) -> None:
    """Remove a temp tree without failing a passed smoke on late Windows locks."""
    if not path:
        return
    for _attempt in range(3):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(path, ignore_errors=True)


def verify_windows_exe(
    path: Path,
    seconds: int = 12,
    require_signed: bool = False,
    clean_profile: bool = False,
) -> dict[str, Any]:
    """Launch a Windows executable and verify it does not crash immediately."""
    path = path.expanduser().resolve()
    if not path.exists():
        return {
            "ok": False,
            "artifact": str(path),
            "error": f"Artifact not found: {path}",
        }
    if platform.system() != "Windows":
        return {
            "ok": False,
            "artifact": str(path),
            "error": "Windows executable smoke tests must run on Windows.",
        }

    temp_profile = tempfile.mkdtemp(prefix="xpst-clean-profile-") if clean_profile else ""
    profile_dir = temp_profile
    try:
        env = os.environ.copy()
        if clean_profile:
            env = _clean_profile_env(env, Path(temp_profile))
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        process = subprocess.Popen([str(path)], env=env)
        deadline = time.monotonic() + seconds
        exit_code: int | None = None

        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                break
            time.sleep(0.25)

        if exit_code is None:
            _stop_process_tree(process)
    finally:
        _remove_tree_best_effort(temp_profile)

    signature = _authenticode_status(path)
    stayed_alive = exit_code is None
    clean_exit = exit_code == 0
    launch_ok = stayed_alive or clean_exit
    signature_ok = not require_signed or bool(signature.get("signed"))
    return {
        "ok": launch_ok and signature_ok,
        "artifact": str(path),
        "seconds": seconds,
        "stayed_alive": stayed_alive,
        "exit_code": exit_code,
        "require_signed": require_signed,
        "clean_profile": clean_profile,
        "profile_dir": profile_dir,
        "signature": signature,
        "error": "Executable is not signed with a valid Authenticode signature." if launch_ok and not signature_ok else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a Windows xPST executable")
    parser.add_argument("--path", default=str(ROOT / "dist" / "xPST.exe"), help="Executable path")
    parser.add_argument("--seconds", type=int, default=12, help="Seconds the app must survive before termination")
    parser.add_argument("--require-signed", action="store_true", help="Fail unless Authenticode signature status is Valid")
    parser.add_argument("--clean-profile", action="store_true", help="Launch with an isolated temporary user profile")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = verify_windows_exe(
        Path(args.path),
        seconds=args.seconds,
        require_signed=args.require_signed,
        clean_profile=args.clean_profile,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "ok" if result["ok"] else "failed"
        print(f"{Path(result['artifact']).name}: {status}")
        if "error" in result:
            print(result["error"])
        elif result.get("stayed_alive"):
            print(f"Stayed alive for {result['seconds']} seconds.")
        else:
            print(f"Exited with code {result.get('exit_code')}.")
        signature = result.get("signature", {})
        if signature:
            print(f"Signature: {signature.get('status')}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
