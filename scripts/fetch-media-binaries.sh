#!/usr/bin/env bash
# Fetch the media binaries (ffmpeg/ffprobe/yt-dlp) that `cargo tauri build`
# bundles into the xPST desktop app. They are NOT committed to git (see
# .gitignore) - run this before building, locally and in CI.
#
# PROVENANCE CONTRACT (see scripts/media-binaries.lock)
#   * every download comes from an IMMUTABLE versioned url, never `latest`;
#   * every download is checked against the SHA-256 recorded in the lock file
#     and is REFUSED if the bytes do not match - a rotated or tampered upstream
#     asset is a loud, named failure, never a silently different release;
#   * when several locked candidates exist for one artifact (Windows/Linux
#     have a second pinned source) the first healthy one wins, so a single
#     unavailable mirror cannot fail the lane while the artifact stays pinned;
#   * a partial/corrupt transfer cannot be installed: the checksum is verified
#     before anything is unpacked, and the unpacked binary is sanity-checked;
#   * a provenance record (url + expected + installed sha256 + byte size) is
#     written next to the binaries and printed, so a release states exactly
#     what it shipped.
#
# There is deliberately NO unpinned fallback: an unverified binary must never
# end up inside a release. For local development only,
# XPST_ALLOW_UNPINNED_MEDIA=1 permits using an ffmpeg/ffprobe already on PATH;
# it is recorded as UNPINNED in the provenance file (the release lane asserts
# that file contains no UNPINNED line).
#
# Usage: scripts/fetch-media-binaries.sh [macos-arm64|macos-x64|win-x64|linux-x64|linux-arm64]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="${MEDIA_BINARIES_LOCK:-$ROOT/scripts/media-binaries.lock}"
FF_DIR="$ROOT/src-tauri/binaries/ffmpeg"
YTDLP_DIR="$ROOT/src-tauri/binaries/ytdlp"
PROVENANCE="${MEDIA_BINARIES_PROVENANCE:-$ROOT/src-tauri/binaries/PROVENANCE.txt}"
# Shared download cache: two artifacts can come out of one archive (the BtbN
# tarball carries both ffmpeg and ffprobe), so the same pinned url is fetched
# once per run.
CACHE="${MEDIA_BINARIES_CACHE:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/xpst-media-binaries}"
mkdir -p "$FF_DIR" "$YTDLP_DIR" "$CACHE"
rm -f "$PROVENANCE"
: >"$PROVENANCE"

# Tuning knobs (overridable so tests/slow networks can adjust).
: "${FETCH_RETRIES:=3}"        # curl-level attempts per url (--retry)
: "${FETCH_RETRY_DELAY:=2}"    # curl backoff + our inter-round delay, seconds
: "${FETCH_ROUNDS:=2}"         # script-level rounds (round 2 resumes partials)
: "${FETCH_CONNECT_TIMEOUT:=20}"
: "${FETCH_MAX_TIME:=1800}"    # a pinned ffmpeg asset is ~80MB

log()  { printf '%s\n' "$*" >&2; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

TRIED=()          # candidate urls tried, in order (for the failure report)
MISMATCH=()       # "artifact url expected actual" provenance violations
note_tried() { TRIED+=("$1"); }

have() { [[ -x "$1" ]]; }

# sha256_of <file>: shasum / sha256sum / openssl / python, whichever exists.
# Covers macOS (shasum), Linux (sha256sum) and Git Bash on Windows (openssl or
# the Python that actions/setup-python installed).
sha256_of() {
  local f="$1" out=""
  if command -v shasum >/dev/null 2>&1; then
    out="$(shasum -a 256 "$f" | awk '{print $1}')"
  elif command -v sha256sum >/dev/null 2>&1; then
    out="$(sha256sum "$f" | awk '{print $1}')"
  elif command -v openssl >/dev/null 2>&1; then
    out="$(openssl dgst -sha256 "$f" | awk '{print $NF}')"
  elif command -v python3 >/dev/null 2>&1; then
    out="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$f")"
  elif command -v python >/dev/null 2>&1; then
    out="$(python -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$f")"
  else
    die "no sha256 tool found (need shasum, sha256sum, openssl or python)"
  fi
  printf '%s' "$out" | tr 'A-Z' 'a-z'
}

