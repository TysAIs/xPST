#!/usr/bin/env bash
# Harden-sign and notarize a built xPST macOS bundle.
#
# Order matters: every nested Mach-O binary must be signed before the bundle that contains
# it, or codesign --verify --deep and the notary service will both reject the result.
#
# Usage:
#   scripts/macos-sign-and-notarize.sh --app dist/xPST.app \
#       --identity "Developer ID Application: ..." \
#       [--keychain-profile xpst-notary] [--skip-notarize] [--out-zip dist/xPST.zip]
#
# Everything is read from flags or the environment; nothing is hardcoded:
#   APPLE_SIGNING_IDENTITY   same as --identity
#   XPST_NOTARY_PROFILE      same as --keychain-profile (default: xpst-notary)
#
# Exits non-zero with an explicit reason when a requirement is missing. It never prints
# credentials: notarytool reads them from the keychain profile created by
# `xcrun notarytool store-credentials`.
set -euo pipefail

APP=""
IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
KEYCHAIN_PROFILE="${XPST_NOTARY_PROFILE:-xpst-notary}"
OUT_ZIP=""
DO_NOTARIZE=1

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="${2:-}"; shift 2 ;;
    --identity) IDENTITY="${2:-}"; shift 2 ;;
    --keychain-profile) KEYCHAIN_PROFILE="${2:-}"; shift 2 ;;
    --out-zip) OUT_ZIP="${2:-}"; shift 2 ;;
    --skip-notarize) DO_NOTARIZE=0; shift ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "$APP" ] || die "--app is required"
[ -d "$APP" ] || die "--app path does not exist: $APP"
case "$APP" in *.app) ;; *) die "--app must point at a .app bundle, got: $APP" ;; esac
command -v codesign >/dev/null 2>&1 || die "codesign not found (install Xcode command line tools)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_ENTITLEMENTS="$REPO_ROOT/src-tauri/entitlements.plist"
ENGINE_ENTITLEMENTS="$REPO_ROOT/src-tauri/entitlements-engine.plist"
[ -f "$APP_ENTITLEMENTS" ] || die "missing entitlements file: $APP_ENTITLEMENTS"
[ -f "$ENGINE_ENTITLEMENTS" ] || die "missing entitlements file: $ENGINE_ENTITLEMENTS"

if [ -z "$IDENTITY" ]; then
  printf 'ERROR: no signing identity supplied.\n\nAvailable code-signing identities:\n' >&2
  security find-identity -v -p codesigning >&2 || true
  cat >&2 <<'EOF'

This means the machine has no Developer ID certificate installed (or APPLE_SIGNING_IDENTITY
is not set). Creating one requires an Apple Developer Program membership and a one-time
portal step; see docs/SIGNING.md. Nothing here can substitute for it: a self-signed
certificate does not satisfy Gatekeeper.
EOF
  exit 2
fi

if ! security find-identity -v -p codesigning | grep -qF "$IDENTITY"; then
  printf 'ERROR: identity not found in the keychain: %s\n\nAvailable:\n' "$IDENTITY" >&2
  security find-identity -v -p codesigning >&2 || true
  exit 2
fi

printf 'Signing %s\n' "$APP"
note "identity: $IDENTITY"
note "entitlements (app):    $APP_ENTITLEMENTS"
note "entitlements (engine): $ENGINE_ENTITLEMENTS"

# --- 1. enumerate every nested Mach-O binary, deepest first -------------------------------
macho_files=()
while IFS= read -r -d '' candidate; do
  # Only regular files inside the bundle; `file` is the reliable detector for Mach-O.
  if file -b "$candidate" 2>/dev/null | grep -q 'Mach-O'; then
    macho_files+=("$candidate")
  fi
done < <(find "$APP" -type f -print0)

[ "${#macho_files[@]}" -gt 0 ] || die "no Mach-O binaries found inside $APP — is this a real bundle?"

# Deepest paths first so nested libraries are signed before their parents.
IFS=$'\n' sorted=($(printf '%s\n' "${macho_files[@]}" | awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)); unset IFS

nested=0
for f in "${sorted[@]}"; do
  [ "$f" = "$APP/Contents/MacOS/$(basename "$APP" .app)" ] && continue
  entitlements=()
  case "$f" in
    */Resources/binaries/engine/*) entitlements=(--entitlements "$ENGINE_ENTITLEMENTS") ;;
  esac
  codesign --force --options runtime --timestamp --sign "$IDENTITY" "${entitlements[@]}" "$f"
  nested=$((nested + 1))
done
note "signed $nested nested binaries"

# --- 2. sign the bundle itself ------------------------------------------------------------
codesign --force --options runtime --timestamp \
  --entitlements "$APP_ENTITLEMENTS" \
  --sign "$IDENTITY" "$APP"
note "signed the app bundle"

# --- 3. verify ----------------------------------------------------------------------------
codesign --verify --deep --strict --verbose=2 "$APP"
if command -v spctl >/dev/null 2>&1; then
  # Informational at this stage: an app is only accepted by Gatekeeper after notarization.
  spctl -a -vvv -t exec "$APP" 2>&1 | sed 's/^/  spctl: /' || true
fi

# --- 4. notarize + staple -----------------------------------------------------------------
if [ "$DO_NOTARIZE" -eq 1 ]; then
  command -v xcrun >/dev/null 2>&1 || die "xcrun not found; cannot notarize"
  ZIP="${OUT_ZIP:-$(mktemp -d)/$(basename "$APP" .app).zip}"
  mkdir -p "$(dirname "$ZIP")"
  ditto -c -k --keepParent "$APP" "$ZIP"
  note "submitting $(basename "$ZIP") for notarization (profile: $KEYCHAIN_PROFILE)"
  xcrun notarytool submit "$ZIP" --keychain-profile "$KEYCHAIN_PROFILE" --wait
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
  note "notarized and stapled"
  if command -v spctl >/dev/null 2>&1; then
    spctl -a -vvv -t exec "$APP" 2>&1 | sed 's/^/  spctl after staple: /'
  fi
  printf '%s\n' "$ZIP" > "$APP.notaryzip.path"
  note "notarization zip: $ZIP"
else
  note "notarization skipped (--skip-notarize): this bundle is signed but NOT notarized"
fi

printf '{"app":"%s","identity":"%s","nested_signed":%d,"notarized":%s}\n' \
  "$APP" "$IDENTITY" "$nested" "$([ "$DO_NOTARIZE" -eq 1 ] && echo true || echo false)"
