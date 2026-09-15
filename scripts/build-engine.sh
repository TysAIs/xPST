#!/usr/bin/env bash
# build-engine.sh — build the Python engine sidecar for the Tauri shell.
#
# Produces src-tauri/binaries/engine/ (PyInstaller ONEDIR bundle). It is
# shipped as a Tauri bundle resource (bundle.resources) and the shell spawns
# the platform-native executable from resource_dir()/binaries/engine/.
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
#   - Bash (including Git Bash on Windows)
#   - PyInstaller builds for the host OS; cross-compilation is not supported
#   - PYTHONPATH does NOT need to point at src/ — this script handles it,
#     so the sidecar always bundles THIS checkout's code.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/src-tauri/binaries"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    elif command -v python >/dev/null 2>&1; then
        PYTHON=python
    else
        echo "ERROR: Python >=3.10 was not found (set PYTHON to its executable)." >&2
        exit 1
    fi
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "ERROR: $PYTHON is not Python >=3.10." >&2
    exit 1
fi

# PyInstaller appends .exe to EXE(name="xpst-engine") on Windows. The
# directory name remains xpst-engine on every platform.
case "$(uname -s 2>/dev/null || printf 'unknown')" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT) ENGINE_NAME=xpst-engine.exe ;;
    *) ENGINE_NAME=xpst-engine ;;
esac
ENGINE_DIST_DIR="$REPO_ROOT/dist/engine/xpst-engine"
ENGINE_EXECUTABLE="$OUT_DIR/engine/$ENGINE_NAME"
CHECK_LOG="${TMPDIR:-/tmp}/xpst-engine-check-$$.log"
CHECK_CONFIG_DIR="${TMPDIR:-/tmp}/xpst-engine-check-config-$$"

CHECK_PID=""
cleanup() {
    if [[ -n "$CHECK_PID" ]]; then
        kill "$CHECK_PID" 2>/dev/null || true
        if [[ "$ENGINE_NAME" == *.exe ]] && command -v taskkill >/dev/null 2>&1; then
            taskkill //PID "$CHECK_PID" //T //F >/dev/null 2>&1 || true
        fi
        CHECK_PID=""
    fi
    rm -rf "$CHECK_CONFIG_DIR" "$CHECK_LOG"
}
trap cleanup EXIT

echo "==> Building engine sidecar (onedir; executable=$ENGINE_NAME)"
cd "$REPO_ROOT"
rm -rf dist/engine build/engine-work
mkdir -p "$OUT_DIR/engine"
# Keep the tracked placeholder in the resource root. It lets a clean checkout
# satisfy Tauri's resource-path validation before this build replaces artifacts.
shopt -s dotglob nullglob
for existing in "$OUT_DIR/engine"/*; do
    [[ "$existing" == "$OUT_DIR/engine/.gitkeep" ]] || rm -rf "$existing"
done
shopt -u dotglob nullglob
PYTHONPATH="$REPO_ROOT/src" "$PYTHON" -m PyInstaller build_engine.spec \
    --noconfirm --distpath dist/engine --workpath build/engine-work

if [[ ! -f "$ENGINE_DIST_DIR/$ENGINE_NAME" ]]; then
    echo "ERROR: PyInstaller did not produce $ENGINE_DIST_DIR/$ENGINE_NAME" >&2
    exit 1
fi
cp -R "$ENGINE_DIST_DIR"/. "$OUT_DIR/engine/"
SIDECAR_SIZE="$("$PYTHON" -c 'from pathlib import Path; import sys; root=Path(sys.argv[1]); total=sum(p.stat().st_size for p in root.rglob("*") if p.is_file()); print(f"{total / 1024 / 1024:.1f} MiB")' "$OUT_DIR/engine")"
echo "==> Wrote $OUT_DIR/engine ($SIDECAR_SIZE)"

# Sanity check: the onedir engine must honor the port BOTH ways the shell may
# pass it — the XPST_DASHBOARD_PORT env var AND an explicit --port argv flag.
# The argv path was silently ignored by an earlier frozen entrypoint (it always
# bound 8080), so both are exercised here.
#
# The probe, the port selection and the clock are pure Python: Git Bash on
# Windows has no `jot`, `shuf`, or guaranteed GNU `curl`, so the old
# shell-tooling version could not run there.
mkdir -p "$CHECK_CONFIG_DIR"

pick_port() {
    "$PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

# probe_health <label> <port> <command...>
probe_health() {
    local label="$1" port="$2"; shift 2
    "$@" >"$CHECK_LOG" 2>&1 &
    CHECK_PID=$!
    local start elapsed
    start="$("$PYTHON" -c 'import time; print(time.monotonic())')"
    for ((attempt=1; attempt<=60; attempt++)); do
        if "$PYTHON" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1)' \
            "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            elapsed="$("$PYTHON" -c 'import sys, time; print(f"{time.monotonic() - float(sys.argv[1]):.2f}")' "$start")"
            echo "PASS: sidecar /health OK via $label on port $port (cold start ${elapsed}s)"
            kill "$CHECK_PID" 2>/dev/null || true
            wait "$CHECK_PID" 2>/dev/null || true
            CHECK_PID=""
            return 0
        fi
        sleep 0.25
    done
    echo "FAIL: sidecar did not report healthy via $label on port $port" >&2
    "$PYTHON" -c 'from pathlib import Path; import sys; path=Path(sys.argv[1]); print("\n".join(path.read_text(errors="replace").splitlines()[-20:]))' "$CHECK_LOG" >&2 || true
    return 1
}

ENV_PORT="$(pick_port)"
probe_health "XPST_DASHBOARD_PORT env" "$ENV_PORT" \
    env "XPST_CONFIG_DIR=$CHECK_CONFIG_DIR" "XPST_DASHBOARD_PORT=$ENV_PORT" "$ENGINE_EXECUTABLE"

ARGV_PORT="$(pick_port)"
probe_health "--port argv flag" "$ARGV_PORT" \
    env "XPST_CONFIG_DIR=$CHECK_CONFIG_DIR" "$ENGINE_EXECUTABLE" --port "$ARGV_PORT"

echo "PASS: sidecar honors both the env var and --port"
