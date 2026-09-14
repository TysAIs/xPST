#!/usr/bin/env bash
# Fetch static media binaries (ffmpeg/ffprobe/yt-dlp) into src-tauri/binaries/
# so `cargo tauri build` can bundle them. These are NOT committed to git (see
# .gitignore) — run this before building, locally and in CI.
#
# Resilience contract (see tests/test_fetch_media_binaries_script.py):
#   * every download is retried with exponential backoff, resuming partial
#     transfers where the server supports byte ranges;
#   * partial/corrupt transfers are detected and rejected (curl --fail, plus
#     archive integrity + executable-magic checks) instead of being unpacked;
#   * each artifact has several candidate sources and the first healthy one
#     wins, so a single flaky mirror cannot fail the release lane;
#   * a usable binary already on PATH is used as a last resort;
#   * if every source fails the script exits non-zero with an explicit
#     "MEDIA BINARY FETCH FAILED" report listing what was tried, instead of a
#     bare `curl: (18)`.
#
# Usage: scripts/fetch-media-binaries.sh [macos-arm64|macos-x64|win-x64|linux-x64|linux-arm64]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FF_DIR="$ROOT/src-tauri/binaries/ffmpeg"
YTDLP_DIR="$ROOT/src-tauri/binaries/ytdlp"
mkdir -p "$FF_DIR" "$YTDLP_DIR"

# Tuning knobs (overridable so tests/slow networks can adjust).
: "${FETCH_RETRIES:=3}"        # curl-level attempts per URL (--retry)
: "${FETCH_RETRY_DELAY:=2}"    # curl backoff + our inter-round delay, seconds
: "${FETCH_ROUNDS:=2}"         # script-level rounds (round 2 resumes partials)
: "${FETCH_CONNECT_TIMEOUT:=20}"
: "${FETCH_MAX_TIME:=900}"

log()  { printf '%s\n' "$*" >&2; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

# Sources tried per artifact, in order. Recorded so the failure report can
# name them and so tests can assert the fallback chain exists.
TRIED_SOURCES=()
note_tried() { TRIED_SOURCES+=("$1"); }

have() { [[ -x "$1" ]]; }

# download <url> <dest>
# Two layers of resilience:
#   1. curl retries each transfer (--retry --retry-all-errors --retry-delay
#      --retry-connrefused) so a flaky mirror / truncation (curl exit 18) is
#      retried before we give up on the URL;
#   2. our round loop then re-attempts the URL with `--continue-at -` to resume
#      the partial file, discarding it if the server has no byte-range support.
# --fail makes an HTTP error status a hard failure; callers additionally verify
# the payload (zip/unzip -t, tar -tJf, executable magic) so a partial transfer
# can never be installed as a working binary.
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

# verify_binary <path> <label>: non-empty and looks like a Mach-O/ELF executable.
verify_binary() {
  local path="$1" label="$2"
  if [[ ! -s "$path" ]]; then
    warn "$label: downloaded file is empty or missing"
    return 1
  fi
  if command -v file >/dev/null 2>&1; then
    local kind
    kind="$(file -b "$path" 2>/dev/null || true)"
    case "$kind" in
      *Mach-O*|*ELF*|*"PE32"*) : ;;
      *) warn "$label: rejected partial/foreign transfer ($kind)"; return 1 ;;
    esac
  fi
  chmod +x "$path"
  return 0
}

# install_zip <wanted> <destdir> <archive>: unpack, integrity-check, install
install_zip() {
  local want="$1" dstdir="$2" archive="$3"
  if ! unzip -tq "$archive" >/dev/null 2>&1; then
    warn "$want: rejected truncated/corrupt zip $(basename "$archive")"
    return 1
  fi
  local tmp="$dstdir/.unpack.$$"
  rm -rf "$tmp"; mkdir -p "$tmp"
  if ! unzip -oq "$archive" -d "$tmp" >/dev/null 2>&1; then
    warn "$want: unzip failed for $(basename "$archive")"
    rm -rf "$tmp"; return 1
  fi
  local found
  found="$(find "$tmp" -type f -name "$want" -not -path '*__MACOSX*' | head -n 1)"
  if [[ -z "$found" ]]; then
    warn "$want: not present inside $(basename "$archive")"
    rm -rf "$tmp"; return 1
  fi
  if ! verify_binary "$found" "$want"; then
    rm -rf "$tmp"; return 1
  fi
  mv -f "$found" "$dstdir/$want"
  rm -rf "$tmp"
  return 0
}

# install_raw <wanted> <destdir> <file>: install a bare binary
install_raw() {
  local want="$1" dstdir="$2" src="$3"
  verify_binary "$src" "$want" || return 1
  mv -f "$src" "$dstdir/$want"
  return 0
}

