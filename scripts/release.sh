#!/bin/bash
set -euo pipefail

VERSION="${1:?Usage: release.sh <version>}"

# Bump version
sed -i '' "s/^version = .*/version = \"$VERSION\"/" pyproject.toml
sed -i '' "s/__version__ = .*/__version__ = \"$VERSION\"/" src/xpst/__init__.py

# Build
.venv/bin/python -m build --wheel --sdist

# Tag and release
git add -A && git commit -m "release: v$VERSION"
git tag "v$VERSION"
git push origin main --tags
gh release create "v$VERSION" dist/*.whl dist/*.tar.gz --title "xPST v$VERSION" --notes "See CHANGELOG.md"
echo "✅ Released v$VERSION"
