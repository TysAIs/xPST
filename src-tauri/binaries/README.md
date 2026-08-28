# Engine sidecar binaries

`xpst-engine-<rust-target-triple>` is the PyInstaller onefile build of the
FastAPI dashboard engine (entrypoint: `scripts/engine_entry.py`, spec:
`build_engine.spec`). It is a build artifact and is NOT committed to git —
build it with:

```bash
scripts/build-engine.sh          # builds + smoke-checks the sidecar
(cd src-tauri && cargo tauri build --bundles app)
```

`tauri.conf.json` declares it under `bundle.externalBin`
(`binaries/xpst-engine`); the bundler strips the target-triple suffix and
copies it into `xPST.app/Contents/MacOS/` next to the shell binary.

Note: `externalBin` requires a single executable file, which is why the
sidecar is built `--onefile` (onedir bundles are not supported).
