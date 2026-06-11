"""Release readiness preflight for local and public xPST releases."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BUILD_ACTION = "Run python scripts/build_package.py before release preflight."
LIVE_EVIDENCE_ACTION = (
    "Run python scripts/verify_live_platforms.py --require --json > release/live-platforms.json "
    "and pass --live-evidence release/live-platforms.json."
)


@dataclass
class PreflightCheck:
    id: str
    ok: bool
    severity: str
    message: str
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ok": self.ok,
            "severity": self.severity,
            "message": self.message,
            "action": self.action,
        }


def _artifact_exists(dist_dir: Path, patterns: list[str]) -> bool:
    return any(any(dist_dir.glob(pattern)) for pattern in patterns)


def _env_present(names: list[str]) -> bool:
    return all(bool(os.environ.get(name)) for name in names)


def _live_evidence_result(path: Path | None) -> tuple[bool, str]:
    if path is None:
        return False, "Live platform validation evidence is not configured."
    evidence_path = path.expanduser()
    if not evidence_path.exists():
        return False, f"Live platform validation evidence is missing: {evidence_path}"
    try:
        data = json.loads(evidence_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Live platform validation evidence is unreadable: {exc}"

    if data.get("ok") is not True:
        return False, "Live platform validation evidence did not pass."
    if data.get("mode") != "required":
        return False, "Live platform validation evidence was not run in required mode."

    results = data.get("results", [])
    if not isinstance(results, list) or not results:
        return False, "Live platform validation evidence has no platform results."
    not_passed = [
        str(item.get("platform", "unknown"))
        for item in results
        if not isinstance(item, dict) or item.get("status") != "passed" or item.get("ok") is not True
    ]
    if not_passed:
        return False, f"Live platform validation has incomplete platforms: {', '.join(not_passed)}"
    return True, f"Live platform validation evidence passed: {evidence_path}"


def build_release_preflight(
    dist_dir: Path = ROOT / "dist",
    public: bool = False,
    live_evidence: Path | None = None,
) -> dict[str, Any]:
    """Return release readiness checks.

    Default mode is suitable for CI and local release candidates. Public mode is
    intentionally stricter and requires desktop artifacts/signing prerequisites.
    """
    dist_dir = dist_dir.expanduser()
    strict_severity = "error" if public else "warning"
    checks: list[PreflightCheck] = []

    # Tag / version / CHANGELOG consistency (ISC-179): a release where these
    # disagree ships a lie. pyproject version must appear as a CHANGELOG
    # heading, and any tag being cut must match pyproject.
    version = None
    try:
        pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        try:
            import tomllib  # stdlib on 3.11+

            version = tomllib.loads(pyproject_text)["project"]["version"]
        except ImportError:  # 3.10: regex fallback, no extra dependency
            import re as _re

            match = _re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, _re.M)
            version = match.group(1) if match else None
    except Exception:
        pass
    changelog_text = ""
    try:
        changelog_text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        pass
    version_in_changelog = bool(
        version and (f"[{version}]" in changelog_text or f"## {version}" in changelog_text)
    )
    checks.append(
        PreflightCheck(
            id="version_changelog_consistency",
            ok=bool(version) and version_in_changelog,
            severity="error",
            message=(
                f"pyproject version {version} has a CHANGELOG entry."
                if version and version_in_changelog
                else f"pyproject version {version!r} has NO CHANGELOG heading — add one before tagging."
            ),
            action=f"Add a '## [{version}]' section to CHANGELOG.md and tag v{version}." if version else "Fix pyproject version.",
        )
    )

    checks.append(
        PreflightCheck(
            id="dist_dir",
            ok=dist_dir.exists(),
            severity="error",
            message=f"Distribution directory exists: {dist_dir}" if dist_dir.exists() else f"Missing distribution directory: {dist_dir}",
            action=BUILD_ACTION if not dist_dir.exists() else "",
        )
    )

    checks.append(
        PreflightCheck(
            id="wheel",
            ok=_artifact_exists(dist_dir, ["*.whl"]),
            severity="error",
            message="Wheel artifact is present." if _artifact_exists(dist_dir, ["*.whl"]) else "Wheel artifact is missing.",
            action=BUILD_ACTION,
        )
    )
    checks.append(
        PreflightCheck(
            id="sdist",
            ok=_artifact_exists(dist_dir, ["*.tar.gz"]),
            severity="error",
            message="Source distribution artifact is present." if _artifact_exists(dist_dir, ["*.tar.gz"]) else "Source distribution artifact is missing.",
            action=BUILD_ACTION,
        )
    )

    has_windows = _artifact_exists(dist_dir, ["xPST.exe", "*.exe", "*.msi"])
    checks.append(
        PreflightCheck(
            id="windows_desktop_artifact",
            ok=has_windows,
            severity=strict_severity,
            message="Windows desktop artifact is present." if has_windows else "Windows desktop artifact is not present.",
            action="Run the Windows release job and attach the signed executable/installer." if not has_windows else "",
        )
    )

    has_macos = _artifact_exists(dist_dir, ["xPST.app", "*.dmg", "*.pkg"])
    checks.append(
        PreflightCheck(
            id="macos_desktop_artifact",
            ok=has_macos,
            severity=strict_severity,
            message="macOS desktop artifact is present." if has_macos else "macOS desktop artifact is not present.",
            action="Run the macOS release job and attach the signed/notarized app or DMG." if not has_macos else "",
        )
    )

    windows_signing_env = _env_present(["WINDOWS_CERTIFICATE_BASE64", "WINDOWS_CERTIFICATE_PASSWORD"]) or _env_present(
        ["WINDOWS_CERTIFICATE_PATH", "WINDOWS_CERTIFICATE_PASSWORD"]
    )
    checks.append(
        PreflightCheck(
            id="windows_signing",
            ok=windows_signing_env or shutil.which("signtool.exe") is not None,
            severity=strict_severity,
            message="Windows signing prerequisite is configured."
            if windows_signing_env or shutil.which("signtool.exe") is not None
            else "Windows signing prerequisite is not configured.",
            action="Configure a signing certificate secret/path and Windows SDK signtool before public release.",
        )
    )

    macos_signing_env = _env_present(["MACOS_CODESIGN_IDENTITY"])
    notarization_env = _env_present(["APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"])
    current_system = platform.system()
    mac_tools = shutil.which("codesign") is not None and shutil.which("xcrun") is not None
    checks.append(
        PreflightCheck(
            id="macos_signing",
            ok=macos_signing_env and (current_system != "Darwin" or mac_tools),
            severity=strict_severity,
            message="macOS signing identity is configured." if macos_signing_env else "macOS signing identity is not configured.",
            action="Set MACOS_CODESIGN_IDENTITY and run signing on macOS before public release.",
        )
    )
    checks.append(
        PreflightCheck(
            id="macos_notarization",
            ok=notarization_env,
            severity=strict_severity,
            message="macOS notarization credentials are configured." if notarization_env else "macOS notarization credentials are not configured.",
            action="Set APPLE_ID, APPLE_TEAM_ID, and APPLE_APP_PASSWORD before public distribution outside local development.",
        )
    )

    evidence_path = live_evidence
    if evidence_path is None and os.environ.get("XPST_LIVE_PLATFORM_EVIDENCE"):
        evidence_path = Path(os.environ["XPST_LIVE_PLATFORM_EVIDENCE"])
    live_ok, live_message = _live_evidence_result(evidence_path)
    checks.append(
        PreflightCheck(
            id="live_platform_validation",
            ok=live_ok,
            severity=strict_severity,
            message=live_message,
            action=LIVE_EVIDENCE_ACTION,
        )
    )

    blocking = [check for check in checks if not check.ok and check.severity == "error"]
    warnings = [check for check in checks if not check.ok and check.severity == "warning"]
    return {
        "ok": not blocking,
        "mode": "public" if public else "local",
        "checks": [check.to_dict() for check in checks],
        "blocking": [check.to_dict() for check in blocking],
        "warnings": [check.to_dict() for check in warnings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check xPST release readiness")
    parser.add_argument("--dist", default=str(ROOT / "dist"), help="Distribution artifact directory")
    parser.add_argument("--public", action="store_true", help="Require public desktop release prerequisites")
    parser.add_argument(
        "--live-evidence",
        default=None,
        help="Path to JSON from scripts/verify_live_platforms.py --require --json",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = build_release_preflight(
        Path(args.dist),
        public=args.public,
        live_evidence=Path(args.live_evidence) if args.live_evidence else None,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for check in result["checks"]:
            status = "ok" if check["ok"] else check["severity"]
            print(f"{check['id']}: {status} - {check['message']}")
            if check["action"] and not check["ok"]:
                print(f"  action: {check['action']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
