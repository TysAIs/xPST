# Release verification

xPST release checks have two separate scopes:

- `scripts/clean_install_smoke.py` installs Python wheels/sdists into fresh virtual environments and exercises CLI JSON commands. It does **not** inspect a Tauri app, DMG, updater archive, installer contents, file permissions, signing, or notarization.
- `scripts/verify_desktop_package.py` checks repository packaging inputs when called without `--artifact`, and checks a built desktop artifact when an artifact is supplied.

The artifact verifier is the release gate for a Tauri bundle. It does not modify the input app, archive, installer, or mounted DMG.

## Local usage

From the repository root:

```bash
# Verify the source version surfaces, including Cargo.lock.
python scripts/verify_release_version.py

# Verify an unpacked app. Evidence is written outside the app bundle.
python scripts/verify_desktop_package.py \
  --artifact src-tauri/target/release/bundle/macos/xPST.app \
  --evidence-output release/xPST.app.RELEASE_EVIDENCE.json \
  --json

# Verify a DMG or an archive containing an .app.
python scripts/verify_desktop_package.py \
  --artifact src-tauri/target/release/bundle/dmg/xPST_1.1.0_aarch64.dmg \
  --root . \
  --evidence-output release/xPST.dmg.RELEASE_EVIDENCE.json
```

`--expected-version` (also accepted as `--expected`) is available when the expected release is supplied by a tag or another release coordinator. Without it, the verifier uses `pyproject.toml` as the expected version and calls `scripts/verify_release_version.py` to compare Python runtime, Cargo.toml, Cargo.lock, Tauri, UI, and UI lockfile versions.

Supported inputs are an unpacked `.app` or `Contents` directory, `.zip`, `.dmg`, common tar archives (including Tauri `.app.tar.gz` updater archives), `.pkg`, `.appimage`, and `.exe`/`.msi` when `7z`/`7zz` is installed. An opaque installer without an available safe extractor fails closed; unpack it and run the verifier again rather than treating a hash as a content check.

## Checks performed

For every materialized Tauri bundle, the verifier asserts:

- `Contents/Resources/ui/index.html` exists.
- `Contents/Resources/binaries/engine/xpst-engine` exists and is executable.
- FFmpeg resources `Contents/Resources/binaries/ffmpeg/ffmpeg` and `ffprobe` exist and are executable (with `.exe` alternatives for unpacked Windows bundles).
- The yt-dlp resource `Contents/Resources/binaries/ytdlp/yt-dlp` exists and is executable (with a `.exe` alternative).
- `Contents/Info.plist` has one consistent bundle version matching all repository release version sources.
- SHA-256 and SHA-512 hashes are recorded for the original artifact. For a directory app, hashes cover a canonical path/mode/content manifest.
- No bundle file is group/world writable or setuid/setgid, and required executables have an executable bit. Symlinks are rejected.
- Bundle text and printable strings in binary files contain no absolute `/Users/...`, `/home/...`, or Windows user-home path, email address, phone number, or high-confidence API key/token pattern. Findings contain only category and relative path; matched values are never written to evidence.
- On macOS, `codesign --verify --deep --strict` and `spctl --assess --type execute --verbose=4` are run when available. These checks are informational unless a release workflow explicitly makes them required.

## Evidence format

Each artifact run writes `RELEASE_EVIDENCE.json` with:

- `artifact`: filename, type, and size;
- `hashes.sha256` and `hashes.sha512`;
- `version`, `required_resources`, `permissions`, and `privacy` result blocks;
- an explicit signing block and flat compatibility fields:

```json
{
  "quality_checks": {
    "run_by_release_script": true,
    "artifact_verifier": "scripts/verify_desktop_package.py"
  },
  "signed": false,
  "notarized": "unknown",
  "verified_by": "unknown",
  "attestation": {
    "signed": false,
    "notarized": "unknown",
    "verified_by": "unknown"
  }
}
```

The values remain `false`/`unknown` until actual `codesign`/`spctl` output proves otherwise. An ad-hoc signature may make `signed` true and records `signing.signature_type` as `adhoc`; it is not Developer ID signing and does not prove notarization. A rejected Gatekeeper assessment records `notarized: false`. Tool output is redacted for local home paths before it is placed in evidence.

A failed content/privacy/version check still writes evidence, so CI can upload the failure report. Do not publish that report as a passing release claim.

## CI workflow

`.github/workflows/verify-release-artifacts.yml` runs on `release: published` and `workflow_dispatch` (with a release tag input). It checks out the matching tag, downloads every published desktop installer/archive, runs the verifier once per artifact, runs the existing clean-install smoke for published wheel/sdist assets, and uploads the per-artifact evidence plus a hash manifest. It intentionally does not edit `.github/workflows/tauri-release.yml`.

The older `scripts/release_artifacts.py` manifest now also contains explicit `signed: false`, `notarized: "unknown"`, and `verified_by: "unknown"` fields. Its `quality_checks.run_by_release_script` remains `false` when called with `--skip-checks`; that flag must not be used as evidence that checks ran.

## Current local limitation

The installed app under the local checkout's `dist/xPST.app` path has all required resources, matching version `1.1.0`, valid SHA-256/SHA-512 results, and sane permissions. Its local run is expected to fail the privacy gate because the existing frozen engine contains a source checkout home path. It is ad-hoc signed (`codesign` verifies) and rejected by local `spctl`, so it is not a notarization proof. The verifier writes evidence to the requested output path without changing the installed app.
