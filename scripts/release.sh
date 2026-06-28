#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?Usage: scripts/release.sh <version>}"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is dirty. Commit or stash changes before releasing." >&2
  exit 1
fi

python - <<PY
import re
from pathlib import Path

version = "$VERSION"
pyproject = Path("pyproject.toml")
init_file = Path("src/xpst/__init__.py")

pyproject.write_text(
    re.sub(r'^version = "[^"]+"', f'version = "{version}"', pyproject.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE),
    encoding="utf-8",
)
init_file.write_text(
    re.sub(r'^__version__ = "[^"]+"', f'__version__ = "{version}"', init_file.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE),
    encoding="utf-8",
)
PY

python -m pytest
ruff check src tests
mypy src/xpst
pip-audit
python scripts/build_package.py
python scripts/release_artifacts.py --dist dist --output-dir release --skip-checks

git add pyproject.toml src/xpst/__init__.py
git commit -m "release: v${VERSION}"
git tag -s "v${VERSION}" -m "xPST v${VERSION}" || git tag "v${VERSION}" -m "xPST v${VERSION}"

echo "Release commit and tag created: v${VERSION}"
echo "Push with: git push origin HEAD && git push origin v${VERSION}"
echo "GitHub Actions will publish via Trusted Publishing when the tag is pushed."
