#!/bin/bash
set -euo pipefail

APP_PATH="${1:?Usage: sign_macos.sh <path-to-.app>}"

codesign --force --deep --sign - "$APP_PATH"
codesign --verify "$APP_PATH"
echo "✅ Signed and verified: $APP_PATH"

DMG_PATH="${APP_PATH%.app}.dmg"
hdiutil create -volname "xPST" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"
echo "✅ DMG created: $DMG_PATH"
