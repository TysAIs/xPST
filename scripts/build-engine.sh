#!/usr/bin/env bash
# build-engine.sh — build the Python engine sidecar for the Tauri shell.
#
# Produces src-tauri/binaries/engine/ (PyInstaller ONEDIR bundle). It is
# shipped as a Tauri bundle resource (bundle.resources) and the shell
# spawns resource_dir()/binaries/engine/xpst-engine at boot.
#
# Why onedir: Tauri externalBin requires a single file, forcing onefile —
# but onefile self-extracts ~45MB on every launch (~1.3s), blowing the
# boot-to-ready <= 1s gate. See src-tauri/binaries/README.md.
#
# Usage:
#   scripts/build-engine.sh
#
# Requirements:
#   - Python >=3.10 with the xpst dependencies + pyinstaller installed
#     (the project venv works: ~/XPST/.venv)
#   - PYTHONPATH does NOT need to point at src/ — this script handles it,
#     so the sidecar always bundles THIS checkout's code.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/src-tauri/binaries"

PYTHON="${PYTHON:-python3}"

echo "==> Building engine sidecar (onedir)"
cd "$REPO_ROOT"
rm -rf dist/engine build/engine-work "$OUT_DIR/engine"
PYTHONPATH="$REPO_ROOT/src" "$PYTHON" -m PyInstaller build_engine.spec \
    --noconfirm --distpath dist/engine --workpath build/engine-work

cp -R dist/engine/xpst-engine "$OUT_DIR/engine"
echo "==> Wrote $OUT_DIR/engine ($(du -sh "$OUT_DIR/engine" | cut -f1))"

# Sanity check: the onedir engine must honor XPST_DASHBOARD_PORT and
# report healthy quickly.
echo "==> Smoke-checking sidecar"
TEST_PORT="$(jot -r 1 20000 40000 2>/dev/null || shuf -i 20000-40000 -n 1 2>/dev/null || echo 39999)"
XPST_DASHBOARD_PORT="$TEST_PORT" "$OUT_DIR/engine/xpst-engine" >/tmp/xpst-engine-check.log 2>&1 &
CHECK_PID=$!
trap 'kill "$CHECK_PID" 2>/dev/null || true' EXIT
START="$(python3 -c 'import time; print(time.time())')"
for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "http://127.0.0.1:$TEST_PORT/health"; then
        ELAPSED="$(python3 -c "import time; print(f'{time.time()-$START:.2f}')")"
        echo "PASS: sidecar /health OK on port $TEST_PORT (cold start ${ELAPSED}s)"
        exit 0
    fi
    sleep 0.25
done
echo "FAIL: sidecar did not report healthy on port $TEST_PORT"
tail -20 /tmp/xpst-engine-check.log
exit 1
