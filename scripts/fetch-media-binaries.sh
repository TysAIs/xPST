#!/usr/bin/env bash
# Fetch the media binaries that the desktop bundle still ships: yt-dlp only.
#
# ffmpeg / ffprobe used to be fetched here too and bundled (~87 MB of a 192 MB
# app, and the flaky mirror behind that download is what kept the Tauri release
# lane red). They are no longer bundled: the engine resolves
#   XPST_FFMPEG_PATH > system ffmpeg > a verified copy fetched on first use
# (see src/xpst/media/binaries.py, surfaced as `xpst media fetch`). A machine
# that already has ffmpeg downloads nothing.
#
# yt-dlp stays bundled because it is a ~3 MB zipapp with no system equivalent;
# the engine's bundled `yt_dlp` Python module remains the primary path, so a
# failure here is a warning, not a lane failure.
#
# Resilience contract (see tests/test_fetch_media_binaries_script.py):
#   * downloads are retried with exponential backoff and resume partial
#     transfers where the server supports byte ranges;
#   * a partial/corrupt transfer is rejected (curl --fail plus zipapp payload
#     checks) instead of being installed;
#   * if every source fails the script says so explicitly, naming what it tried.
#
# Usage: scripts/fetch-media-binaries.sh [macos-arm64|macos-x64|win-x64|linux-x64|linux-arm64]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
YTDLP_DIR="$ROOT/src-tauri/binaries/ytdlp"
mkdir -p "$YTDLP_DIR"

# Tuning knobs (overridable so tests/slow networks can adjust).
: "${FETCH_RETRIES:=3}"        # curl-level attempts per URL (--retry)
: "${FETCH_RETRY_DELAY:=2}"    # curl backoff + our inter-round delay, seconds
: "${FETCH_ROUNDS:=2}"         # script-level rounds (round 2 resumes partials)
: "${FETCH_CONNECT_TIMEOUT:=20}"
: "${FETCH_MAX_TIME:=900}"

log()  { printf '%s\n' "$*" >&2; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

TRIED_SOURCES=()
note_tried() { TRIED_SOURCES+=("$1"); }

# download <url> <dest>
# Two layers of resilience:
#   1. curl retries each transfer (--retry --retry-all-errors --retry-delay
#      --retry-connrefused) so a flaky mirror / truncation (curl exit 18) is
#      retried before we give up on the URL;
#   2. our round loop then re-attempts the URL with `--continue-at -` to resume
#      the partial file, discarding it if the server has no byte-range support.
# --fail makes an HTTP error status a hard failure; the caller additionally
# verifies the payload, so a partial transfer can never be installed as a
# working binary.
download() {
  local url="$1" dest="$2"
  local round=1 rc=0 note=""
  local opts=()
  while :; do
    opts=()
    note=""
    if [[ $round -gt 1 && -s "$dest" ]]; then
      opts=(--continue-at -)
      note=", resuming partial download"
    fi
    log "fetching $url (round $round/$FETCH_ROUNDS$note)"
    if curl --fail --location --show-error --silent --globoff \
        --retry "$FETCH_RETRIES" --retry-all-errors \
        --retry-delay "$FETCH_RETRY_DELAY" --retry-connrefused \
        --connect-timeout "$FETCH_CONNECT_TIMEOUT" --max-time "$FETCH_MAX_TIME" \
        "${opts[@]+"${opts[@]}"}" \
        -o "$dest" "$url"; then
      return 0
    else
      rc=$?   # curl's real exit status (e.g. 18 = partial transfer)
    fi
    warn "download failed (curl exit $rc) on round $round/$FETCH_ROUNDS: $url"
    if [[ $round -ge $FETCH_ROUNDS ]]; then
      return "$rc"
    fi
    if [[ ${#opts[@]} -gt 0 ]]; then
      # Resume made it fail (no byte-range support) — restart from scratch.
      warn "discarding partial file and retrying without resume"
      rm -f "$dest"
    fi
    sleep $(( FETCH_RETRY_DELAY * round ))
    round=$(( round + 1 ))
  done
}

# install_ytdlp_zipapp <src> <dest>: yt-dlp is a Python zipapp (or a PE
# executable on Windows), so a Mach-O/ELF magic check does not apply — but a
# truncated download must still be rejected instead of installed.
install_ytdlp_zipapp() {
  local src="$1" dest="$2" size
  if [[ ! -s "$src" ]]; then
    warn "yt-dlp: download is empty — rejecting partial transfer"
    return 1
  fi
  size="$(wc -c < "$src" | tr -d ' ')"
  if [[ "$size" -lt 1000000 ]]; then
    warn "yt-dlp: only ${size} bytes — rejecting partial transfer"
    return 1
  fi
  if ! head -c 2 "$src" | grep -q '#!'; then
    if command -v file >/dev/null 2>&1 && ! file -b "$src" | grep -qi 'PE32'; then
      warn "yt-dlp: not a zipapp or Windows executable — rejecting download"
      return 1
    fi
  fi
  mv -f "$src" "$dest" && chmod +x "$dest"
}

# fetch_ytdlp <dest> <url...>
fetch_ytdlp() {
  local dest="$1"; shift
  local url tmp
  for url in "$@"; do
    tmp="$dest.part"
    rm -f "$tmp"
    note_tried "$url"
    if download "$url" "$tmp" && install_ytdlp_zipapp "$tmp" "$dest"; then
      log "ok: yt-dlp via $url"
      return 0
    fi
    warn "source exhausted for yt-dlp: $url"
    rm -f "$tmp"
  done
  return 1
}

platform="${1:-}"
if [[ -z "$platform" ]]; then
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) platform=macos-arm64 ;;
    Darwin/x86_64) platform=macos-x64 ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT) platform=win-x64 ;;
    Linux/x86_64) platform=linux-x64 ;;
    Linux/aarch64) platform=linux-arm64 ;;
    *) echo "unsupported platform: $(uname -s)/$(uname -m)" >&2; exit 1 ;;
  esac
