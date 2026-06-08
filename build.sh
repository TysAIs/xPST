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
