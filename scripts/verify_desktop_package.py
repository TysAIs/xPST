"""Static packaging checks for xPST desktop bundles.

The full PyInstaller builds still need to run on Windows/macOS, but these
checks catch common release failures before the expensive platform jobs:
missing QML data, missing icon assets, and hidden-import drift for modules
loaded dynamically by the desktop backend.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_HIDDEN_IMPORTS = {
    "xpst",
    "xpst.cli",
    "xpst.desktop_app.backend",
    "xpst.desktop_app.models",
    "xpst.diagnostics",
    "xpst.providers",
    "xpst.readiness",
    "xpst.updater",
    "xpst.platforms.base",
    "xpst.platforms.youtube",
    "xpst.platforms.instagram",
    "xpst.platforms.x",
    "xpst.sources.base",
    "xpst.sources.local",
    "xpst.sources.tiktok",
    "xpst.sources.youtube",
    "xpst.sources.instagram",
    "xpst.sources.x",
    "xpst.utils.credentials",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtWidgets",
}


def _hidden_imports(text: str) -> set[str]:
    match = re.search(r"hiddenimports=\[(.*?)\],", text, flags=re.DOTALL)
    if not match:
        return set()
    return set(re.findall(r"""["']([^"'\n]+)["']""", match.group(1)))


def _check_spec(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    hidden_imports = _hidden_imports(text)
    missing_imports = sorted(REQUIRED_HIDDEN_IMPORTS - hidden_imports)
    issues: list[str] = []

    if 'str(qml_dir), "xpst/desktop_app/qml"' not in text:
        issues.append("QML directory is not bundled into xpst/desktop_app/qml")
    if "src_dir / \"xpst\" / \"desktop_app\" / \"main.py\"" not in text:
        issues.append("Desktop main.py is not the PyInstaller entrypoint")

    if missing_imports:
        issues.append("Missing hidden imports: " + ", ".join(missing_imports))

    if path.name == "build_windows.spec":
        icon_path = ROOT / "assets" / "icon.ico"
        if not icon_path.exists():
            issues.append("Windows icon asset is missing: assets/icon.ico")
        if "console=False" not in text:
            issues.append("Windows desktop build should be windowed with console=False")

    if path.name == "build_macos.spec":
        icon_path = ROOT / "docs" / "assets" / "xpst-icon.icns"
        if not icon_path.exists():
            issues.append("macOS icon asset is missing: docs/assets/xpst-icon.icns")
        if "BUNDLE(" not in text or 'name="xPST.app"' not in text:
            issues.append("macOS spec does not create xPST.app")
        if "bundle_identifier=\"com.xpst.app\"" not in text:
            issues.append("macOS bundle identifier is missing")

    return {
        "path": str(path.relative_to(ROOT)),
        "ok": not issues,
        "issues": issues,
    }


def verify_desktop_package(root: Path = ROOT) -> dict[str, Any]:
    """Return desktop package static verification results."""
    specs = [root / "build_windows.spec", root / "build_macos.spec"]
    results = [_check_spec(path) for path in specs]

    qml_pages = sorted((root / "src" / "xpst" / "desktop_app" / "qml" / "pages").glob("*.qml"))
    qml_issue = None if qml_pages else "No QML pages found"
    if qml_issue:
        results.append({"path": "src/xpst/desktop_app/qml/pages", "ok": False, "issues": [qml_issue]})

    return {
        "ok": all(item["ok"] for item in results),
        "checks": results,
        "qml_pages": [page.name for page in qml_pages],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify desktop packaging inputs")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = verify_desktop_package()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for check in result["checks"]:
            status = "ok" if check["ok"] else "failed"
            print(f"{check['path']}: {status}")
            for issue in check["issues"]:
                print(f"  - {issue}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