# install_tarxz <wanted> <destdir> <archive>
install_tarxz() {
  local want="$1" dstdir="$2" archive="$3"
  if ! tar -tJf "$archive" >/dev/null 2>&1; then
    warn "$want: rejected truncated/corrupt tar.xz $(basename "$archive")"
    return 1
  fi
  local tmp="$dstdir/.unpack.$$"
  rm -rf "$tmp"; mkdir -p "$tmp"
  if ! tar -xJf "$archive" -C "$tmp" >/dev/null 2>&1; then
    warn "$want: tar extraction failed for $(basename "$archive")"
    rm -rf "$tmp"; return 1
  fi
  local found
  found="$(find "$tmp" -type f -name "$want" | head -n 1)"
  if [[ -z "$found" ]] || ! verify_binary "$found" "$want"; then
    rm -rf "$tmp"; return 1
  fi
  mv -f "$found" "$dstdir/$want"
  rm -rf "$tmp"
  return 0
}

# fetch_from_url <wanted> <destdir> <url>
fetch_from_url() {
  local want="$1" dstdir="$2" url="$3"
  local tmp="$dstdir/.dl.$$.part"
  rm -f "$tmp"
  note_tried "$url"
  case "$url" in
    *.zip|*/zip)   download "$url" "$tmp" && install_zip   "$want" "$dstdir" "$tmp" ;;
    *.tar.xz|*.txz) download "$url" "$tmp" && install_tarxz "$want" "$dstdir" "$tmp" ;;
    *)             download "$url" "$tmp" && install_raw   "$want" "$dstdir" "$tmp" ;;
  esac
  local rc=$?
  rm -f "$tmp"
  return $rc
}

# fetch_binary <wanted> <destdir> <url...>: first healthy source wins.
fetch_binary() {
  local want="$1" dstdir="$2"; shift 2
  local url
  for url in "$@"; do
    if fetch_from_url "$want" "$dstdir" "$url"; then
      log "ok: $want via $url"
      return 0
    fi
    warn "source exhausted for $want: $url"
  done
  return 1
}

# fallback_from_path <wanted> <destdir>: last resort — a binary already on PATH.
fallback_from_path() {
  local want="$1" dstdir="$2" found
  found="$(command -v "$want" 2>/dev/null || true)"
  [[ -n "$found" ]] || return 1
  [[ "$found" == "$dstdir/$want" ]] && return 0
  warn "$want: all remote sources failed; falling back to $found"
  cp "$found" "$dstdir/$want" && chmod +x "$dstdir/$want"
}

# require_binary <wanted> <destdir> <url...>
# Already present -> ok. Otherwise try every source, then a PATH fallback.
# Stays non-fatal so one artifact's failure still gets reported together with
# the others in a single, explicit report at the end of the script.
FAILED=()
require_binary() {
  local want="$1" dstdir="$2"; shift 2
  if have "$dstdir/$want"; then
    log "already present: $dstdir/$want"
    return 0
  fi
  if fetch_binary "$want" "$dstdir" "$@"; then
    return 0
  fi
  if fallback_from_path "$want" "$dstdir"; then
    return 0
  fi
  FAILED+=("$want")
  return 1
}

fetch_ytdlp() { # <dest> <url...>
  local dest="$1"; shift
  local url tmp
  for url in "$@"; do
    tmp="$dest.part"
    rm -f "$tmp"
    # not recorded in TRIED_SOURCES: yt-dlp is best-effort (the engine also
    # bundles its own yt_dlp module), so it must not appear in the hard-failure
    # report for ffmpeg/ffprobe.
    if download "$url" "$tmp" && install_ytdlp_zipapp "$tmp" "$dest"; then
      log "ok: yt-dlp via $url"
      return 0
    fi
    warn "source exhausted for yt-dlp: $url"
    rm -f "$tmp"
  done
  return 1
}

# install_ytdlp_zipapp <src> <dest>: yt-dlp is a Python zipapp (or a PE
# executable on Windows), so the Mach-O/ELF magic check does not apply — but a
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

# Pinned third-party release used as an alternative to osxexperts.net.
# eugeneware/ffmpeg-static publishes darwin-arm64 / darwin-x64 binaries.
FFSTATIC_TAG="b6.1.1"
FFSTATIC_BASE="https://github.com/eugeneware/ffmpeg-static/releases/download/$FFSTATIC_TAG"
OSXEXP="https://www.osxexperts.net"

