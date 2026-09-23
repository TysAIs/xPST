# Engine sidecar bundle

`engine/` is the PyInstaller **onedir** build of the FastAPI dashboard
engine (entrypoint: `scripts/engine_entry.py`, spec: `build_engine.spec`).
It is a build artifact and is NOT committed to git — build it with:

```bash
scripts/build-engine.sh          # builds + smoke-checks the sidecar
(cd src-tauri && cargo tauri build --bundles app)
```

`tauri.conf.json` ships it as a bundle resource
(`bundle.resources: ["binaries/engine/"]`); the shell spawns
`resource_dir()/binaries/engine/xpst-engine` at boot.

Why onedir-as-resource and not `externalBin` (onefile): `externalBin`
requires a single executable file, and PyInstaller onefile self-extracts
~45 MB on **every** launch (~1.3 s), blowing the boot-to-ready ≤ 1 s gate.
Onedir has no extraction step; the tradeoff is manual process management,
which the shell owns (env, health poll, kill on exit/panic/signal).

## Media binaries

Two very different things live behind that one phrase:

**`ytdlp/` — bundled.** The `yt-dlp` zipapp (~3 MB, no system
equivalent) is fetched before building and shipped as a bundle resource:

```bash
scripts/fetch-media-binaries.sh            # auto-detects the host platform
```

The Tauri shell sets `XPST_YTDLP_PATH` on the spawned engine process,
pointing at that resource (honored by
`xpst.utils.platform.resolve_ytdlp_path`).

**`ffmpeg/` — deliberately NOT bundled.** ffmpeg + ffprobe were 87 MB of a
192 MB app, and the flaky mirror behind that download is what kept the Tauri
release lane red. They are resolved at runtime instead, in this order
(`xpst/utils/platform.py`, `xpst/media/binaries.py`):

1. `XPST_FFMPEG_PATH` / `XPST_FFPROBE_PATH` — explicit override;
2. a system install (PATH, then the locations a GUI-launched macOS app must
   probe itself: `~/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`);
3. a copy fetched on first use into `<config dir>/bin` (`~/.xpst/bin`),
   pinned to an immutable upstream release tag and verified against a
   SHA-256 before it is installed.

A machine that already has ffmpeg downloads nothing. The desktop app runs the
fetch automatically on first launch (`XPST_MEDIA_AUTO_FETCH=0` opts out) and
the CLI exposes it explicitly as `xpst media status` / `xpst media fetch`.

Do not add an `ffmpeg/` directory back to `bundle.resources`: the release
lane's size budget (130 MB unpacked app / 150 MB installer) and
`tests/test_ffmpeg_not_bundled.py` both fail if it comes back.