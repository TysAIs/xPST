#!/usr/bin/env bash
set -euo pipefail

APP_PATH="${1:?Usage: sign_macos.sh <path-to-.app>}"
IDENTITY="${MACOS_CODESIGN_IDENTITY:--}"
APPLE_ID="${APPLE_ID:-}"
APPLE_TEAM_ID="${APPLE_TEAM_ID:-}"
APPLE_APP_PASSWORD="${APPLE_APP_PASSWORD:-}"

codesign --force --deep --sign "$IDENTITY" "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"
echo "Signed and verified: $APP_PATH"

DMG_PATH="${APP_PATH%.app}.dmg"
hdiutil create -volname "xPST" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"
echo "DMG created: $DMG_PATH"

if [[ -n "$APPLE_ID" && -n "$APPLE_TEAM_ID" && -n "$APPLE_APP_PASSWORD" ]]; then
  xcrun notarytool submit "$DMG_PATH" \
    --apple-id "$APPLE_ID" \
    --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --wait
  xcrun stapler staple "$DMG_PATH"
  spctl --assess --type open --context context:primary-signature "$DMG_PATH"
  echo "Notarized and stapled: $DMG_PATH"
else
  echo "Notarization skipped. Set APPLE_ID, APPLE_TEAM_ID, and APPLE_APP_PASSWORD to enable it."
fi
