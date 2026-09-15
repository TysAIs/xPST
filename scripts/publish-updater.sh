#!/usr/bin/env bash
# Stage a validated updater manifest at the path GitHub Pages serves.
#
# GitHub Pages does not accept an HTTP PUT at the updater endpoint, and this
# repository serves Pages from the `main` branch root (build_type "legacy",
# source main:/). The served path is therefore a committed file:
# `<site-dir>/updates/latest.json`, where <site-dir> is the branch root of the
# checkout that will be committed.
#
# The manifest must cover a non-empty subset of the four supported Tauri
# targets: an artifact pair (signature + hash) is only published for the
# platforms that actually produced a signed artifact, and the workflow reports
# the rest as missing rather than inventing them.

set -euo pipefail

MANIFEST=""
SITE_DIR=""

usage() {
    printf '%s\n' "Usage: $0 --manifest PATH --site-dir PATH"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --manifest)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            MANIFEST="$2"
            shift 2
            ;;
        --site-dir)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            SITE_DIR="$2"
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

[[ -n "$MANIFEST" && -n "$SITE_DIR" ]] || { usage >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { printf 'manifest not found: %s\n' "$MANIFEST" >&2; exit 2; }

python3 - "$MANIFEST" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    manifest = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid updater manifest {path}: {exc}") from exc

supported = ("darwin-aarch64", "darwin-x86_64", "windows-x86_64", "linux-x86_64")
if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str):
    raise SystemExit("updater manifest must contain a version")
entries = manifest.get("platforms")
if not isinstance(entries, dict) or not entries:
    raise SystemExit("updater manifest must cover at least one platform")
unknown = sorted(set(entries) - set(supported))
if unknown:
    raise SystemExit(f"updater manifest has unsupported platform(s): {', '.join(unknown)}")
for platform in supported:
    if platform not in entries:
        continue
    entry = entries[platform]
    if not isinstance(entry, dict):
        raise SystemExit(f"updater manifest entry is not an object: {platform}")
    if not isinstance(entry.get("url"), str) or not entry["url"]:
        raise SystemExit(f"updater manifest URL is missing: {platform}")
    if not isinstance(entry.get("signature"), str) or not entry["signature"].strip():
        raise SystemExit(f"updater manifest signature is missing: {platform}")
    if not re.fullmatch(r"[0-9a-f]{128}", str(entry.get("sha512", ""))):
        raise SystemExit(f"updater manifest SHA512 is missing or malformed: {platform}")
print(
    f"validated updater manifest {path} "
    f"({manifest['version']}, platforms: {', '.join(sorted(entries))})"
)
PY

mkdir -p "$SITE_DIR/updates"
cp "$MANIFEST" "$SITE_DIR/updates/latest.json"
printf 'staged %s\n' "$SITE_DIR/updates/latest.json"