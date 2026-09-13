# Tauri updater — production wiring (xPST desktop shell)

`src-tauri/tauri.conf.json` keeps the **production-facing** updater settings
(endpoint `https://tysais.github.io/xPST/updates/latest.json`, release pubkey
slot). The local E2E (`scripts/updater-e2e.sh`) does **not** mutate that file:
it injects the local endpoint, a throwaway pubkey and the test version via a
Tauri config overlay (`cargo tauri build --config`), so the production config is
never mutated by the test.

## Production config

In `src-tauri/tauri.conf.json` → `plugins.updater`:

```json
"updater": {
  "endpoints": ["https://tysais.github.io/xPST/updates/latest.json"],
  "pubkey": "<PUBLIC HALF OF THE RELEASE SIGNING KEYPAIR>"
}
```

and `bundle.createUpdaterArtifacts` is already `true` for release builds.

Notes:

- The committed config has `createUpdaterArtifacts: true`. Release builds fail
  closed unless `TAURI_SIGNING_PRIVATE_KEY` is present; the release workflow
  exports the owner-managed key from CI secrets.
- Never commit `dangerousInsecureTransportProtocol: true` — that flag exists
  only in the E2E overlay because the local endpoint is plain
  `http://127.0.0.1:9555`. The production endpoint is HTTPS.
- The endpoint serves a static manifest (GitHub Pages is deployed by
  `.github/workflows/publish-updater.yml`):
  `{ "version", "notes", "pub_date", "platforms": { "darwin-aarch64": { "signature", "sha512", "url" } } }`.
  `signature` = the contents of the `.sig` file produced at build time; `sha512`
  = the digest of the exact payload bytes; `url` = the absolute GitHub Release
  asset URL.

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

The release workflow builds the four platform artifacts and their `.sig`
sidecars. After the canonical GitHub Release exists,
`.github/workflows/publish-updater.yml` downloads those artifacts, runs
`scripts/gen-updater-manifest.py`, stages the exact
`updates/latest.json` path, and deploys it with the GitHub Pages Actions
publisher. See `docs/RELEASE.md` for the owner runbook and live endpoint
verification commands.

```bash
# CI or local, with TAURI_SIGNING_PRIVATE_KEY(_PASSWORD) exported:
cargo tauri build --bundles app          # xPST.app + xPST.app.tar.gz + .sig on macOS
python scripts/gen-updater-manifest.py --help
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
5. Asserts the version-A process checks, downloads, verifies the minisign
   signature, installs, restarts, and that a `started-0.2.0.txt` marker — written
   by `lib.rs` **inside the relaunched process** — appears in
   `/private/tmp/xpst-updater-e2e/`.

Known pitfalls baked into the script:

- Everything under `/private/tmp/...` (canonical path): the updater refuses to
  run when `current_exe()` crosses a symlink and `/tmp` is a symlink on macOS.
- Cleanup (`pkill`) is scoped to `/private/tmp/xpst-updater-e2e/` — never a
  generic `xPST.app` pattern, which would kill other agents' bundles.
