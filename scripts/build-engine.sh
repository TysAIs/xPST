#!/usr/bin/env bash
# build-engine.sh — build the Python engine sidecar for the Tauri shell.
#
# Produces src-tauri/binaries/xpst-engine-<rust-target-triple> (a PyInstaller
# onefile executable). Tauri's bundle.externalBin picks it up on the next
# `cargo tauri build`.
#
# Usage:
#   scripts/build-engine.sh            # build for this host
#
# Requirements:
#   - Python >=3.10 with the xpst dependencies + pyinstaller installed
#     (the project venv works: ~/XPST/.venv)
#   - PYTHONPATH does NOT need to point at src/ — this script handles it,
#     so the sidecar always bundles THIS checkout's code.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRIPLE="$(rustc -vV | awk '/^host/ {print $2}')"
OUT="$REPO_ROOT/src-tauri/binaries/xpst-engine-$TRIPLE"

PYTHON="${PYTHON:-python3}"

echo "==> Building engine sidecar ($TRIPLE)"
cd "$REPO_ROOT"
PYTHONPATH="$REPO_ROOT/src" "$PYTHON" -m PyInstaller build_engine.spec \
    --noconfirm --distpath dist/engine --workpath build/engine-work

cp dist/engine/xpst-engine "$OUT"
echo "==> Wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Sanity check: the sidecar must honor XPST_DASHBOARD_PORT.
echo "==> Smoke-checking sidecar"
TEST_PORT="$(jot -r 1 20000 40000 2>/dev/null || shuf -i 20000-40000 -n 1 2>/dev/null || echo 39999)"
XPST_DASHBOARD_PORT="$TEST_PORT" "$OUT" >/tmp/xpst-engine-check.log 2>&1 &
CHECK_PID=$!
trap 'kill "$CHECK_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/health"; then
        echo "PASS: sidecar /health OK on port $TEST_PORT"
        exit 0
    fi
    sleep 0.5
done
echo "FAIL: sidecar did not report healthy on port $TEST_PORT"
tail -20 /tmp/xpst-engine-check.log
exit 1
