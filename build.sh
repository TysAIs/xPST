#!/usr/bin/env bash
# Build xPST standalone bundles with PyInstaller.
# Usage:
#   ./build.sh              # Build for current platform
#   ./build.sh macos        # Build macOS .app bundle
#   ./build.sh windows      # Build Windows .exe (requires Wine + PyInstaller)
#   ./build.sh linux        # Build Linux standalone binary

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Detect platform
PLATFORM="${1:-}"
if [[ -z "$PLATFORM" ]]; then
    case "$(uname -s)" in
        Darwin*)  PLATFORM="macos" ;;
        Linux*)   PLATFORM="linux" ;;
        MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
        *)        echo "Unknown platform: $(uname -s)"; exit 1 ;;
    esac
fi

echo "=== Building xPST for $PLATFORM ==="

# Ensure venv is active and PyInstaller is installed
if ! command -v pyinstaller &>/dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller --quiet
fi

# Ensure PySide6 is installed
if ! python3 -c "import PySide6" 2>/dev/null; then
    echo "Installing PySide6..."
    pip install PySide6 --quiet
fi

# Clean previous builds
rm -rf build/ dist/

# Bake the canonical runtime version from pyproject.toml into the package so
# the frozen bundle's `xpst.__version__` (About page / CLI / diagnostics)
# EXACTLY matches the Info.plist version that build_macos.spec reads from the
# same pyproject.toml. Without this, a PyInstaller one-folder app ships no
# xpst dist-info, importlib.metadata raises PackageNotFoundError, and the
# runtime falls back to a stale hard-coded literal (v1.0.0 vs Info.plist
# 1.1.0 mismatch). Generated file is gitignored; never a tracked artifact.
python3 - <<'PY' 
import re
from pathlib import Path

root = Path.cwd()
src = root / "src" / "xpst"
ver = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE)
if not ver:
    raise SystemExit("pyproject.toml has no version = \"...\" line")
src.mkdir(parents=True, exist_ok=True)
(src / "_build_version.py").write_text(
    '"""Generated at build time by build.sh from pyproject.toml — never edit.\n\n'
    'Single source of truth for the runtime version so it can never drift\n'
    'from the Info.plist / release metadata. Git-ignored.\n"""\n'
    '__version__ = "%s"\n' % ver.group(1),
    encoding="utf-8",
)
print("Generated src/xpst/_build_version.py =", ver.group(1))
PY

case "$PLATFORM" in
    macos)
        echo "Building macOS .app bundle..."
        pyinstaller build_macos.spec --noconfirm --clean
        echo ""
        echo "✅ Build complete: dist/xPST.app"
        echo "   To distribute, create a DMG or zip the .app bundle."
        ;;
    windows)
        echo "Building Windows .exe..."
        pyinstaller build_windows.spec --noconfirm --clean
        echo ""
        echo "✅ Build complete: dist/xPST.exe"
        ;;
    linux)
        echo "Building Linux standalone binary..."
        pyinstaller \
            --name xPST \
            --windowed \
            --noconfirm \
            --clean \
            --add-data "src/xpst/desktop_app/qml:xpst/desktop_app/qml" \
            --hidden-import xpst \
            --hidden-import xpst.config \
            --hidden-import xpst.state \
            --hidden-import xpst.engine \
            --hidden-import xpst.cli \
            --hidden-import xpst.desktop_app.backend \
            --hidden-import xpst.desktop_app.models \
            --hidden-import xpst.plugins \
            --hidden-import PySide6.QtQuick \
            --hidden-import PySide6.QtQuickControls2 \
            --hidden-import PySide6.QtQml \
            --exclude-module tkinter \
            --exclude-module matplotlib \
            --exclude-module scipy \
            src/xpst/desktop_app/main.py
        echo ""
        echo "✅ Build complete: dist/xPST/"
        ;;
    *)
        echo "Unknown platform: $PLATFORM"
        echo "Usage: $0 [macos|windows|linux]"
        exit 1
        ;;
esac
