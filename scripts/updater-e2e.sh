#!/usr/bin/env bash
# xPST Tauri updater E2E (local self-hosted manifest).
#
# Proves the full Tauri updater loop on this machine (macOS aarch64):
#   1. Build app v0.1.0 -> xPST.app + updater artifacts (.app.tar.gz + .sig)
#   2. Serve a static manifest dir on http://127.0.0.1:9555 (latest.json, v0.1.0)
#   3. Rebuild as v0.2.0, replace artifact + latest.json
#   4. Launch the v0.1.0 app with XPST_UPDATER_CHECK=1 (opt-in updater check)
#   5. Assert the process downloads, installs and relaunches as v0.2.0
#      (proof = marker files in /private/tmp/xpst-updater-e2e/ written by
#       src-tauri/src/lib.rs on every boot — written by the app itself)
#
# Config overlay: the COMMITTED src-tauri/tauri.conf.json keeps the production
# wiring (see docs/tauri-updater-production.md). This script injects the local
# E2E endpoint, throwaway pubkey, createUpdaterArtifacts and version via
# `cargo tauri build --config <overlay>` — the committed config is never
# mutated.
#
# Exit codes:
#   0  success: v0.1.0 booted, update downloaded+installed, relaunched as v0.2.0
#   1  usage error (must run from repo root)
#   2  v0.1.0 build failed
#   3  v0.1.0 updater artifacts missing (.app.tar.gz / .sig)
#   4  v0.2.0 build failed
#   5  v0.2.0 updater artifacts missing
#   10 v0.1.0 app never booted (marker missing)
#   11 relaunch as v0.2.0 never happened (timeout)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# NOTE: must be the canonical path — /tmp is a symlink to /private/tmp on macOS
# and tauri-plugin-updater refuses to run when current_exe() crosses a symlink.
WORK=/private/tmp/xpst-updater-e2e
SERVE_DIR="$WORK/serve"
MANIFEST="$SERVE_DIR/updates/latest.json"
MARKERS="$WORK"
E2E_PORT=9555
E2E_URL="http://127.0.0.1:${E2E_PORT}"
E2E_KEY="$ROOT/.tauri/xpst-updater-e2e.key"

export PATH="$HOME/.cargo/bin:$PATH"
command -v cargo >/dev/null || { echo "cargo not found (need ~/.cargo/bin on PATH)" >&2; exit 2; }

# Throwaway E2E signing key (gitignored; NEVER use in production).
if [[ ! -f "$E2E_KEY" ]]; then
    echo "E2E signing key missing at $E2E_KEY"
    echo "Generate it once with: cargo tauri signer generate -w $E2E_KEY --password \"\" --ci"
    exit 2
fi
export TAURI_SIGNING_PRIVATE_KEY="$(cat "$E2E_KEY")"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""

rm -rf "$WORK"; mkdir -p "$SERVE_DIR/updates"
rm -f "$MARKERS"/started-*.txt "$MARKERS"/current.txt

APP_BUNDLE_DIR="src-tauri/target/release/bundle/macos"

write_overlay() { # $1 = version ; writes $WORK/e2e-overlay.json (merged over the
                  # committed tauri.conf.json via `cargo tauri build --config`)
    python3 - "$1" "$E2E_URL" "$WORK/e2e-overlay.json" <<'EOF'
import json, sys
version, base, out = sys.argv[1], sys.argv[2], sys.argv[3]
pubkey = open(".tauri/xpst-updater-e2e.key.pub").read().strip()
overlay = {
    "version": version,
    "bundle": {"createUpdaterArtifacts": True},
    "plugins": {"updater": {
        "endpoints": [f"{base}/updates/latest.json"],
        "dangerousInsecureTransportProtocol": True,  # local http-only endpoint
        "pubkey": pubkey,
    }},
}
json.dump(overlay, open(out, "w"), indent=2)
EOF
}

build_app() { # $1 = version
    echo "== [build] v$1 =="
    write_overlay "$1"
    # The bundle declares `resources: ["binaries/engine/"]` (the real PyInstaller
    # sidecar, gitignored). For the updater E2E a stub is enough — the shell
    # logs a FATAL engine line but boot markers + the updater check still run.
    mkdir -p src-tauri/binaries/engine
    if [[ ! -f src-tauri/binaries/engine/xpst-engine ]]; then
        : > src-tauri/binaries/engine/xpst-engine
        chmod +x src-tauri/binaries/engine/xpst-engine
    fi
    if ! cargo tauri build --bundles app --config "$WORK/e2e-overlay.json" \
            > "$WORK/build-v$1.log" 2>&1; then
        echo "BUILD FAILED (v$1) — tail of log:"; tail -40 "$WORK/build-v$1.log"; return 1
    fi
    tail -3 "$WORK/build-v$1.log"
}

