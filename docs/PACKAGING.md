# Packaging xPST desktop apps

xPST ships as a Python package (PyPI) **and** as standalone desktop bundles
per platform. This document covers building and using the desktop bundles.

> Status: macOS arm64 is the maintained path (built + verified on aarch64,
> macOS 27 / PyInstaller 6). Linux/Windows specs exist but are only built on
> their respective platforms. macOS is a single-arch (arm64) bundle — see
> "Universal2" below.

## macOS: build the .app

Prerequisites: the project venv with `pyinstaller` installed (dev extra), and
a native macOS machine (arm64 recommended).

```bash
cd ~/xPST
source .venv/bin/activate      # uv venv on this repo; pyinstaller available
./build.sh macos               # -> dist/xPST.app
```

Two runtime notes:

- **Offline transcription is not bundled.** PyAV (`av`) and `faster-whisper`
  are excluded from the macOS bundle on purpose: PyAV 18 ships dylibs under
  `av/__dot__dylibs/` which trips PyInstaller's BUNDLE copy step
  (FileNotFoundError on `libSvtAv1Enc`). Both are lazy-imported only by
  offline transcription (`xpst.knowledge.ingest.transcribe`), so the desktop
  app runs fine without them. Online captioning/transcription (API-based) is
  unaffected.
- **Unsigned / ad-hoc signed.** The repo has no Apple signing secrets, so the
  bundle is ad-hoc signed. If macOS gates first launch: right-click the app in
  Finder → Open → Open, or System Settings → Privacy & Security → Open Anyway.
  It is safe and local — xPST never sends your data anywhere except the
  social platform you post to.

### Pin the app to the Dock

1. `open dist/xPST.app` once (first launch).
2. Right-click the xPST icon in the Dock → **Options → Keep in Dock**.

### Known cosmetic issue (non-blocking)

The frozen app logs `QML FontLoader: Cannot load font
…/Contents/assets/fonts/lucide.ttf`. The font is actually at
`Contents/Resources/assets/fonts/lucide.ttf`; `resource_path()` in frozen
mode resolves against the PyInstaller extraction base and misses the
Resources location. The UI still renders fully (unused-glyph fallback in the
icon font may occur in a few places). Tracked as a follow-up.

## Universal2 (x86_64 + arm64) status

Blocked on current dependency tree: 420 arm64-only `.so`/`.dylib` files
(Pillow, numpy, av, aiohttp, PySide6 pieces) plus an arm64-only uv-managed
Python make PyInstaller's `universal2` requirement (fat binaries everywhere)
unmet. Not needed for arm64 Macs; revisit if x86_64 Mac distribution becomes
a goal.

## Windows / Linux

`./build.sh windows` (requires Wine + PyInstaller on this repo's dev box) and
`./build.sh linux` are configured via `build_windows.spec` / `build_linux.spec`
but are built on their respective host platforms. CI release workflow
(`.github/workflows/release.yml`) compiles the per-platform assets.

## The ~15 MB Tauri plan (Phase 3.2, greenfield)

The long-term target is a lightweight **Tauri 2 shell** (~15 MB) wrapping the
local FastAPI dashboard (`python -m xpst dashboard` / `xpst ui`) instead of
shipping a ~1 GB PySide6 bundle. As of 2026-08 there is **no `src-tauri/`**
in the repo — it is greenfield. Requirements on the build box: Rust toolchain
(`cargo`/`rustc`) + `tauri-cli`, plus product decisions on dashboard process
spawn, localhost port, and auth handoff. See the xPST bot's plan under
`missions/work/desktop-app-phase3-*.md`.
