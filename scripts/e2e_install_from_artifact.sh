#!/usr/bin/env bash
# Run xPST's clean-profile stranger-install E2E against one desktop artifact.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${XPST_E2E_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
  else
    printf 'FAIL: Python 3 is required to run %s\n' "${BASH_SOURCE[0]}" >&2
    exit 2
  fi
fi

exec "$PYTHON" "$ROOT/scripts/e2e_install_from_artifact.py" "$@"
