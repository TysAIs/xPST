# The Tauri release lane (`.github/workflows/tauri-release.yml`)

This workflow builds the real, downloadable xPST desktop app: a Tauri 2 shell
with the Python engine sidecar, for every platform we claim to support. (The
only bundled media binary is the yt-dlp zipapp; ffmpeg/ffprobe are resolved at
runtime, not bundled.)

## Lanes

| lane | runner | target | bundles |
| --- | --- | --- | --- |
| macOS | `macos-latest` | `aarch64-apple-darwin` | `xPST_*_aarch64.dmg` (plus the `.app`) |
| Windows | `windows-latest` | `x86_64-pc-windows-msvc` | `xPST_*_x64-setup.exe` (NSIS), `.msi` |
| Linux | `ubuntu-22.04` | `x86_64-unknown-linux-gnu` | `.deb`, `.AppImage` |

Every lane runs the same ordered steps and fails closed at each one:

1. `python scripts/verify_release_version.py` - one version source.
2. `npm ci && npm run build` (Svelte UI).
3. engine sidecar via `scripts/build-engine.sh` (PyInstaller onedir), asserted to exist.
4. bundled media binary (the yt-dlp zipapp) via
   `scripts/fetch-media-binaries.sh` - **pinned and checksummed**
   ([docs/media-binary-provenance.md](media-binary-provenance.md)); the lane then
   runs `scripts/verify-media-binaries-provenance.sh` over the record, which
   fails on an `UNPINNED` entry or a bundled binary with no pinned row. A failed
   yt-dlp fetch is loud but non-fatal (the engine bundles the `yt_dlp` module).
5. `cargo tauri build --bundles <lane bundles>`.
6. assert the lane's installer really exists (no silent empty upload).
7. size budget on the built artifacts: the unpacked macOS `.app` must stay
   <= 130MB and the installer a stranger downloads <= 150MB, and the bundle must
   carry no ffmpeg/ffprobe binary while still containing the engine sidecar.
8. upload the per-target artifact (`if-no-files-found: error`).
9. boot proof for the lane: macOS runs the cold-boot harness
   (`scripts/measure_boot.py`, 3 runs, enforced against `perf/boot-budget.json`,
   which also proves the engine sidecar answered `/health` and was reaped on
   exit); Linux runs the real `.AppImage` under xvfb and spawns the
   `xpst-engine` sidecar; Windows silently installs the NSIS setup, launches it
   and requires the sidecar.
10. tag builds only: resolve + assert the release asset list, then publish.

## Triggers

- `push` of a **version tag** (`v*.*.*`): build, smoke, publish.
- `push` of a **documented dry-run tag** (`dryrun-tauri-*`): identical build,
  smoke and upload path, but the release it creates is a **draft** and the
  legacy `release.yml` lane ignores the tag entirely. Nothing is published and
  no watcher is notified.
- `workflow_dispatch`: build and smoke without publishing (used for lane work).

There is deliberately **no `paths:` filter** on the tag trigger. GitHub does not
evaluate path filters for tag pushes, and a filter here would be actively
misleading: the lane must run for every version tag, otherwise a tag can produce
no artifact at all while the workflow list shows nothing wrong.

## Proving the lane without publishing

```
# from the branch under test (tag must point at the commit you want to build)
git tag -f -a dryrun-tauri-1 -m "dry-run: prove the tag lane without publishing"
git push origin dryrun-tauri-1

gh run list --workflow tauri-release.yml --limit 3
gh run watch <run-id> --exit-status          # 3 lanes must be green

# the draft release and its assets: nothing here is public
gh release view dryrun-tauri-1 --repo TysAIs/xPST --json isDraft,assets

# clean up
gh release delete dryrun-tauri-1 --repo TysAIs/xPST --yes
git push origin :refs/tags/dryrun-tauri-1
```

A real version tag differs from this in exactly one way: `draft` is `false`.
Everything that produces and verifies the artifact - engine, pinned media
binaries, bundles, installer assertion, size gate, per-OS boot smoke, resolved
asset list - is identical.

## What a tag publishes

Each lane uploads its own workflow artifact (installers + its
`media-binaries-PROVENANCE-<target>.txt`), then a single `publish-release` job
runs once the three lanes are green:

1. downloads every lane's artifact;
2. asserts the file a stranger installs exists for **every** platform this
   workflow claims (`.dmg`, `.exe`/`.msi`, `.AppImage`/`.deb`) plus at least one
   provenance record, and fails naming the missing platform otherwise;
3. writes an aggregate `SHA256SUMS` whose entries use the asset names GitHub
   publishes, so `sha256sum -c SHA256SUMS` works on a downloaded set;
4. publishes with `fail_on_unmatched_files: true`.

One job owns the release on purpose. Before this, each of the three lanes
uploaded straight to the release and all three raced to create it; a tag build
died on `Creating new GitHub release ... 500 / Too many retries`. Collecting the
artifacts first also means the published asset set is asserted *before* anything
becomes visible.

Signing is optional and never changes *what* is built:

- macOS: notarization only if the Apple secrets exist. A set-but-empty Apple
  variable is unset before the build, because Tauri treats an empty
  `APPLE_SIGNING_IDENTITY` as an identity named `""` and every bundle then dies
  with `codesign: no identity found`. With no secrets the bundle is ad-hoc
  signed - a stranger gets a Gatekeeper warning, not a broken app.
- Windows: unsigned unless `WINDOWS_CERTIFICATE_BASE64`/`PASSWORD` exist.
- Updater artifacts (`*.app.tar.gz` + `.sig`) only exist when
  `TAURI_SIGNING_PRIVATE_KEY` is set; the resolve step tolerates their absence
  and fails only if a lane produced nothing at all.

## Known gaps (honest list)

- **Two lanes publish to the same tag.** `release.yml` (the legacy PySide6/QML
  app, `dist/xPST.app`, `xPST.dmg`, `xPST.exe`) still triggers on `v*.*.*` and
  uploads to the same GitHub release. That is why v1.1.0's published macOS
  `.dmg` is the old app and not this one. Until the legacy lane is retired or
  restricted, "the release asset" is ambiguous - check the asset name against
  the lane above, and use
  `scripts/e2e_install_from_artifact.sh --release <tag> --require-published`
  (which resolves a specific asset) rather than assuming.
- **No signing/notarization secrets are configured**, so released installers are
  unsigned.
- **The macOS yt-dlp pin is a single source.** If that asset disappears the lane
  reports it loudly but still succeeds (the engine bundles the `yt_dlp` module);
  an unverified fallback is never bundled (see the provenance doc).
