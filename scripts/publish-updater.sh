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
ASSETS_DIR=""

usage() {
    printf '%s\n' "Usage: $0 --manifest PATH --site-dir PATH [--assets-dir PATH]"
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
        --assets-dir)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            ASSETS_DIR="$2"
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
if [[ -n "$ASSETS_DIR" && ! -d "$ASSETS_DIR" ]]; then
    printf 'assets directory not found: %s\n' "$ASSETS_DIR" >&2
    exit 2
fi

python3 - "$MANIFEST" "$ASSETS_DIR" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

path = Path(sys.argv[1])
assets_dir = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
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

digests: dict[str, tuple[str, str]] = {}
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
    previous = digests.get(str(entry["sha512"]))
    if previous is not None and previous[1] != entry["url"]:
        raise SystemExit(
            f"{platform} and {previous[0]} describe different files with one sha512; "
            "a manifest may repeat a digest only when it repeats the payload URL"
        )
    digests[str(entry["sha512"])] = (platform, entry["url"])

if assets_dir is not None:
    for platform, entry in entries.items():
        name = unquote(Path(urlparse(entry["url"]).path).name)
        artifact = assets_dir / name
        if not artifact.is_file():
            raise SystemExit(
                f"{platform} manifest points at {name}, which is not among the release assets"
            )
        digest = hashlib.sha512(artifact.read_bytes()).hexdigest()
        if digest != entry["sha512"]:
            raise SystemExit(
                f"{platform} manifest sha512 does not match the published bytes of {name} "
                f"(manifest {str(entry['sha512'])[:12]}…, file {digest[:12]}…)"
            )
        signature_path = assets_dir / (name + ".sig")
        if not signature_path.is_file():
            raise SystemExit(f"{platform} updater signature sidecar is missing for {name}")
        signature = signature_path.read_text(encoding="utf-8").strip()
        if signature != entry["signature"].strip():
            raise SystemExit(f"{platform} manifest signature does not match {name}.sig")

print(
    f"validated updater manifest {path} "
    f"({manifest['version']}, platforms: {', '.join(sorted(entries))}"
    f"{', bytes verified against ' + str(assets_dir) if assets_dir is not None else ''})"
)
PY

mkdir -p "$SITE_DIR/updates"
cp "$MANIFEST" "$SITE_DIR/updates/latest.json"
printf 'staged %s\n' "$SITE_DIR/updates/latest.json"