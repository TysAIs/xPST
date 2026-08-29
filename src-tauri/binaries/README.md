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

## Media binaries: `ffmpeg/` and `ytdlp/`

Static `ffmpeg` + `ffprobe` and the `yt-dlp` zipapp are also shipped here
as bundle resources so users never install anything. Like `engine/`, these
are build artifacts and NOT committed — fetch them before building:

```bash
scripts/fetch-media-binaries.sh            # auto-detects the host platform
```

The Tauri shell sets `XPST_FFMPEG_PATH`, `XPST_FFPROBE_PATH`, and
`XPST_YTDLP_PATH` on the spawned engine process, pointing at the resource
binaries (honored by `xpst.utils.platform.resolve_ffmpeg_path`,
`resolve_ffprobe_path`, and `resolve_ytdlp_path`).
