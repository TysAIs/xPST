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

# Sanity check: the onedir engine must honor the port BOTH ways the shell
# may pass it — the XPST_DASHBOARD_PORT env var AND an explicit --port argv
# flag. The argv path was silently ignored by an earlier frozen entrypoint
# (it always bound 8080), so both are smoke-tested here.
echo "==> Smoke-checking sidecar (env var + --port argv)"
TEST_PORT="$(jot -r 1 20000 40000 2>/dev/null || shuf -i 20000-40000 -n 1 2>/dev/null || echo 39999)"
ARGV_PORT="$(jot -r 1 20001 40001 2>/dev/null || shuf -i 20001-40001 -n 1 2>/dev/null || echo 39998)"

check_port() {
    local mode="$1" port="$2"; shift 2
    "$@" >/tmp/xpst-engine-check.log 2>&1 &
    local pid=$!
    local start; start="$(python3 -c 'import time; print(time.time())')"
    for _ in $(seq 1 60); do
        if curl -sf -o /dev/null "http://127.0.0.1:$port/health"; then
            local elapsed; elapsed="$(python3 -c "import time; print(f'{time.time()-$start:.2f}')")"
            echo "PASS: sidecar /health OK via $mode on port $port (cold start ${elapsed}s)"
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            return 0
        fi
        sleep 0.25
    done
    echo "FAIL: sidecar did not report healthy via $mode on port $port"
    tail -20 /tmp/xpst-engine-check.log
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    return 1
}

check_port "XPST_DASHBOARD_PORT env" "$TEST_PORT" \
    env "XPST_DASHBOARD_PORT=$TEST_PORT" "$OUT_DIR/engine/xpst-engine"
check_port "--port argv flag" "$ARGV_PORT" \
    "$OUT_DIR/engine/xpst-engine" --port "$ARGV_PORT"
echo "PASS: sidecar honors both the env var and --port"
