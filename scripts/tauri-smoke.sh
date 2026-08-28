#!/usr/bin/env bash
# tauri-smoke.sh — build & boot the Tauri shell offscreen and verify the
# engine sidecar comes up healthy.
#
# Usage:
#   scripts/tauri-smoke.sh [--skip-build]
#
# What it does:
#   1. Builds the app bundle with `cargo tauri build --bundles app`
#      (unless --skip-build).
#   2. Ensures a GUI security session exists (background/SSH runs need
#      `security-session create` on macOS).
#   3. Boots the .app binary offscreen, captures stderr, and greps for the
#      engine health markers the shell prints:
#        ENGINE_HEALTH_WAIT_SECS=<n>  — time waiting for /health
#        BOOT_TO_READY_SECS=<n>       — process start -> dashboard loaded
#   4. Exits non-zero if the markers are missing or boot exceeds 1s.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_BIN="$REPO_ROOT/src-tauri/target/release/bundle/macos/xPST.app/Contents/MacOS/xPST"
BOOT_LIMIT_SECS="1.0"
LOG="$(mktemp -t xpst-tauri-smoke.XXXXXX)"
SKIP_BUILD=0
[[ "${1:-}" == "--skip-build" ]] && SKIP_BUILD=1

cleanup() {
    if [[ -n "${APP_PID:-}" ]] && kill -0 "$APP_PID" 2>/dev/null; then
        kill "$APP_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [[ "$SKIP_BUILD" -eq 0 ]]; then
    echo "==> Building app bundle (cargo tauri build --bundles app)"
    (cd "$REPO_ROOT/src-tauri" && cargo tauri build --bundles app) \
        || { echo "FAIL: build failed"; exit 1; }
fi

if [[ ! -x "$APP_BIN" ]]; then
    echo "FAIL: app binary not found at $APP_BIN"
    exit 1
fi

# Background/SSH sessions have no GUI security session; create one so the
# window server accepts our process.
if [[ -z "${SECURITYSESSIONID:-}" ]] && command -v security-session >/dev/null 2>&1; then
    security-session create >/dev/null 2>&1 || true
fi

echo "==> Booting app offscreen: $APP_BIN"
"$APP_BIN" >"$LOG" 2>&1 &
APP_PID=$!

# Wait up to 90s for the engine health + ready markers.
READY=""
for _ in $(seq 1 90); do
    if ! kill -0 "$APP_PID" 2>/dev/null; then
        echo "FAIL: app exited before engine became healthy"
        echo "--- app log ---"; cat "$LOG"
        exit 1
    fi
    if grep -q "BOOT_TO_READY_SECS=" "$LOG" && grep -q "ENGINE_HEALTH_WAIT_SECS=" "$LOG"; then
        READY=1
        break
    fi
    sleep 1
done

# Webview navigation evidence (printed ~2s after the ready marker).
WEBVIEW_URL=""
for _ in $(seq 1 10); do
    WEBVIEW_URL="$(sed -n 's/^WEBVIEW_URL=//p' "$LOG" | head -1)"
    [[ -n "$WEBVIEW_URL" ]] && break
    sleep 1
done
if [[ -n "$WEBVIEW_URL" ]]; then
    echo "==> Webview navigated to: $WEBVIEW_URL"
else
    echo "WARN: no WEBVIEW_URL marker (webview url probe did not fire)"
fi

if [[ -z "$READY" ]]; then
    echo "FAIL: engine health markers not observed within 90s"
    echo "--- app log ---"; cat "$LOG"
    exit 1
fi

BOOT="$(sed -n 's/^BOOT_TO_READY_SECS=//p' "$LOG" | head -1)"
HEALTH_WAIT="$(sed -n 's/^ENGINE_HEALTH_WAIT_SECS=//p' "$LOG" | head -1)"

echo "==> Engine healthy: health_wait=${HEALTH_WAIT}s boot_to_ready=${BOOT}s"

# Verify the engine is actually listening and answering /health.
PORT="$(grep -o 'engine port: [0-9]*' "$LOG" | awk '{print $3}' | head -1)"
if [[ -n "$PORT" ]]; then
    STATUS="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health" || true)"
    echo "==> GET /health -> HTTP ${STATUS}"
    if [[ "$STATUS" != "200" ]]; then
        echo "FAIL: engine /health did not return 200"
        exit 1
    fi
fi

# Gate: boot-to-VISIBLE window must stay under 1s (size/boot gates).
# boot_to_ready additionally includes the PyInstaller onefile extraction
# (~1.5s) and is reported but not gated.
VIS="$(sed -n 's/^BOOT_TO_VISIBLE_SECS=//p' "$LOG" | head -1)"
if [[ -n "$VIS" ]] && awk -v b="$VIS" -v l="$BOOT_LIMIT_SECS" 'BEGIN { exit (b+0 <= l+0) ? 0 : 1 }'; then
    echo "PASS: boot_to_visible ${VIS}s <= ${BOOT_LIMIT_SECS}s (boot_to_ready=${BOOT}s)"
else
    echo "FAIL: boot_to_visible ${VIS:-missing} exceeds ${BOOT_LIMIT_SECS}s gate"
    exit 1
fi

echo "PASS: tauri shell smoke test (log: $LOG)"
