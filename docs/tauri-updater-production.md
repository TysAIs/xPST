# Tauri updater — production wiring (xPST desktop shell)

> **One manifest generator.** `scripts/gen-updater-manifest.py` is the *only*
> authority for `latest.json` in this repository. It is invoked by both
> pipelines that can produce a manifest — `.github/workflows/publish-updater.yml`
> (publishes to the Pages root) and `.github/workflows/tauri-release.yml`
> (`publish-updater-manifest` job, attaches the manifest to the tagged release).
> A second, differently named generator (`scripts/generate_update_manifest.py`)
> was proposed and rejected: two scripts writing the same manifest is how a
> release advertises a platform it never built, or a signature that does not
> match `plugins.updater.pubkey`. `tests/test_updater_manifest.py` fails if a
> second generator ever reappears (`test_there_is_exactly_one_manifest_generator`).

## Update endpoint: hosting decision (verified, not assumed)

Configured endpoint: `plugins.updater.endpoints[0]` =
`https://tysais.github.io/xPST/updates/latest.json`.

**Decision: keep the GitHub Pages endpoint.** GitHub Pages for this repository
serves `build_type: legacy` from source `main:/` — a *committed file*, not an
upload target — and it genuinely serves committed paths:

```console
$ curl -s -o /dev/null -w '%{http_code}' https://tysais.github.io/xPST/privacy/
200
$ curl -s -o /dev/null -w '%{http_code}' https://tysais.github.io/xPST/terms/
200
$ curl -s -o /dev/null -w '%{http_code}' https://tysais.github.io/xPST/this-path-does-not-exist-xyz/
404
```

So Pages *can* serve `updates/latest.json`; the endpoint is not in the
repository today because no release has ever produced a signed updater
artifact, and `publish-updater.yml` refuses to invent one.

**Observed status right now (2026-09-14), stated as measured:**

```console
$ curl -s -o /dev/null -w '%{http_code}' https://tysais.github.io/xPST/updates/latest.json
404
$ curl -s -o /dev/null -w '%{http_code}' https://github.com/TysAIs/xPST/releases/latest/download/latest.json
404
```

The `404` is a **signing-key** blocker, not a hosting blocker: this repository
has **0** GitHub Actions secrets, so `TAURI_SIGNING_PRIVATE_KEY` is absent, so
`cargo tauri build` emits no `.sig`, so there is nothing a manifest could
legitimately point at.

The Release-asset URL
(`https://github.com/TysAIs/xPST/releases/latest/download/latest.json`) is kept
as a **documented fallback**: both `publish-updater.yml` and the release lane's
`publish-updater-manifest` job attach `latest.json` to the release, so once a
signed release exists that URL serves the same bytes without depending on a
commit to branch-protected `main`. It is *not* the configured endpoint because
`releases/latest` resolves to whichever release is newest and this repository
has two independent pipelines publishing to the same tag; the committed Pages
path is stable regardless.

Direct commits to `main` are gated by branch protection (13 required status
checks, `allow_force_pushes: false`), so `GITHUB_TOKEN` cannot create the
manifest commit; `publish-updater.yml` force-updates `updater/latest-json` and
opens a pull request in that case and stays green.

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

## How `latest.json` actually gets published

The endpoint `https://tysais.github.io/xPST/updates/latest.json` is a **committed
file**, not an upload target. GitHub Pages for this repository runs with
`build_type: legacy` and source `main:/` (`gh api repos/TysAIs/xPST/pages`), so
`actions/deploy-pages` cannot deploy here, and a Pages *artifact* upload would
publish a site built from that artifact — replacing the site instead of adding
`updates/` to it. The served path is therefore `updates/latest.json` on `main`,
next to `privacy/` and `terms/`.

`.github/workflows/publish-updater.yml` builds and lands that file:

1. Triggers: `release: [published]`, `workflow_run` of `Tauri Shell Release`
   (completed), and `workflow_dispatch` with a `release_tag` input.