# download <url> <dest>: retried, resumed, fail-closed transfers.
download() {
  local url="$1" dest="$2"
  local round=1 rc=0 note="" opts=()
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
      # Resume made it fail (no byte-range support) - restart from scratch.
      warn "discarding partial file and retrying without resume"
      rm -f "$dest"
    fi
    sleep $(( FETCH_RETRY_DELAY * round ))
    round=$(( round + 1 ))
  done
}

# verify_binary <path> <label>: non-empty and looks like a Mach-O/ELF/PE file.
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

# verify_ytdlp <path> <label>: yt-dlp ships either as a python-zipapp
# (`#!/usr/bin/env python3`, what the macOS/Linux x64 lanes bundle) or as a
# standalone PE/ELF/Mach-O build (Windows, linux-arm64), so the ffmpeg magic
# rule does not apply - but a truncated transfer still must not be installed.
verify_ytdlp() {
  local path="$1" label="$2"
  if [[ ! -s "$path" ]]; then
    warn "$label: downloaded file is empty or missing"
    return 1
  fi
  if head -c 2 "$path" | grep -q '#!'; then
    chmod +x "$path"
    return 0
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

# extract_member <archive> <kind> <member> <outdir>
# Unpacks a checked archive and copies the requested member out of it.
extract_member() {
  local archive="$1" kind="$2" member="$3" outdir="$4"
  local tmp="$CACHE/.unpack.$$"
  rm -rf "$tmp"; mkdir -p "$tmp"
  case "$kind" in
    zip)
      unzip -tq "$archive" >/dev/null 2>&1 || { warn "corrupt zip $(basename "$archive")"; rm -rf "$tmp"; return 1; }
      unzip -oq "$archive" -d "$tmp" >/dev/null 2>&1 || { warn "unzip failed for $(basename "$archive")"; rm -rf "$tmp"; return 1; }
      ;;
    tar.xz)
      tar -tJf "$archive" >/dev/null 2>&1 || { warn "corrupt tar.xz $(basename "$archive")"; rm -rf "$tmp"; return 1; }
      tar -xJf "$archive" -C "$tmp" >/dev/null 2>&1 || { warn "tar extraction failed for $(basename "$archive")"; rm -rf "$tmp"; return 1; }
      ;;
    *) warn "unknown archive kind: $kind"; rm -rf "$tmp"; return 1 ;;
  esac
  local found
  found="$(find "$tmp" -type f -path "*/$member" -not -path '*__MACOSX*' -print -quit)"
  if [[ -z "$found" ]]; then
    warn "$member: not present inside $(basename "$archive")"
    rm -rf "$tmp"; return 1
  fi
  cp -f "$found" "$outdir/.stage.$$"
  rm -rf "$tmp"
  return 0
}

# install_candidate <artifact> <destfile> <kind> <member> <sha256> <url>
# Download once (cached by checksum), verify the checksum BEFORE installing,
# then place the binary. Returns:
#   0 = installed from this candidate
#   1 = candidate unusable (download/unpack failure) - try the next one
#   (a checksum mismatch exits the script: it is a provenance violation, not a
#    flaky mirror.)
install_candidate() {
  local artifact="$1" destfile="$2" kind="$3" member="$4" expected="$5" url="$6"
  local cached="$CACHE/$expected"
  note_tried "$url"

  if [[ ! -s "$cached" || "$(sha256_of "$cached")" != "$expected" ]]; then
    rm -f "$cached"
    download "$url" "$cached" || return 1
  fi

  local actual
  actual="$(sha256_of "$cached")"
  if [[ "$actual" != "$expected" ]]; then
    MISMATCH+=("$artifact|$url|$expected|$actual")
    {
      echo
      echo "=============================================================="
      echo "MEDIA BINARY PROVENANCE MISMATCH"
      echo "  artifact : $artifact"
      echo "  url      : $url"
      echo "  locked   : sha256 $expected"
      echo "  actual   : sha256 $actual"
      echo
      echo "The bytes served by a pinned url are not the bytes the lock file"
      echo "(scripts/media-binaries.lock) pins. Refusing to bundle them."
      echo "Upstream changed a release asset: verify why, then re-pin with"
      echo "  scripts/update-media-binaries-lock.py --check"
      echo "  scripts/update-media-binaries-lock.py --write"
      echo "=============================================================="
    } >&2
    exit 1
  fi

  local staged
  case "$kind" in
    raw)
      staged="$CACHE/.stage.$$"
      cp -f "$cached" "$staged"
      ;;
    zip|tar.xz)
      extract_member "$cached" "$kind" "$member" "$CACHE" || return 1
      staged="$CACHE/.stage.$$"
      ;;
    *) warn "$artifact: unknown kind $kind"; return 1 ;;
  esac

  if [[ "$artifact" == "yt-dlp" ]]; then
    if ! verify_ytdlp "$staged" "$artifact"; then
      rm -f "$staged"
      return 1
    fi
  elif ! verify_binary "$staged" "$artifact"; then
    rm -f "$staged"
    return 1
  fi
  mv -f "$staged" "$destfile"
  log "ok: $artifact via $url (sha256 verified)"

  local bytes installed
  bytes="$(wc -c < "$destfile" | tr -d ' ')"
  installed="$(sha256_of "$destfile")"
  printf 'pinned\t%s\t%s\tsha256=%s\tinstalled_sha256=%s\tbytes=%s\turl=%s\n' \
    "$platform" "$artifact" "$expected" "$installed" "$bytes" "$url" >> "$PROVENANCE"
  return 0
}

