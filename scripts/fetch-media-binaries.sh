#!/usr/bin/env bash
# Fetch static media binaries (ffmpeg/ffprobe/yt-dlp) into src-tauri/binaries/
# so `cargo tauri build` can bundle them. These are NOT committed to git (see
# .gitignore) — run this before building, locally and in CI.
#
# Usage: scripts/fetch-media-binaries.sh [macos-arm64|macos-x64|win-x64|linux-x64|linux-arm64]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FF_DIR="$ROOT/src-tauri/binaries/ffmpeg"
YTDLP_DIR="$ROOT/src-tauri/binaries/ytdlp"
mkdir -p "$FF_DIR" "$YTDLP_DIR"

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

fetch() { # url -> dest
  local url="$1" dest="$2"
  echo "fetching $url"
  curl -fsSL --retry 3 -o "$dest" "$url"
}

case "$platform" in
  macos-arm64|macos-x64)
    # osxexperts.net static builds (arm64 = ffmpeg6arm.zip, x64 = ffmpeg6intel.zip)
    if [[ "$platform" == macos-arm64 ]]; then
      FF_URL=https://www.osxexperts.net/ffmpeg6arm.zip
      FP_URL=https://www.osxexperts.net/ffprobe6arm.zip
    else
      FF_URL=https://www.osxexperts.net/ffmpeg6intel.zip
      FP_URL=https://www.osxexperts.net/ffprobe6intel.zip
    fi
    if [[ ! -x "$FF_DIR/ffmpeg" ]]; then
      fetch "$FF_URL" "$FF_DIR/ffmpeg.zip"
      (cd "$FF_DIR" && unzip -oq ffmpeg.zip && rm -rf __MACOSX ffmpeg.zip)
    fi
    if [[ ! -x "$FF_DIR/ffprobe" ]]; then
      fetch "$FP_URL" "$FF_DIR/ffprobe.zip"
      (cd "$FF_DIR" && unzip -oq ffprobe.zip && rm -rf __MACOSX ffprobe.zip)
    fi
    chmod +x "$FF_DIR/ffmpeg" "$FF_DIR/ffprobe"
    # yt-dlp: standalone python-zipapp (needs a Python >=3.10 interpreter).
    # The zipapp ships with `#!/usr/bin/env python3`, but macOS system
    # python3 is 3.9 (unsupported by yt-dlp), so re-shebang to /usr/bin/env
    # python3.10 first and fall back to plain python3. On macOS also probe
    # Homebrew python. The engine's own bundled yt_dlp module remains the
    # primary path; this zipapp is the CLI fallback surfaced via
    # XPST_YTDLP_PATH.
    if [[ ! -x "$YTDLP_DIR/yt-dlp" ]]; then
      fetch https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp "$YTDLP_DIR/yt-dlp"
    fi
    chmod +x "$YTDLP_DIR/yt-dlp"
    fix_ytdlp_shebang() {
      local f="$1" interpreter="$2"
      # Rebuild the shebang line without touching the rest of the file.
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
    ;;
  win-x64)
    if [[ ! -f "$FF_DIR/ffmpeg.exe" ]]; then
      fetch https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip "$FF_DIR/ffmpeg-essentials.zip"
      (cd "$FF_DIR" && unzip -oq ffmpeg-essentials.zip \
        && cp ffmpeg-*-essentials_build/ffmpeg.exe ffmpeg-*-essentials_build/ffprobe.exe . \
        && rm -rf ffmpeg-*-essentials_build ffmpeg-essentials.zip)
    fi
    if [[ ! -f "$YTDLP_DIR/yt-dlp.exe" ]]; then
      fetch https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe "$YTDLP_DIR/yt-dlp.exe"
    fi
    ;;
  linux-x64|linux-arm64)
    if [[ "$platform" == linux-x64 ]]; then
      ARCH=amd64; else ARCH=arm64; fi
    if [[ ! -x "$FF_DIR/ffmpeg" ]]; then
      fetch "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-$ARCH-static.tar.xz" "$FF_DIR/ffmpeg.tar.xz"
      (cd "$FF_DIR" && tar xJf ffmpeg.tar.xz \
        && cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe . \
        && rm -rf ffmpeg-*-static ffmpeg.tar.xz)
    fi
    if [[ ! -x "$YTDLP_DIR/yt-dlp" ]]; then
      fetch https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp "$YTDLP_DIR/yt-dlp"
    fi
    chmod +x "$FF_DIR/ffmpeg" "$FF_DIR/ffprobe" "$YTDLP_DIR/yt-dlp"
    ;;
esac

echo "media binaries ready ($platform):"
ls -lh "$FF_DIR" "$YTDLP_DIR"