case "$platform" in
  macos-arm64|macos-x64)
    if [[ "$platform" == macos-arm64 ]]; then
      FF_URLS=(
        "$OSXEXP/ffmpeg6arm.zip"
        "$FFSTATIC_BASE/ffmpeg-darwin-arm64"
        "https://evermeet.cx/ffmpeg/getrelease/zip"   # x86_64, runs via Rosetta
        "$FFSTATIC_BASE/ffmpeg-darwin-x64"
      )
      FP_URLS=(
        "$OSXEXP/ffprobe6arm.zip"
        "$FFSTATIC_BASE/ffprobe-darwin-arm64"
        "https://evermeet.cx/ffprobe/getrelease/zip"
        "$FFSTATIC_BASE/ffprobe-darwin-x64"
      )
    else
      FF_URLS=(
        "$OSXEXP/ffmpeg6intel.zip"
        "https://evermeet.cx/ffmpeg/getrelease/zip"
        "$FFSTATIC_BASE/ffmpeg-darwin-x64"
      )
      FP_URLS=(
        "$OSXEXP/ffprobe6intel.zip"
        "https://evermeet.cx/ffprobe/getrelease/zip"
        "$FFSTATIC_BASE/ffprobe-darwin-x64"
      )
    fi
    require_binary ffmpeg "$FF_DIR" "${FF_URLS[@]}" || true
    require_binary ffprobe "$FF_DIR" "${FP_URLS[@]}" || true

    # yt-dlp: standalone python-zipapp (needs a Python >=3.10 interpreter).
    # The zipapp ships with `#!/usr/bin/env python3`, but macOS system python3
    # is 3.9 (unsupported by yt-dlp), so re-shebang to a >=3.10 interpreter
    # when one is available. The engine's bundled yt_dlp module remains the
    # primary path; this zipapp is the CLI fallback surfaced via
    # XPST_YTDLP_PATH.
    if [[ ! -x "$YTDLP_DIR/yt-dlp" ]]; then
      fetch_ytdlp "$YTDLP_DIR/yt-dlp" \
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" || {
          warn "could not fetch the yt-dlp zipapp; the engine's bundled yt_dlp module still works"
        }
    fi
    if [[ -x "$YTDLP_DIR/yt-dlp" ]]; then
      fix_ytdlp_shebang() {
        local f="$1" interpreter="$2"
        local tmp="$f.tmpshebang"
        { printf '#!%s\n' "$interpreter"; tail -n +2 "$f"; } > "$tmp" \
          && mv "$tmp" "$f" && chmod +x "$f"
      }
      if head -c 2 "$YTDLP_DIR/yt-dlp" | grep -q '#!'; then
        if command -v python3.10 >/dev/null; then
          fix_ytdlp_shebang "$YTDLP_DIR/yt-dlp" /usr/bin/env\ python3.10
        elif [[ "$(python3 -c 'import sys; print(sys.version_info[:2] >= (3,10))' 2>/dev/null)" == "True" ]]; then
          : # system python3 already >= 3.10
        elif [[ -x /opt/homebrew/bin/python3 ]]; then
          fix_ytdlp_shebang "$YTDLP_DIR/yt-dlp" /opt/homebrew/bin/python3
        fi
      fi
      "$YTDLP_DIR/yt-dlp" --version >/dev/null 2>&1 || true
    fi
    ;;
  win-x64)
    require_binary ffmpeg.exe "$FF_DIR" \
      "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" || true
    require_binary ffprobe.exe "$FF_DIR" \
      "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" || true
    if [[ ! -x "$YTDLP_DIR/yt-dlp.exe" ]]; then
      fetch_ytdlp "$YTDLP_DIR/yt-dlp.exe" \
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" || {
          warn "could not fetch yt-dlp.exe"
        }
    fi
    ;;
  linux-x64|linux-arm64)
    if [[ "$platform" == linux-x64 ]]; then
      ARCH=amd64; BTBN_ARCH=linux64
    else
      ARCH=arm64; BTBN_ARCH=linuxarm64
    fi
    # Two independent upstreams. johnvansickle.com intermittently answers a CI
    # request with an HTML error page and HTTP 200, which install_tarxz rejects
    # as `rejected truncated/corrupt tar.xz`; the GitHub-hosted BtbN build is the
    # fallback, so one flaky mirror cannot fail the Linux lane. Both archives
    # carry ffmpeg and ffprobe (nested at the top level and in bin/).
    JVS_TARBALL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-$ARCH-static.tar.xz"
    BTBN_TARBALL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-$BTBN_ARCH-gpl.tar.xz"
    require_binary ffmpeg "$FF_DIR" "$JVS_TARBALL" "$BTBN_TARBALL" || true
    require_binary ffprobe "$FF_DIR" "$JVS_TARBALL" "$BTBN_TARBALL" || true
    if [[ ! -x "$YTDLP_DIR/yt-dlp" ]]; then
      fetch_ytdlp "$YTDLP_DIR/yt-dlp" \
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" || {
          warn "could not fetch the yt-dlp zipapp"
        }
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# Loud, actionable failure report (instead of a bare `curl: (18)`).
# ---------------------------------------------------------------------------
if [[ ${#FAILED[@]} -gt 0 ]]; then
  {
    echo
    echo "=============================================================="
    echo "MEDIA BINARY FETCH FAILED ($platform)"
    echo "Could not obtain: ${FAILED[*]}"
    echo
    echo "Every source below was tried with retries, backoff, partial-transfer"
    echo "detection and resume; all of them failed:"
    for src in "${TRIED_SOURCES[@]+"${TRIED_SOURCES[@]}"}"; do
      echo "  - $src"
    done
    echo
    echo "No alternative mirror or local fallback produced a usable binary."
    echo "Check network access to the mirrors above, or point XPST_FFMPEG_PATH /"
    echo "XPST_FFPROBE_PATH at an existing ffmpeg/ffprobe and re-run:"
    echo "  scripts/fetch-media-binaries.sh $platform"
    echo "=============================================================="
  } >&2
  exit 1
fi

echo "media binaries ready ($platform):"
ls -lh "$FF_DIR" "$YTDLP_DIR"