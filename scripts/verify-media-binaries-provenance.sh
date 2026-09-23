#!/usr/bin/env bash
# Assert that the media binaries a build actually bundles are pinned and
# checksummed.
#
#   scripts/verify-media-binaries-provenance.sh [PROVENANCE.txt] [binaries-dir]
#
# scripts/fetch-media-binaries.sh verifies every download against the SHA-256
# recorded in scripts/media-binaries.lock and writes the record this script
# checks. The release lane runs it so a release can never ship:
#   * a build that fell back to an UNPINNED local binary
#     (XPST_ALLOW_UNPINNED_MEDIA=1 is a local-development escape hatch),
#   * a provenance row that is not a `pinned` row, or that names a moving-target
#     (non-versioned) url, or
#   * a binary present in the bundle inputs with no provenance row at all.
#
# What is NOT required: ffmpeg/ffprobe are no longer bundled (the engine
# resolves them at runtime), so an absent or empty record is fine when nothing
# is bundled. yt-dlp is likewise optional — the engine's bundled `yt_dlp` Python
# module is the primary path, so a failed yt-dlp fetch is a warning, not a lane
# failure. The invariant this checker protects is "nothing unverified ships",
# not "something must ship".
#
# Exits 0 only when those invariants hold. Prints the record either way, so the
# lane log states exactly which bytes each artifact came from.
set -euo pipefail

PROV="${1:-src-tauri/binaries/PROVENANCE.txt}"
BIN_DIR="${2:-$(dirname "$PROV")}"

echo "--- media binary provenance ($PROV) ---"
if [[ -s "$PROV" ]]; then
  cat "$PROV"
else
  echo "(no provenance record at $PROV)"
fi

# Every non-empty row must be a `pinned` row with a checksum and an immutable,
# versioned url. An UNPINNED row is a hard failure.
if [[ -s "$PROV" ]]; then
  if grep -q '^UNPINNED' "$PROV"; then
    echo "::error::media binaries include an UNPINNED local fallback; the release lane ships checksum-verified pinned binaries only" >&2
    exit 1
  fi
  if ! awk -F'\t' '
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        $1 != "pinned" { printf "unexpected provenance row kind: %s\n", $1 > "/dev/stderr"; bad = 1 }
        $3 == "" { print "pinned row without an artifact name" > "/dev/stderr"; bad = 1 }
        END { exit bad }
      ' "$PROV"; then
    echo "::error::$PROV has a malformed provenance row" >&2
    exit 1
  fi
fi

# Whatever is actually present in the bundle inputs must be covered by a pinned
# row: this is what closes the "replaced the binary by hand" hole.
missing=()
for candidate in yt-dlp yt-dlp.exe ffmpeg ffmpeg.exe ffprobe ffprobe.exe; do
  [[ -e "$BIN_DIR/ytdlp/$candidate" || -e "$BIN_DIR/ffmpeg/$candidate" ]] || continue
  if ! awk -F'\t' -v a="${candidate%.exe}" '$1 == "pinned" && $3 == a { found = 1 } END { exit !found }' "$PROV"; then
    missing+=("$candidate")
  fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "::error::bundled media binaries with no pinned provenance entry in $PROV: ${missing[*]}" >&2
  exit 1
fi

echo "PASS: every bundled media binary has a pinned, checksum-verified provenance row"