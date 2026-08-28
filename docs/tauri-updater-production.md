# Tauri updater — production wiring (xPST desktop shell)

`src-tauri/tauri.conf.json` keeps the **production-facing** updater settings
(endpoint `https://tysais.github.io/xPST/updates/latest.json`, release pubkey
slot). The local E2E (`scripts/updater-e2e.sh`) does **not** mutate that file:
it injects the local endpoint, a throwaway pubkey, `createUpdaterArtifacts` and
the test version via a Tauri config overlay (`cargo tauri build --config`), so
switching to production is a one-line config change (below) and CI workflows
that build the shell are unaffected.

## Production config (the one-line switch)

In `src-tauri/tauri.conf.json` → `plugins.updater`:

```json
"updater": {
  "endpoints": ["https://tysais.github.io/xPST/updates/latest.json"],
  "pubkey": "<PUBLIC HALF OF THE RELEASE SIGNING KEYPAIR>"
}
```

and set `"bundle.createUpdaterArtifacts": true` for release builds.

Notes:

- The committed config has `createUpdaterArtifacts: false` so plain
  `cargo tauri build` never requires signing keys; the release workflow
  (`.github/workflows/tauri-release.yml`) exports `TAURI_SIGNING_PRIVATE_KEY`
  from CI secrets and enables updater artifacts when the key is present.
- Never commit `dangerousInsecureTransportProtocol: true` — that flag exists
  only in the E2E overlay because the local endpoint is plain
  `http://127.0.0.1:9555`. The production endpoint is HTTPS.
- The endpoint serves a static manifest (GitHub Pages works well):
  `{ "version", "notes", "pub_date", "platforms": { "darwin-aarch64": { "signature", "url" } } }`.
  `signature` = the contents of the `.sig` file produced at build time; `url` =
  absolute URL of the `xPST.app.tar.gz` artifact.

## Release signing keypair

1. Generate a dedicated release keypair (do **not** reuse the E2E key):
   ```bash
   cargo tauri signer generate -w xpst-release.key --password ""
   ```
2. `pubkey` in `tauri.conf.json` = the base64 contents of `xpst-release.key.pub`.
3. Store the private key (`xpst-release.key`) as the GitHub Actions secret
   `TAURI_SIGNING_PRIVATE_KEY` (plus `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` if a
   password is set). tauri-cli ≥ 2.x honors the `_KEY` env var; note that
   `TAURI_SIGNING_PRIVATE_KEY_PATH` alone is **not** honored (tauri-cli 2.11.4).
4. The local E2E key lives at `.tauri/xpst-updater-e2e.key` (gitignored,
   `*.key` is already in `.gitignore`). It signs only local E2E artifacts and
   must never sign a production release.

## Release build & publish flow

```bash
# CI or local, with TAURI_SIGNING_PRIVATE_KEY(_PASSWORD) exported:
cargo tauri build --bundles app          # produces xPST.app + xPST.app.tar.gz + .sig
# upload xPST.app.tar.gz (e.g. to the GitHub Pages repo / a release asset)
# update latest.json with the new version + .sig contents
```

On this host the DMG bundler step (`bundle_dmg.sh`) hits an AppleScript
Finder timeout (`AppleEvent timed out. -1712`) in headless/automated sessions;
either build with `--bundles app` (updater artifacts are unaffected) or run
`bundle_dmg.sh --skip-jenkins` manually. Cosmetic only.

## macOS Gatekeeper / signing notes

- The Tauri updater verifies the **Tauri minisign signature** of
  `xPST.app.tar.gz` against `plugins.updater.pubkey` — that is what gates the
  install. This is independent from Apple codesigning.
- For a distributable app, macOS Gatekeeper requires the bundle to be
  codesigned with an Apple Developer ID Application cert **and notarized**
  (`notarytool` + staple); otherwise users must right-click → Open on first
  run. The updater's extracted/relaunched bundle inherits the signature of the
  parent, so keep the codesign+notarize step in the release pipeline before
  publishing updater artifacts.
- The E2E loop proven by `scripts/updater-e2e.sh` runs unsigned apps launched
  directly from their binary path (no LaunchServices quarantine is applied, so
  Gatekeeper does not intervene locally; no ad-hoc codesign was needed). Expect
  stricter behavior when apps are launched via `open`/Finder after download
  from the internet.

## What the E2E proves

`scripts/updater-e2e.sh` (run from repo root; exit codes documented in the
script header):

1. Builds v0.1.0 via a config overlay (`cargo tauri build --bundles app
   --config <overlay>`) with updater artifacts.
2. Serves `latest.json` on `http://127.0.0.1:9555`.
3. Rebuilds as v0.2.0 via the same overlay, replaces artifact + manifest.
4. Launches the v0.1.0 bundle with `XPST_UPDATER_CHECK=1` (the opt-in trigger
   in `src-tauri/src/lib.rs` — normal boots never auto-update).
5. Asserts the v0.1.0 process checks, downloads (~2.1 MB over HTTP), verifies
   the minisign signature, installs, restarts, and that a `started-0.2.0.txt`
   marker — written by `lib.rs` **inside the relaunched process** — appears in
   `/private/tmp/xpst-updater-e2e/`.

Known pitfalls baked into the script:

- Everything under `/private/tmp/...` (canonical path): the updater refuses to
  run when `current_exe()` crosses a symlink and `/tmp` is a symlink on macOS.
- Cleanup (`pkill`) is scoped to `/private/tmp/xpst-updater-e2e/` — never a
  generic `xPST.app` pattern, which would kill other agents' bundles.
