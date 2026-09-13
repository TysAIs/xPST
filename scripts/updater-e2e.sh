#!/usr/bin/env bash
# Run a local, signed Tauri updater round-trip.
#
# The script builds two macOS app versions, serves the second version's
# artifact and manifest over HTTP, then launches version A with the updater
# check enabled.  The Rust shell writes boot markers itself, so a passing run
# proves that version B actually relaunched.
#
# Usage:
#   scripts/updater-e2e.sh
#   scripts/updater-e2e.sh --manifest-url http://127.0.0.1:9555/updates/latest.json
#
# A throwaway local Tauri signing key is required.  Set
# XPST_E2E_SIGNING_KEY or create the default key with:
#   cargo tauri signer generate -w .tauri/xpst-updater-e2e.key --password "" --ci
# Never use that key for a release.
#
# Exit codes:
#   0  version A booted, updated, and relaunched as version B
#   2  prerequisites or version A build failed
#   3  version A updater artifact/signature or local server failed
#   4  version B build failed
#   5  version B updater artifact/signature failed
#   10 version A never booted
#   11 version B never relaunched

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${XPST_PYTHON:-python3}"
if [[ "$PYTHON" == "python3" && -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
elif [[ "$PYTHON" == "python3" && -x "$HOME/xPST/.venv/bin/python" ]]; then
    PYTHON="$HOME/xPST/.venv/bin/python"
fi

VERSION_A="${XPST_E2E_VERSION_A:-0.1.0}"
VERSION_B="${XPST_E2E_VERSION_B:-0.2.0}"
WORK="${XPST_E2E_WORK:-/private/tmp/xpst-updater-e2e}"
SERVE_DIR="$WORK/serve"
MARKERS="$WORK"
E2E_KEY="${XPST_E2E_SIGNING_KEY:-$ROOT/.tauri/xpst-updater-e2e.key}"
MANIFEST_URL="${XPST_UPDATER_MANIFEST_URL:-http://127.0.0.1:9555/updates/latest.json}"

usage() {
    printf '%s\n' \
        "Usage: $0 [--manifest-url http://127.0.0.1:PORT/updates/latest.json]" \
        "Environment: XPST_E2E_SIGNING_KEY, XPST_E2E_VERSION_A, XPST_E2E_VERSION_B, XPST_E2E_WORK"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --manifest-url)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            MANIFEST_URL="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

export PATH="$HOME/.cargo/bin:$PATH"
command -v cargo >/dev/null || { printf 'cargo not found\n' >&2; exit 2; }
command -v curl >/dev/null || { printf 'curl not found\n' >&2; exit 2; }
[[ -f "$E2E_KEY" ]] || {
    printf 'E2E signing key missing: %s\nGenerate it with: cargo tauri signer generate -w %s --password "" --ci\n' \
        "$E2E_KEY" "$E2E_KEY" >&2
    exit 2
}
[[ -f "$E2E_KEY.pub" ]] || {
    printf 'E2E public key missing: %s.pub\n' "$E2E_KEY" >&2
    exit 2
}
export TAURI_SIGNING_PRIVATE_KEY="$(<"$E2E_KEY")"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""

# The script owns a loopback server.  Requiring the exact local URL prevents a
# typo from silently testing a remote endpoint or publishing anywhere.
URL_DATA="$("$PYTHON" - "$MANIFEST_URL" <<'PY'
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"}:
    raise SystemExit("manifest URL must be local HTTP on 127.0.0.1 or localhost")
if url.path != "/updates/latest.json":
    raise SystemExit("manifest URL must end with /updates/latest.json")
if url.query or url.fragment:
    raise SystemExit("manifest URL must not contain a query or fragment")
print(url.hostname)
print(url.port or 80)
print(f"{url.scheme}://{url.netloc}")
PY
)" || {
    printf 'invalid local manifest URL: %s\n' "$MANIFEST_URL" >&2
    exit 2
}
SERVER_HOST="${URL_DATA%%$'\n'*}"
URL_REST="${URL_DATA#*$'\n'}"
E2E_PORT="${URL_REST%%$'\n'*}"
E2E_URL="${URL_REST#*$'\n'}"

