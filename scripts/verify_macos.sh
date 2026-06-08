#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"

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

QT_QPA_PLATFORM=offscreen "$VENV_DIR/bin/python" - <<'PY'
from pathlib import Path
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtQml import QQmlApplicationEngine

app = QApplication([])
engine = QQmlApplicationEngine()
qml = Path("src/xpst/desktop_app/qml/main.qml").resolve()
engine.load(QUrl.fromLocalFile(str(qml)))
roots = engine.rootObjects()
print(f"root_objects {len(roots)}")
raise SystemExit(0 if roots else 1)
PY

"$VENV_DIR/bin/pyinstaller" --clean --noconfirm build_macos.spec
"$VENV_DIR/bin/python" scripts/release_artifacts.py --dist dist

if [[ -d "dist/xPST.app" ]]; then
  codesign --verify --deep --strict "dist/xPST.app" || true
  spctl --assess --type execute "dist/xPST.app" || true
fi

echo "macOS validation complete"

