# The Tauri release lane (`.github/workflows/tauri-release.yml`)

This workflow builds the real, downloadable xPST desktop app: a Tauri 2 shell
with the Python engine sidecar and the media binaries, for every platform we
claim to support.

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
4. media binaries via `scripts/fetch-media-binaries.sh` - **pinned and
   checksummed** ([docs/media-binary-provenance.md](media-binary-provenance.md));
   the lane then asserts the provenance record exists, covers ffmpeg/ffprobe/
   yt-dlp and contains no `UNPINNED` entry.
5. `cargo tauri build --bundles <lane bundles>`.
6. assert the lane's installer really exists (no silent empty upload).
7. installer size gate (<= 200MB on the file a stranger downloads).
8. upload the per-target artifact (`if-no-files-found: error`).
9. boot smoke for the lane: macOS app stays up 15s; Linux `.AppImage` runs under
   xvfb and spawns the `xpst-engine` sidecar; Windows silently installs the NSIS
   setup, launches it and requires the sidecar.
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

Per lane, the resolved asset list (installers produced by that lane plus
`media-binaries-PROVENANCE-<target>.txt`) is uploaded with
`fail_on_unmatched_files: true`. A tag that resolves no assets fails the lane
instead of publishing an empty release.

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
- **macOS has a single pinned media-binary source.** If that asset disappears
  the lane goes red with a named reason rather than falling back to an
  unverifiable build (see the provenance doc).