rm -rf "$WORK"
mkdir -p "$SERVE_DIR/updates"
rm -f "$MARKERS"/started-*.txt "$MARKERS/current.txt"

APP_BUNDLE_DIR="src-tauri/target/release/bundle/macos"
TAR_GZ="$APP_BUNDLE_DIR/xPST.app.tar.gz"
SIG="$TAR_GZ.sig"
HTTPD_PID=""
APP_PID=""
cleanup() {
    if [[ -n "$APP_PID" ]]; then
        kill "$APP_PID" 2>/dev/null || true
    fi
    if [[ -n "$HTTPD_PID" ]]; then
        kill "$HTTPD_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

write_overlay() { # $1 = version
    "$PYTHON" - "$1" "$E2E_URL" "$WORK/e2e-overlay.json" "$E2E_KEY.pub" <<'PY'
import json
import sys

version, base, output, pubkey_path = sys.argv[1:]
overlay = {
    "version": version,
    "bundle": {"createUpdaterArtifacts": True},
    "plugins": {
        "updater": {
            "endpoints": [f"{base}/updates/latest.json"],
            "dangerousInsecureTransportProtocol": True,
            "pubkey": open(pubkey_path, encoding="utf-8").read().strip(),
        }
    },
}
with open(output, "w", encoding="utf-8") as stream:
    json.dump(overlay, stream, indent=2)
    stream.write("\n")
PY
}

build_app() { # $1 = version
    local version="$1"
    printf '== [build] v%s ==\n' "$version"
    write_overlay "$version"
    # The real onedir engine is not needed to exercise updater verification;
    # the app's own boot marker and updater still run with this stub resource.
    mkdir -p src-tauri/binaries/engine
    if [[ ! -f src-tauri/binaries/engine/xpst-engine ]]; then
        : > src-tauri/binaries/engine/xpst-engine
        chmod +x src-tauri/binaries/engine/xpst-engine
    fi
    if ! cargo tauri build --bundles app --config "$WORK/e2e-overlay.json" >"$WORK/build-v$version.log" 2>&1; then
        printf 'BUILD FAILED (v%s)\n' "$version" >&2
        "$PYTHON" - "$WORK/build-v$version.log" <<'PY'
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
for line in lines[-40:]:
    print(line)
PY
        return 1
    fi
    printf 'build complete (v%s)\n' "$version"
}

write_manifest() { # $1 = version, $2 = output path
    local version="$1"
    local output="$2"
    "$PYTHON" scripts/gen-updater-manifest.py \
        --version "$version" \
        --notes "xPST updater E2E release $version" \
        --base-url "$E2E_URL" \
        --output "$output" \
        --platform darwin-aarch64 \
        --artifact "darwin-aarch64=$TAR_GZ"
}

assert_artifacts() { # $1 = exit code for this phase
    local exit_code="$1"
    if [[ ! -f "$TAR_GZ" || ! -f "$SIG" ]]; then
        printf 'missing updater artifact or signature: %s / %s\n' "$TAR_GZ" "$SIG" >&2
        return "$exit_code"
    fi
}

wait_for_http() {
    local attempt
    for attempt in $(seq 1 30); do
        if curl -fsS "$MANIFEST_URL" >/dev/null; then
            return 0
        fi
        [[ "$attempt" -eq 30 ]] && break
        sleep 0.5
done
    printf 'local manifest server did not come up: %s\n' "$MANIFEST_URL" >&2
    return 1
}

# 1. Build version A and retain its complete app bundle.
build_app "$VERSION_A" || exit 2
assert_artifacts 3 || exit $?
cp -R "$APP_BUNDLE_DIR/xPST.app" "$WORK/xPST-$VERSION_A.app"

# 2. Start the local endpoint with a valid version-A manifest.
cp "$TAR_GZ" "$SERVE_DIR/xPST.app.tar.gz"
write_manifest "$VERSION_A" "$SERVE_DIR/updates/latest.json"
printf '== [serve] %s ==\n' "$MANIFEST_URL"
"$PYTHON" -m http.server "$E2E_PORT" --bind "$SERVER_HOST" --directory "$SERVE_DIR" >"$WORK/httpd.log" 2>&1 &
HTTPD_PID=$!
wait_for_http || exit 3
"$PYTHON" - "$MANIFEST_URL" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1]) as response:
    manifest = json.load(response)
