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

## State measured 2026-11-14

```console
$ curl -s -o /dev/null -w '%{http_code}' https://tysais.github.io/xPST/updates/latest.json
200
$ gh secret list --repo TysAIs/xPST          # names only
PYPI_API_TOKEN
TAURI_SIGNING_PRIVATE_KEY
TAURI_SIGNING_PRIVATE_KEY_PASSWORD
```

What that means, precisely:

- The endpoint is live and serves the v1.2.1 manifest. Hosting was never the
  problem.
- The updater keypair exists, so builds can emit `.sig` sidecars. Those sidecars
  are **update integrity signatures** (minisign, verified by the app against
  `plugins.updater.pubkey`); they are not OS code signing.
- No Apple or Windows **code-signing** credential exists. macOS installers are
  ad-hoc signed (Gatekeeper warning) and Windows installers are unsigned
  (SmartScreen "unknown publisher"). See [Code signing](#code-signing-what-is-missing).

The v1.2.1 manifest also carried a defect worth remembering: both `darwin-*`
keys carried the arm64 `sha512` and signature while `darwin-x86_64` pointed at a
different file (`xPST.app.tar.gz`, sha512 `a4ffe4a1…` against the arm64
`2e969c3d…`). An Intel Mac therefore downloaded a payload it could never verify.
The selector and generator now make that state unrepresentable (below), and
`scripts/publish-updater.sh --assets-dir` re-hashes the released bytes before
anything is staged.

## How the app behaves

| Situation | Behaviour |
|---|---|
| Normal launch | The UI calls `updater_check` once, after routing, and shows a banner if a newer release exists. Nothing downloads. |
| User presses "Check for updates" (About → Updates) | Same command, user-initiated; the result is reported verbatim, including failures. |
| User presses "Install and restart" | `updater_install` downloads, verifies the minisign signature against the embedded public key, installs, and restarts. |
| `XPST_UPDATER_CHECK=1` | E2E harness mode: unattended check → install → relaunch, plus the `/private/tmp/xpst-updater-e2e` boot-version markers. |
| `XPST_UPDATER_CHECK=0` | No network check at all (offline runs, boot measurement). |
| Browser (no shell) | The UI reports that update checks need the desktop app; it never claims a check ran. |

The commands are registered app commands (`invoke_handler` in
`src-tauri/src/lib.rs`); Tauri allows app commands to every window by default,
and the loopback origin is granted by `capabilities/default.json`'s
`remote.urls`. No new permission entry is required.

Why this shape: the previous code gated the entire update path behind
`XPST_UPDATER_CHECK=1`, an E2E harness variable, so a user who installed xPST
was never told a newer release existed. The variable now selects only the
unattended harness behaviour.

## Manifest rules the pipeline enforces

`scripts/select-updater-artifacts.py` picks exactly one signed payload per
platform, or explains why it cannot:

- `<platform>-<name>` explicit names win (the release lane renames its outputs
  into that shape).
- A macOS `*.app.tar.gz` must name its architecture. `cargo tauri build` emits
  the bare name `xPST.app.tar.gz` on every architecture, so a bare name is
  reported `unattributable` rather than guessed into either darwin key.
- Windows: NSIS `setup.exe` wins over the WiX `.msi` when both are signed
  (NSIS is the updater's install mode). Two signed candidates are no longer
  reported as `ambiguous`.
- Linux: the AppImage is the updater payload; a `.deb` is not.

`scripts/gen-updater-manifest.py` additionally refuses:

- a payload whose file type cannot install on that platform (a macOS archive
  assigned to `windows-x86_64`);
- two platform keys that describe **different files with one sha512** — the
  exact v1.2.1 defect. One payload under two keys is allowed only when the URL
  is identical (a universal macOS build).

`scripts/publish-updater.sh --assets-dir <dir>` then re-hashes every published
payload, re-reads its `.sig`, and fails if the manifest disagrees with the bytes
in the release.

## Platform payloads

| Manifest key | Build lane | Updater payload |
|---|---|---|
| `darwin-aarch64` | `macos-latest` (arm64) | `darwin-aarch64-xPST.app.tar.gz` + `.sig` |
| `darwin-x86_64` | `macos-15-intel` (native x86_64) | `darwin-x86_64-xPST.app.tar.gz` + `.sig` |
| `windows-x86_64` | `windows-latest` | NSIS `…-setup.exe` + `.sig` (Tauri reuses the installer) |
| `linux-x86_64` | `ubuntu-22.04` | `…_amd64.AppImage` + `.sig` |

Both macOS architectures are built on native runners because PyInstaller cannot
cross-build the engine sidecar. GitHub retired `macos-13`; `macos-15-intel` is
the supported x86_64 label and is available until fall 2027, which is why the
manifest can honestly keep a `darwin-x86_64` key.

## Code signing: what is missing

Everything below is empty in this repository today, and the release lane
**fails a `v*` tag before building** when it is missing, rather than publishing
unsigned installers that look signed:

| Platform | Secrets required | What it fixes |
|---|---|---|
| macOS | `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` | Gatekeeper: Developer ID signature + notarisation + staple |
| Windows | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TRUSTED_SIGNING_ENDPOINT`, `AZURE_CODE_SIGNING_ACCOUNT`, `AZURE_CERTIFICATE_PROFILE` | SmartScreen: Authenticode via Azure Trusted Signing |

Accounts behind those credentials:

1. **Apple Developer Program membership** — $99/year. Provides the Developer ID
   Application certificate and notarisation.
2. **Azure Trusted Signing** (Basic) — $9.99/month, requires an Azure
   subscription and an identity-validated organisation or individual account.

When present, `tauri-release.yml`:

- imports the Apple certificate, lets Tauri sign and notarise during the build,
  then asserts `codesign --verify --deep --strict`, that the authority is a
  `Developer ID Application`, and that the DMG passes `xcrun stapler validate`;
- installs `trusted-signing-cli` and passes it to Tauri as
  `bundle.windows.signCommand`, so the installer is signed **during** bundling —
  before the updater artifact and its `.sig` are produced. Signing the `.exe`
  afterwards would change its bytes and break the updater signature;
- asserts every built `.exe`/`.msi` has `Get-AuthenticodeSignature -Status Valid`.

`dryrun-tauri-*` tags stay open on purpose: they exercise the build path without
requiring credentials and publish only to a draft release.

## How `latest.json` gets published

The endpoint is a **committed file**, not an upload target. GitHub Pages for this
repository runs with `build_type: legacy` and source `main:/`
(`gh api repos/TysAIs/xPST/pages`), so the served path is
`updates/latest.json` on `main`, next to `privacy/` and `terms/`.

`.github/workflows/publish-updater.yml`:

1. Triggers: `release: [published]`, `workflow_run` of `Tauri Shell Release`
   (completed), and `workflow_dispatch` with a `release_tag` input.
2. Downloads the release's updater artifacts into a scratch directory.
3. `scripts/select-updater-artifacts.py` resolves one signed artifact per target
   (`absent` / `unsigned` / `ambiguous` / `unattributable` are reported, never
   guessed).
4. `scripts/gen-updater-manifest.py` writes the manifest for the publishable
   platforms, with the `.sig` contents and a SHA-512 of the exact artifact bytes.
5. `scripts/publish-updater.sh --assets-dir …` validates the manifest against
   the downloaded bytes and stages it at `updates/latest.json`.
6. The job commits that file to `main` (Pages rebuilds from the branch) and
   attaches `latest.json` to the release. When `main` rejects the commit — the
   normal case, see below — the same validated manifest goes to
   `updater/latest-json` as a pull request, and the release asset is refreshed on
   that route too, so the two never disagree.

Manual run, including a build-only rehearsal:

```bash
gh workflow run publish-updater.yml --repo TysAIs/xPST -f release_tag=v1.2.3 -f dry_run=true
```

Invariants worth keeping:

- **The job never signs anything and needs no signing secret.** It publishes only
  artifacts that already carry a `.sig`, and never invents one.
- A release with no updater artifacts leaves the committed manifest untouched, so
  the endpoint keeps serving the last good manifest instead of going 404.
- **Direct commits to `main` are gated by branch protection.** The job
  force-updates `updater/latest-json` and opens/refreshes a pull request instead.
  A `GITHUB_TOKEN`-authored PR does not trigger `pull_request` workflows, so it
  is never green on its own; a maintainer merge is the gate. Do not add
  auto-merge: it would wait forever on checks no event will start.

## Release signing keypair

1. Generate a dedicated release keypair (do **not** reuse the E2E key):
   ```bash
   cargo tauri signer generate -w xpst-release.key --password ""
   ```
2. `pubkey` in `tauri.conf.json` = the base64 contents of `xpst-release.key.pub`.
3. Store the private key as `TAURI_SIGNING_PRIVATE_KEY` (plus
   `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` if a password is set). tauri-cli ≥ 2.x
   honors the `_KEY` env var; `TAURI_SIGNING_PRIVATE_KEY_PATH` alone is **not**
   honored (tauri-cli 2.11.4).
4. The local E2E key lives at `.tauri/xpst-updater-e2e.key` (gitignored). It
   signs only local E2E artifacts and must never sign a production release.

## What the E2E harness proves

`scripts/updater-e2e.sh` (run from repo root; exit codes documented in the
script header):

1. Builds v0.1.0 via a config overlay with updater artifacts.
2. Serves `latest.json` on `http://127.0.0.1:9555`.
3. Rebuilds as v0.2.0 via the same overlay, replaces artifact + manifest.
4. Launches the v0.1.0 bundle with `XPST_UPDATER_CHECK=1` (the unattended
   harness mode; the product check no longer needs it).
5. Asserts the v0.1.0 process checks, downloads, verifies the minisign
   signature, installs, restarts, and that a `started-0.2.0.txt` marker — written
   by `lib.rs` **inside the relaunched process** — appears in
   `/private/tmp/xpst-updater-e2e/`.

Known pitfalls baked into the script:

- Everything under `/private/tmp/...` (canonical path): the updater refuses to
  run when `current_exe()` crosses a symlink and `/tmp` is a symlink on macOS.
- Cleanup (`pkill`) is scoped to `/private/tmp/xpst-updater-e2e/` — never a
  generic `xPST.app` pattern, which would kill other agents' bundles.

## macOS Gatekeeper notes

- The updater verifies the Tauri minisign signature before installing; that is
  independent of Apple codesigning.
- A distributable app also needs a Developer ID signature and notarisation, or
  users must right-click → Open on first run. The extracted/relaunched bundle
  inherits the signature of the parent, so codesign+notarise happens in the
  release pipeline before the updater artifacts are published.
- The E2E loop runs unsigned apps launched directly from their binary path (no
  quarantine), so Gatekeeper does not intervene locally.
