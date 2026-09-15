#!/usr/bin/env bash
# Assert that the bundled media binaries are fully pinned and checksummed.
#
#   scripts/verify-media-binaries-provenance.sh [path-to-PROVENANCE.txt]
#
# scripts/fetch-media-binaries.sh verifies every download against the SHA-256
# recorded in scripts/media-binaries.lock and writes the record this script
# checks. The release lane runs it so a release can never ship:
#   * a build whose media binaries were never verified (no record at all),
#   * a build that fell back to an UNPINNED local binary
#     (XPST_ALLOW_UNPINNED_MEDIA=1 is a local-development escape hatch), or
#   * a build missing ffmpeg, ffprobe or yt-dlp.
#
# Exits 0 only when every required artifact has a `pinned` provenance row and no
# UNPINNED row is present. Prints the record either way, so the lane log states
# exactly which bytes each artifact came from.
set -euo pipefail

PROV="${1:-src-tauri/binaries/PROVENANCE.txt}"
REQUIRED_ARTIFACTS=(ffmpeg ffprobe yt-dlp)

if [[ ! -s "$PROV" ]]; then
  echo "::error::no media-binary provenance record at $PROV" >&2
  echo "scripts/fetch-media-binaries.sh must run before this check" >&2
  exit 1
fi

echo "--- media binary provenance ($PROV) ---"
cat "$PROV"

if grep -q '^UNPINNED' "$PROV"; then
  echo "::error::media binaries include an UNPINNED local fallback; the release lane ships checksum-verified pinned binaries only" >&2
  exit 1
fi

for artifact in "${REQUIRED_ARTIFACTS[@]}"; do
  # provenance rows are: pinned <platform> <artifact> <key=value ...>
  if ! awk -F'\t' -v a="$artifact" -v p="$PROV" '
        $1 == "pinned" && $3 == a { found = 1 }
        END { if (!found) printf "missing pinned provenance row for %s (%s)\n", a, p > "/dev/stderr"; exit !found }
      ' "$PROV"; then
    echo "::error::$artifact has no pinned provenance entry in $PROV" >&2
    exit 1
  fi
done

echo "PASS: every bundled media binary has a pinned, checksum-verified provenance row"
