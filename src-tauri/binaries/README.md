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