write_latest_json() { # $1 = version, $2 = signature file
    python3 - "$1" "$2" "$E2E_URL" <<'EOF'
import json, sys
version, sigfile, base = sys.argv[1], sys.argv[2], sys.argv[3]
signature = open(sigfile).read().strip()
manifest = {
    "version": version,
    "notes": f"xPST updater E2E release {version}",
    "pub_date": "2026-08-28T00:00:00Z",
    "platforms": {
        "darwin-aarch64": {
            "signature": signature,
            "url": f"{base}/xPST.app.tar.gz",
        }
    },
}
json.dump(manifest, open("/private/tmp/xpst-updater-e2e/serve/updates/latest.json", "w"), indent=2)
open("/private/tmp/xpst-updater-e2e/serve/updates/latest.json", "a").write("\n")
print(f"latest.json -> {version} ({base}/xPST.app.tar.gz)")
EOF
}

# --- 1. build v0.1.0 ----------------------------------------------------------
build_app 0.1.0 || exit 2

TAR_GZ="$APP_BUNDLE_DIR/xPST.app.tar.gz"
SIG="$TAR_GZ.sig"
[[ -f "$TAR_GZ" && -f "$SIG" ]] || { echo "missing updater artifacts after v0.1.0 build"; ls -la "$APP_BUNDLE_DIR" || true; exit 3; }

# Keep a copy of the v0.1.0 bundle — the v0.2.0 build overwrites xPST.app.
cp -R "$APP_BUNDLE_DIR/xPST.app" "$WORK/xPST-0.1.0.app"

# --- 2. serve manifest (v0.1.0 initially) ------------------------------------
cp "$TAR_GZ" "$SERVE_DIR/xPST.app.tar.gz"
write_latest_json 0.1.0 "$SIG"

echo "== [serve] starting http server on 127.0.0.1:${E2E_PORT} =="
python3 -m http.server "$E2E_PORT" --bind 127.0.0.1 --directory "$SERVE_DIR" \
    > "$WORK/httpd.log" 2>&1 &
HTTPD_PID=$!
# Cleanup is scoped to THIS test's tmp dir — never pkill generic xPST paths
# (other agents may be running their own bundles).
trap 'kill $HTTPD_PID 2>/dev/null; pkill -f "/private/tmp/xpst-updater-e2e/" 2>/dev/null' EXIT

for i in $(seq 1 20); do
    curl -fsS "$E2E_URL/updates/latest.json" >/dev/null 2>&1 && break
    [[ $i -eq 20 ]] && { echo "local manifest server did not come up"; exit 3; }
    sleep 0.5
done
echo "server up: $(curl -fsS "$E2E_URL/updates/latest.json" | head -c 120)..."

# --- 3. build v0.2.0 + refresh manifest --------------------------------------
build_app 0.2.0 || exit 4
[[ -f "$TAR_GZ" && -f "$SIG" ]] || { echo "missing updater artifacts after v0.2.0 build"; exit 5; }
cp "$TAR_GZ" "$SERVE_DIR/xPST.app.tar.gz"
write_latest_json 0.2.0 "$SIG"

# --- 4. launch v0.1.0 with the updater-check trigger -------------------------
echo "== [run] launching v0.1.0 with XPST_UPDATER_CHECK=1 =="
APP_BIN="$(find "$WORK/xPST-0.1.0.app/Contents/MacOS" -maxdepth 1 -type f | head -1)"
[[ -x "$APP_BIN" ]] || { echo "v0.1.0 binary not found in copied bundle"; exit 10; }
XPST_UPDATER_CHECK=1 "$APP_BIN" > "$WORK/app-v0.1.0.log" 2>&1 &

wait_for() { # $1 = file, $2 = timeout seconds
    local waited=0
    while [[ ! -f "$1" ]]; do
        sleep 1; waited=$((waited+1))
        if [[ $waited -ge $2 ]]; then return 1; fi
    done
    return 0
}

wait_for "$MARKERS/started-0.1.0.txt" 30 || {
    echo "FAIL: v0.1.0 never booted (no marker)"; cat "$WORK/app-v0.1.0.log" || true; exit 10
}
echo "proof boot v0.1.0 : $(cat "$MARKERS/started-0.1.0.txt")"

# --- 5. assert relaunch as v0.2.0 --------------------------------------------
wait_for "$MARKERS/started-0.2.0.txt" 120 || {
    echo "FAIL: relaunch as v0.2.0 never happened (timeout)"
    echo "--- app log ---"; cat "$WORK/app-v0.1.0.log" || true
    echo "--- current marker ---"; cat "$MARKERS/current.txt" 2>/dev/null || true
    echo "--- httpd log ---"; tail -20 "$WORK/httpd.log" || true
    exit 11
}

sleep 2 # let the relaunched process settle
RUNNING=$(pgrep -f "/private/tmp/xpst-updater-e2e/.*MacOS/xpst" 2>/dev/null | wc -l | tr -d ' ' || true)

echo ""
echo "================ UPDATER E2E: PASS ================"
echo "boot v0.1.0   : $(cat "$MARKERS/started-0.1.0.txt")"
echo "boot v0.2.0   : $(cat "$MARKERS/started-0.2.0.txt")"
echo "running procs : $RUNNING"
echo "---------------------------------------------------"
echo "app stdout (v0.1.0 process, includes relaunch):"
cat "$WORK/app-v0.1.0.log" || true
echo "==================================================="
exit 0