2. Downloads the release's updater artifacts (`gh release download`, patterns
   for `*.app.tar.gz`, `*setup.exe`, `*.AppImage`, `*.msi` plus the `<platform>-`
   prefixed names) into a scratch directory.
3. `scripts/select-updater-artifacts.py` resolves exactly one **signed** artifact
   per target. A platform with no artifact is `absent`, with no non-empty `.sig`
   is `unsigned`, with several candidates is `ambiguous` — none of those abort
   the run, and none of them is guessed.
4. `scripts/gen-updater-manifest.py` writes the manifest for the platforms that
   are publishable, with the `.sig` contents and a SHA-512 of the exact artifact
   bytes; it refuses to write anything if the configured `plugins.updater.pubkey`
   trust root is missing.
5. `scripts/publish-updater.sh` validates the manifest and stages it at
   `updates/latest.json` in the checkout.
6. The job commits that file to `main` (Pages rebuilds from the branch) and also
   attaches `latest.json` to the release.

Manual run, including a build-only rehearsal:

```bash
gh workflow run publish-updater.yml --repo TysAIs/xPST -f release_tag=v1.2.3 -f dry_run=true
```

Invariants worth keeping:

- **The job never signs anything and needs no signing secret.** It publishes only
  artifacts that already carry a `.sig`. With no `TAURI_SIGNING_PRIVATE_KEY`
  secret (this repository has none) it emits a notice and a job summary and
  finishes green — it must never fail the run over an absent key, and it must
  never invent a signature.
- A release with no updater artifacts leaves the committed manifest untouched, so
  the endpoint keeps serving the last good manifest instead of going 404 or
  advertising an artifact that cannot be verified.
- **Direct commits to `main` are gated by branch protection.** `main` requires 13
  status-check contexts, so the `GITHUB_TOKEN` cannot create a new commit there
  (GitHub rejects the ref update with `GH006: ... Required status check ... is
  expected`). The job handles that by force-updating the `updater/latest-json`
  branch and opening/refreshing a pull request, and stays green. Fully automatic
  publishing needs a bypass: a token stored as a secret whose actor may push to
  `main`, or a repository ruleset with a bypass for the GitHub Actions app.

Current gap for a real update round-trip (tracked outside this workflow): a
release only contains signed updater artifacts if the Tauri build ran with
`TAURI_SIGNING_PRIVATE_KEY` **and** the updater artifacts enabled — the release
lane now passes `--config '{"bundle":{"createUpdaterArtifacts":true}}'` for
exactly that build, so a keyed run emits the macOS `.app.tar.gz`, Windows
`*.nsis.zip` and Linux `*.AppImage.tar.gz` packages plus their `.sig` sidecars.
`gh release view v1.1.0` contains no updater artifact at all, which is exactly
the `absent` path above.

### What is blocked on the user's private key / missing secrets

Nothing in this branch can produce a signed artifact, because:

1. `TAURI_SIGNING_PRIVATE_KEY` (and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`) are
   **not configured** — the repository has **0** Actions secrets
   (`gh api repos/TysAIs/xPST/actions/secrets --jq .total_count` → `0`). The
   private key that matches the committed public key (minisign key id
   `13F290B1316626E2`) belongs to the repository owner and is deliberately not
   available; a manifest signature cannot be produced or faked without it.
2. `TAURI_SIGNING_PRIVATE_KEY` being absent is *expected*, not a failure: the
   release lane and `publish-updater.yml` both report the channel as blocked and
   succeed, so the pipeline stays honest instead of shipping an unsigned entry.
3. Publishing `updates/latest.json` to `main` additionally needs a push actor
   with a branch-protection bypass (or a human merging the `updater/latest-json`
   pull request), since `main` requires 13 status checks.

To unblock: generate the release keypair, set the `TAURI_SIGNING_PRIVATE_KEY`
secret, confirm `plugins.updater.pubkey` is the base64 of the matching `.pub`,
and push a `v*` tag. See "Release signing keypair" below.



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