print(f"served manifest version: {manifest['version']}")
PY

# 3. Build B, replace the hosted payload, and generate the update manifest.
build_app "$VERSION_B" || exit 4
assert_artifacts 5 || exit $?
cp "$TAR_GZ" "$SERVE_DIR/xPST.app.tar.gz"
write_manifest "$VERSION_B" "$SERVE_DIR/updates/latest.json"
printf 'served update manifest version: '
"$PYTHON" - "$SERVE_DIR/updates/latest.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
PY

# 4. Launch A.  lib.rs writes started-A and starts the updater only for this
# opt-in test environment variable.
APP_BIN="$WORK/xPST-$VERSION_A.app/Contents/MacOS/xPST"
[[ -x "$APP_BIN" ]] || {
    printf 'version-A app binary not found: %s\n' "$APP_BIN" >&2
    exit 10
}
printf '== [run] v%s with XPST_UPDATER_CHECK=1 ==\n' "$VERSION_A"
XPST_UPDATER_CHECK=1 "$APP_BIN" >"$WORK/app-v$VERSION_A.log" 2>&1 &
APP_PID=$!

wait_for_marker() { # $1 = marker path, $2 = timeout seconds
    local path="$1"
    local timeout="$2"
    local waited=0
    while [[ ! -f "$path" ]]; do
        sleep 1
        waited=$((waited + 1))
        if [[ "$waited" -ge "$timeout" ]]; then
            return 1
        fi
    done
}

wait_for_marker "$MARKERS/started-$VERSION_A.txt" 30 || {
    printf 'FAIL: version A never booted\n' >&2
    "$PYTHON" - "$WORK/app-v$VERSION_A.log" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))
PY
    exit 10
}
printf 'proof boot v%s: ' "$VERSION_A"
"$PYTHON" - "$MARKERS/started-$VERSION_A.txt" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).read_text(encoding="utf-8").strip())
PY

# 5. A successful update causes the updater to restart the app.  The B marker
# is written by the relaunched binary, not by this harness.
wait_for_marker "$MARKERS/started-$VERSION_B.txt" 120 || {
    printf 'FAIL: version B never relaunched\n' >&2
    printf '%s\n' '--- app log ---'
    "$PYTHON" - "$WORK/app-v$VERSION_A.log" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))
PY
    printf '%s\n' '--- current marker ---'
    "$PYTHON" - "$MARKERS/current.txt" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
if path.exists():
    print(path.read_text(encoding="utf-8"))
PY
    printf '%s\n' '--- HTTP server log ---'
    "$PYTHON" - "$WORK/httpd.log" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))
PY
    exit 11
}

printf '\n================ UPDATER E2E: PASS ================\n'
printf 'boot v%s: ' "$VERSION_A"
"$PYTHON" - "$MARKERS/started-$VERSION_A.txt" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).read_text(encoding="utf-8").strip())
PY
printf 'boot v%s: ' "$VERSION_B"
"$PYTHON" - "$MARKERS/started-$VERSION_B.txt" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).read_text(encoding="utf-8").strip())
PY
printf 'manifest URL: %s\n' "$MANIFEST_URL"
printf '%s\n' '=================================================='
exit 0
