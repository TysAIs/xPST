"""Verify macOS desktop release artifacts."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], timeout: int = 60) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "command": command, "returncode": None, "stdout": "", "stderr": str(exc)}
    return {
        "ok": result.returncode == 0,
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _app_checks(app_path: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    contents = app_path / "Contents"
    macos = contents / "MacOS"
    plist = contents / "Info.plist"

    checks.append({"id": "app_bundle", "ok": app_path.is_dir(), "message": f"{app_path} exists"})
    checks.append({"id": "contents_dir", "ok": contents.is_dir(), "message": "Contents directory exists"})
    checks.append({"id": "macos_dir", "ok": macos.is_dir(), "message": "Contents/MacOS directory exists"})
    checks.append({"id": "info_plist", "ok": plist.is_file(), "message": "Info.plist exists"})
    checks.append(
        {
            "id": "launcher_binary",
            "ok": macos.is_dir() and any(item.is_file() for item in macos.iterdir()),
            "message": "At least one launcher binary exists in Contents/MacOS",
        }
    )
    return checks


def _macos_security_checks(
    app_path: Path,
    dmg_path: Path | None,
    require_developer_id: bool = False,
    require_notarized: bool = False,
) -> list[dict[str, Any]]:
    if platform.system() != "Darwin":
        return [
            {
                "id": "macos_security_tools",
                "ok": True,
                "skipped": True,
                "message": "codesign/spctl/stapler checks run only on macOS.",
            }
        ]

    checks: list[dict[str, Any]] = []
    codesign = shutil.which("codesign")
    spctl = shutil.which("spctl")
    stapler = shutil.which("xcrun")

    if codesign:
        result = _run([codesign, "--verify", "--deep", "--strict", str(app_path)])
        checks.append({"id": "codesign_verify", "ok": result["ok"], "message": result["stderr"] or result["stdout"], "result": result})
        display = _run([codesign, "--display", "--verbose=4", str(app_path)])
        authority_text = "\n".join([display.get("stdout", ""), display.get("stderr", "")])
        developer_id = "Developer ID Application:" in authority_text
        checks.append(
            {
                "id": "developer_id_signature",
                "ok": developer_id or not require_developer_id,
                "message": "Developer ID signature found." if developer_id else "Developer ID signature not found.",
                "required": require_developer_id,
                "result": display,
            }
        )
    else:
        checks.append({"id": "codesign_verify", "ok": False, "message": "codesign not found"})
        if require_developer_id:
            checks.append({"id": "developer_id_signature", "ok": False, "message": "codesign not found", "required": True})

    if spctl:
        result = _run([spctl, "--assess", "--type", "execute", str(app_path)])
        checks.append({"id": "spctl_app_assess", "ok": result["ok"], "message": result["stderr"] or result["stdout"], "result": result})
    else:
        checks.append({"id": "spctl_app_assess", "ok": False, "message": "spctl not found"})

    if dmg_path and dmg_path.exists():
        if spctl:
            result = _run([spctl, "--assess", "--type", "open", "--context", "context:primary-signature", str(dmg_path)])
            checks.append({"id": "spctl_dmg_assess", "ok": result["ok"], "message": result["stderr"] or result["stdout"], "result": result})
        if stapler:
            result = _run(["xcrun", "stapler", "validate", str(dmg_path)])
            checks.append(
                {
                    "id": "stapler_validate",
                    "ok": result["ok"] or not require_notarized,
                    "message": result["stderr"] or result["stdout"],
                    "required": require_notarized,
                    "result": result,
                }
            )
        elif require_notarized:
            checks.append({"id": "stapler_validate", "ok": False, "message": "xcrun stapler not found", "required": True})
    else:
        checks.append({"id": "dmg_artifact", "ok": not require_notarized, "message": "DMG artifact is missing.", "required": require_notarized})

    return checks


def verify_macos_artifact(
    app_path: Path = ROOT / "dist" / "xPST.app",
    dmg_path: Path | None = None,
    require_developer_id: bool = False,
    require_notarized: bool = False,
) -> dict[str, Any]:
    """Return macOS artifact verification results."""
    app_path = app_path.expanduser()
    dmg_path = app_path.with_suffix(".dmg") if dmg_path is None else dmg_path.expanduser()

    checks = _app_checks(app_path)
    checks.extend(_macos_security_checks(app_path, dmg_path, require_developer_id, require_notarized))
    blocking = [check for check in checks if not check.get("ok") and not check.get("skipped")]
    return {
        "ok": not blocking,
        "app": str(app_path),
        "dmg": str(dmg_path),
        "require_developer_id": require_developer_id,
        "require_notarized": require_notarized,
        "checks": checks,
        "blocking": blocking,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify macOS xPST app and DMG artifacts")
    parser.add_argument("--app", default=str(ROOT / "dist" / "xPST.app"), help="Path to xPST.app")
    parser.add_argument("--dmg", default=None, help="Optional path to xPST.dmg")
    parser.add_argument("--require-developer-id", action="store_true", help="Fail unless the app uses a Developer ID Application signature")
    parser.add_argument("--require-notarized", action="store_true", help="Fail unless the DMG passes notarization/stapler validation")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = verify_macos_artifact(
        Path(args.app),
        Path(args.dmg) if args.dmg else None,
        require_developer_id=args.require_developer_id,
        require_notarized=args.require_notarized,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for check in result["checks"]:
            status = "skipped" if check.get("skipped") else "ok" if check["ok"] else "failed"
            print(f"{check['id']}: {status} - {check['message']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
