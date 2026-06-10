#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
PUBLIC_RELEASE=0

if [[ "${1:-}" == "--public" ]]; then
  PUBLIC_RELEASE=1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -e ".[full,dev]" build pip-audit pyinstaller

"$VENV_DIR/bin/python" -m pytest
"$VENV_DIR/bin/ruff" check src tests
"$VENV_DIR/bin/mypy" src/xpst
"$VENV_DIR/bin/pip-audit"
"$VENV_DIR/bin/python" -m build

"$VENV_DIR/bin/xpst" version --json
"$VENV_DIR/bin/xpst" health --json

QT_QPA_PLATFORM=offscreen "$VENV_DIR/bin/python" scripts/verify_qml_pages.py

"$VENV_DIR/bin/pyinstaller" --clean --noconfirm build_macos.spec

if [[ "$PUBLIC_RELEASE" == "1" ]]; then
  : "${MACOS_CODESIGN_IDENTITY:?MACOS_CODESIGN_IDENTITY is required for public macOS releases}"
  : "${APPLE_ID:?APPLE_ID is required for public macOS notarization}"
  : "${APPLE_TEAM_ID:?APPLE_TEAM_ID is required for public macOS notarization}"
  : "${APPLE_APP_PASSWORD:?APPLE_APP_PASSWORD is required for public macOS notarization}"
fi

bash scripts/sign_macos.sh dist/xPST.app

VERIFY_ARGS=(--app dist/xPST.app --dmg dist/xPST.dmg --json)
if [[ "$PUBLIC_RELEASE" == "1" ]]; then
  VERIFY_ARGS+=(--require-developer-id --require-notarized)
fi
"$VENV_DIR/bin/python" scripts/verify_macos_artifact.py "${VERIFY_ARGS[@]}"
"$VENV_DIR/bin/python" scripts/release_artifacts.py --dist dist --output-dir release --skip-checks

echo "macOS validation complete"