# fetch_artifact <artifact> <destdir>: first locked candidate that installs.
fetch_artifact() {
  local artifact="$1" dstdir="$2"
  local destfile="$dstdir/$artifact"
  if [[ "$platform" == "win-x64" ]]; then
    destfile="$dstdir/$artifact.exe"
  fi

  # Candidate list for this platform/artifact, in lock-file order.
  local lines=()
  while IFS= read -r line; do
    lines+=("$line")
  done < <(awk -F'\t' -v p="$platform" -v a="$artifact" \
             'NF >= 6 && $1 == p && $2 == a' "$LOCK")
  if [[ ${#lines[@]} -eq 0 ]]; then
    warn "$artifact: no pinned candidate in $(basename "$LOCK") for platform $platform"
  fi

  # An existing binary is only trusted when its own bytes match a locked
  # checksum; otherwise it is a stale/unverified leftover and is replaced.
  if have "$destfile"; then
    local existing cand expected
    existing="$(sha256_of "$destfile")"
    for cand in "${lines[@]+"${lines[@]}"}"; do
      expected="$(printf '%s' "$cand" | cut -f5)"
      if [[ "$existing" == "$expected" ]]; then
        log "already present and checksum-verified: $destfile (sha256 $existing)"
        printf 'pinned\t%s\t%s\tsha256=%s\tinstalled_sha256=%s\tbytes=%s\turl=%s\n' \
          "$platform" "$artifact" "$expected" "$existing" \
          "$(wc -c < "$destfile" | tr -d ' ')" "$(printf '%s' "$cand" | cut -f6)" >> "$PROVENANCE"
        return 0
      fi
    done
    warn "$destfile exists but matches no locked checksum; reinstalling from the pinned sources"
    rm -f "$destfile"
  fi

  local cand
  for cand in "${lines[@]+"${lines[@]}"}"; do
    local kind member
    expected="$(printf '%s' "$cand" | cut -f5)"
    kind="$(printf '%s' "$cand" | cut -f3)"
    member="$(printf '%s' "$cand" | cut -f4)"
    local url
    url="$(printf '%s' "$cand" | cut -f6)"
    if install_candidate "$artifact" "$destfile" "$kind" "$member" "$expected" "$url"; then
      return 0
    fi
    warn "candidate exhausted for $artifact: $url"
  done

  # Local development escape hatch only - never used by the release lanes.
  if [[ "${XPST_ALLOW_UNPINNED_MEDIA:-}" == "1" ]]; then
    local bare="${artifact}"
    [[ "$platform" == "win-x64" ]] && bare="$artifact.exe"
    local onpath
    onpath="$(command -v "$bare" 2>/dev/null || true)"
    if [[ -n "$onpath" && "$onpath" != "$destfile" ]]; then
      warn "$artifact: UNPINNED local fallback to $onpath (XPST_ALLOW_UNPINNED_MEDIA=1)"
      cp "$onpath" "$destfile" && chmod +x "$destfile"
      printf 'UNPINNED\t%s\t%s\tpath=%s\n' "$platform" "$artifact" "$onpath" >> "$PROVENANCE"
      return 0
    fi
  fi
  return 1
}

# --- platform -----------------------------------------------------------------
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
  macos-arm64|macos-x64|win-x64|linux-x64|linux-arm64) : ;;
  *) echo "unsupported platform argument: $platform" >&2; exit 1 ;;
esac
[[ -f "$LOCK" ]] || die "lock file not found: $LOCK"

FAILED=()
fetch_artifact ffmpeg "$FF_DIR" || FAILED+=(ffmpeg)
fetch_artifact ffprobe "$FF_DIR" || FAILED+=(ffprobe)
fetch_artifact yt-dlp "$YTDLP_DIR" || FAILED+=(yt-dlp)

# macOS/Linux x64 yt-dlp is the python-zipapp with `#!/usr/bin/env python3`;
# the system python3 on macOS is often < 3.10 (unsupported by yt-dlp), so
# re-shebang to a >=3.10 interpreter when one is available. The engine's
# bundled yt_dlp module remains the primary path for xPST itself.
if [[ "$platform" == "macos-arm64" || "$platform" == "macos-x64" ]] && [[ -x "$YTDLP_DIR/yt-dlp" ]]; then
  if head -c 2 "$YTDLP_DIR/yt-dlp" | grep -q '#!'; then
    fix_ytdlp_shebang() {
      local f="$1" interpreter="$2" tmp
      tmp="$f.tmpshebang"
      { printf '#!%s\n' "$interpreter"; tail -n +2 "$f"; } > "$tmp" && mv "$tmp" "$f" && chmod +x "$f"
    }
    ytdlp_before="$(sha256_of "$YTDLP_DIR/yt-dlp")"
    if command -v python3.10 >/dev/null 2>&1; then
      fix_ytdlp_shebang "$YTDLP_DIR/yt-dlp" /usr/bin/env\ python3.10
    elif [[ "$(python3 -c 'import sys; print(sys.version_info[:2] >= (3,10))' 2>/dev/null)" == "True" ]]; then
      : # system python3 is already >= 3.10
    elif [[ -x /opt/homebrew/bin/python3 ]]; then
      fix_ytdlp_shebang "$YTDLP_DIR/yt-dlp" /opt/homebrew/bin/python3
    fi
    # The rewritten shebang changes the file, so the provenance record must
    # describe the bytes that actually ship - never the pre-edit download.
    if [[ "$(sha256_of "$YTDLP_DIR/yt-dlp")" != "$ytdlp_before" ]]; then
      rewrite_ytdlp_provenance() {
        local tmp="$PROVENANCE.tmp" line tab=$'	'
        while IFS= read -r line; do
          [[ "$line" == *"${tab}yt-dlp${tab}"* ]] && continue
          printf '%s\n' "$line" >> "$tmp"
        done < "$PROVENANCE"
        printf 'pinned\t%s\tyt-dlp\tsha256=%s\tinstalled_sha256=%s\tbytes=%s\turl=%s\tshebang_rewritten=1\n' \
          "$platform" "$(awk -F'\t' -v p="$platform" '$1==p && $2=="yt-dlp" {print $5}' "$LOCK" | head -n1)" \
          "$(sha256_of "$YTDLP_DIR/yt-dlp")" "$(wc -c < "$YTDLP_DIR/yt-dlp" | tr -d ' ')" \
          "$(awk -F'\t' -v p="$platform" '$1==p && $2=="yt-dlp" {print $6}' "$LOCK" | head -n1)" >> "$tmp"
        mv -f "$tmp" "$PROVENANCE"
      }
      rewrite_ytdlp_provenance
    fi
  fi
fi

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
    echo "Every pinned candidate below was tried with retries, backoff,"
    echo "partial-transfer detection and resume; all of them failed:"
    for src in "${TRIED[@]+"${TRIED[@]}"}"; do
      echo "  - $src"
    done
    echo
    echo "Candidates come from scripts/media-binaries.lock. A pinned url that"
    echo "is gone means the lock is stale, not that the release is flaky:"
    echo "  scripts/update-media-binaries-lock.py --check"
    echo "For local development only, XPST_ALLOW_UNPINNED_MEDIA=1 lets an"
    echo "ffmpeg/ffprobe already on PATH be used instead (recorded as UNPINNED;"
    echo "the release lane refuses such a build)."
    echo "=============================================================="
  } >&2
  exit 1
fi

echo "media binaries ready ($platform, checksums verified against scripts/media-binaries.lock):"
ls -lh "$FF_DIR" "$YTDLP_DIR"
echo "--- provenance record: $PROVENANCE ---"
cat "$PROVENANCE"
