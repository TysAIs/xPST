# Packaging the xPST desktop app

xPST ships **one** desktop app: the Tauri 2 shell in `src-tauri/`, which opens a
native window on the local dashboard UI (`ui/`, built with Vite + Svelte) and
spawns the Python engine as a bundled sidecar. PyPI publication is not available
today; see [INSTALL.md](INSTALL.md) for the published release assets and their
current status.

The legacy PySide6/QML desktop lane (`src/xpst/desktop_app/`,
`build_macos.spec`, `build_windows.spec`, `build_linux.spec`, `build.sh`) was
removed. It produced a second `xPST.app` whose bundle identifier
(`com.tysais.xpst`) collided exactly with the Tauri product, so an installer
could not be attributed to the app it came from.

## What a bundle contains

| Piece | Source | Where it lands in the app |
|---|---|---|
| Shell | `src-tauri/` (Rust, `cargo tauri build`) | the executable + `.app` wrapper |
| Web UI | `ui/` (`npm ci && npm run build` → `ui/dist/`) | `Contents/Resources/ui/` |
| Engine sidecar | `scripts/build-engine.sh` → `build_engine.spec` (PyInstaller **onedir**) | `Contents/Resources/binaries/engine/xpst-engine` |
| yt-dlp | `scripts/fetch-media-binaries.sh` | `Contents/Resources/binaries/ytdlp/` |

FFmpeg is **not** bundled (it was ~87 MB of a ~192 MB app): the engine prefers a
system ffmpeg and otherwise fetches a pinned, checksum-verified static build on
first use (`xpst media fetch`).

Onedir, not onefile: Tauri's `externalBin` requires a single file, which forces
onefile, and a onefile sidecar self-extracts ~45 MB on every launch (~1.3 s),
blowing the boot-to-ready budget. See `src-tauri/binaries/README.md`.

## Build locally (macOS example)

```bash
# 0. Prerequisites: Python >= 3.10 with the xpst deps, Node 22, Rust + tauri-cli
python -m pip install -e ".[full]" pyinstaller
cargo install tauri-cli --version 2.11.4 --locked

# 1. Web UI
cd ui && npm ci && npm run build && cd ..

# 2. Engine sidecar (POSIX; works in Git Bash on Windows; host arch only)
PYTHON="$(command -v python3 || command -v python)" scripts/build-engine.sh

# 3. Shell + installer for this OS
export PATH="$HOME/.cargo/bin:$PATH"
cd src-tauri && cargo tauri build --bundles app     # macOS .app; see below for installers
```

`scripts/build-engine.sh` deletes and rewrites `src-tauri/binaries/engine/`
except the tracked `.gitkeep`, and it refuses to cross-compile: PyInstaller
builds for the host OS only, so each platform is packaged on its own runner.

Cross-compilation is not supported. `src-tauri/binaries/{engine,ffmpeg,ytdlp}/`
must exist (they are gitignored placeholders) or Tauri's build script fails with
a resource-path error before compiling anything.

### Keyless builds

`bundle.createUpdaterArtifacts` is `false` in `src-tauri/tauri.conf.json`, so a
build without `TAURI_SIGNING_PRIVATE_KEY` still produces the full configured
bundle set with Tauri's default/ad-hoc signing. Do **not** pass `--no-sign` to
work around a missing key — that also skips macOS codesigning/notarization.
`src-tauri/tauri.conf.json` cannot hold comments: Tauri's config is
`#[serde(deny_unknown_fields)]` and repo tooling parses the file with
`json.loads`.

## What CI publishes

`.github/workflows/tauri-release.yml` is the only workflow that builds desktop
installers. On a `v*` tag (or manual dispatch) it builds, per platform:

| Platform | Bundles |
|---|---|
| macOS `aarch64-apple-darwin` | `xPST.app`, `.dmg` |
| Windows `x86_64-pc-windows-msvc` | NSIS `.exe`, `.msi` |
| Linux `x86_64-unknown-linux-gnu` | `.deb`, `.AppImage` |

Each lane builds the UI, runs `cargo test` on the shell, builds the sidecar,
asserts the expected installer really exists, enforces the installer size budget
(≤150 MB) and the unpacked-app budget (≤130 MB), then smoke-boots the result
(`.AppImage` under xvfb with the engine sidecar, the NSIS installer on Windows).

`.github/workflows/release.yml` is the Python lane only: wheel + sdist + SBOM +
the GitHub release and its aggregate checksums.

## App identity

`productName` is `xPST` and the bundle identifier is `com.tysais.xpst`
(`src-tauri/tauri.conf.json`, version `1.1.0`). The shell holds a
single-instance lock at `/tmp/xpst-shell-single-instance.lock`, so a stale copy
of the app left running (for example from an updater E2E build) makes every
fresh launch exit immediately. Kill stray copies before believing a smoke
failure.

## Universal2 (x86_64 + arm64) status

macOS builds are single-arch (arm64) today; Tauri is invoked with the explicit
`aarch64-apple-darwin` target in CI. An Intel build would be a second target
triple, not a fat binary, because the Python engine sidecar it spawns is
arch-specific.