fi

case "$platform" in
  macos-arm64|macos-x64)
    YTDLP_NAME=yt-dlp
    # The zipapp ships with `#!/usr/bin/env python3`, but macOS system python3
    # is 3.9 (unsupported by yt-dlp), so re-shebang to a >=3.10 interpreter
    # when one is available. The engine's bundled yt_dlp module remains the
    # primary path; this zipapp is the CLI fallback surfaced via
    # XPST_YTDLP_PATH.
    fix_ytdlp_shebang() {
      local f="$1" interpreter="$2"
      local tmp="$f.tmpshebang"
      { printf '#!%s\n' "$interpreter"; tail -n +2 "$f"; } > "$tmp" \
        && mv "$tmp" "$f" && chmod +x "$f"
    }
    ;;
  win-x64) YTDLP_NAME=yt-dlp.exe ;;
  linux-x64|linux-arm64) YTDLP_NAME=yt-dlp ;;
esac

if [[ ! -x "$YTDLP_DIR/$YTDLP_NAME" ]]; then
  fetch_ytdlp "$YTDLP_DIR/$YTDLP_NAME" \
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/$YTDLP_NAME" || {
      warn "could not fetch the yt-dlp zipapp; the engine's bundled yt_dlp module still works"
    }
fi

if [[ "$platform" == macos-* && -x "$YTDLP_DIR/$YTDLP_NAME" ]] && head -c 2 "$YTDLP_DIR/$YTDLP_NAME" | grep -q '#!'; then
  if command -v python3.10 >/dev/null; then
    fix_ytdlp_shebang "$YTDLP_DIR/$YTDLP_NAME" /usr/bin/env\ python3.10
  elif [[ "$(python3 -c 'import sys; print(sys.version_info[:2] >= (3,10))' 2>/dev/null)" == "True" ]]; then
    : # system python3 already >= 3.10
  elif [[ -x /opt/homebrew/bin/python3 ]]; then
    fix_ytdlp_shebang "$YTDLP_DIR/$YTDLP_NAME" /opt/homebrew/bin/python3
  fi
  "$YTDLP_DIR/$YTDLP_NAME" --version >/dev/null 2>&1 || true
fi

if [[ ! -e "$YTDLP_DIR/$YTDLP_NAME" ]]; then
  {
    echo
    echo "=============================================================="
    echo "YT-DLP FETCH FAILED ($platform)"
    echo "Every source below was tried with retries, backoff and resume:"
    for src in "${TRIED_SOURCES[@]+"${TRIED_SOURCES[@]}"}"; do
      echo "  - $src"
    done
    echo "The app still works: the engine bundles the yt_dlp Python module."
    echo "Retry with: scripts/fetch-media-binaries.sh $platform"
    echo "=============================================================="
  } >&2
fi

echo "media binaries ready ($platform):"
ls -lh "$YTDLP_DIR"

echo "note: ffmpeg/ffprobe are intentionally NOT bundled — the engine uses a system"
echo "      ffmpeg, or fetches a checksum-verified static build on first use"
echo "      (python -m xpst media fetch / the desktop app does this automatically)."