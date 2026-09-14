# Release verification

## Stranger-install E2E

`scripts/e2e_install_from_artifact.sh` exercises one desktop artifact from the
same perspective as a new user. It accepts a local `.dmg`, `.zip`, `.exe`,
`.AppImage` or extensionless executable, a GitHub release asset URL, or the
release tag itself:

```bash
# Published release: the platform installer is resolved from the GitHub API
scripts/e2e_install_from_artifact.sh --release v1.1.0

# URL: the release directory and macos-SHA256SUMS are inferred
scripts/e2e_install_from_artifact.sh \
  https://github.com/TysAIs/xPST/releases/download/v1.1.0/xPST.dmg

# Local file: provide the matching release checksum asset
scripts/e2e_install_from_artifact.sh \
  --checksums https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS \
  /path/to/xPST.dmg
```

`--release <tag>` resolves the installer a stranger is told to download for the
host platform (override with `--platform macos|windows|linux`, or name the asset
exactly with `--asset-name`). The resolution uses the release's own asset
listing, so the run cannot silently test a local build while claiming to test a
published one. When a release reports an asset size or digest, both are compared
against the bytes actually downloaded.

For a local asset without a checksum URL, pass both `--release-tag` and (when
needed) `--platform macos|windows|linux`; the default repository is
`TysAIs/xPST`. The checksum must contain an exact basename match; a renamed
local file can use `--checksum-asset xPST.dmg` (or another release basename).
The harness uses `curl -L --fail --retry 2` for URL inputs and compares the downloaded
SHA-256 before it mounts or launches anything.

The harness uses an automatically deleted `xpst-stranger-install-*` directory
under the operating system temporary directory. A macOS DMG is mounted
read-only with `hdiutil`, its `.app` is copied into that directory, and the
mount is detached. Zip files are extracted with path-traversal protection and
their entry permission bits restored (an `.app` whose executable bit was lost
cannot be launched); executables are copied into the same throwaway install
root. The launched process receives:

- `XPST_CONFIG_DIR=<temporary config directory>`;
- a temporary `HOME`/`USERPROFILE` and XDG/AppData directories;
- `XPST_NO_KEYRING=1`, so the test cannot read or write the tester's keychain.

The app's process group is terminated after the smoke. The test then requires:

1. an on-screen window measured by the macOS CoreGraphics window probe within
   the documented default boot budget of **15 seconds** (override with
   `--boot-budget-seconds`);
2. a loopback `/health` response of exactly HTTP 200 within **60 seconds**;
3. the healthy root URL to serve packaged HTML containing an xPST title marker;
4. a real running process: the launched PID is alive at the end of the boot
   poll and its command line can be read back, and — for a Tauri bundle — at
   least one real `xpst-engine` sidecar process was observed;
5. no real `xpst-engine` processes after shutdown;
6. the complete throwaway install/config root to be gone after cleanup, and the
   installed bundle, isolated config dir and isolated HOME to be removed.

The default health budget is intentionally separate from boot-to-visible:
Tauri's packaged sidecar has its own startup wait, while a visible window can
appear before the engine is healthy. Any failed assertion exits non-zero.

### Machine-readable evidence

The final line is one JSON object. `--evidence-out <path>` also writes the same
evidence to disk (indented) and records `evidence_written_to`, so a release
record can be attached instead of transcribed. The stable `evidence` object
carries the fields a release record needs:

| field | meaning |
| --- | --- |
| `artifact_name`, `artifact_source`, `release` | what was downloaded, and the resolved release asset metadata |
| `artifact_bytes`, `artifact_sha256`, `release_expected_sha256`, `checksum_ok`, `checksum_verdict_source` | the bytes, the release's own checksum verdict, and whether it came from `<platform>-SHA256SUMS` or the release API digest |
| `http_status`, `health_ok`, `health_url`, `packaged_ui_ok` | the engine/UI result |
| `boot_ok`, `boot_to_visible_seconds`, `window_assertion_required` | launch evidence |
| `running_process` | launched PID, its read-back command line, observed engine sidecars |
| `app_exit_code_during_poll`, `shutdown_exit_code` | exit codes |
| `cleanup_ok`, `uninstall_ok` | teardown |
| `gatekeeper` | signature/quarantine verdict and remediation |
| `checks`, `findings`, `failures` | per-check verdicts and the reasons |

`--keep-work` is a debugging escape hatch and explicitly reports that cleanup
was not asserted; it must not be used as release evidence. Likewise
`--no-require-visible-window` exists for headless CI only: it records
`window_assertion_required: false` and a finding, and its output is not a
clean-profile pass.

### Automated run

`.github/workflows/published-artifact-install-e2e.yml` runs this harness against
the assets a release actually published, on macOS, Linux and Windows, when a
release is published (and on manual dispatch with a tag). It never gates pull
requests. Each job uploads the evidence JSON and writes a Markdown summary of it
to the run's step summary via `scripts/e2e_evidence_summary.py`, which reports a
missing or failed evidence file as such instead of implying a pass.

The workflow passes `--require-published`, which fails the run unless the
artifact came from a published GitHub release: a local file is rejected outright,
so a smoke of an unbuilt working tree can never be reported as a stranger-install
pass. The evidence records `source_kind` and `published_required`.

Hosted runners have no logged-in window server, so the workflow also passes
`--no-require-visible-window`: that run is engine/process/cleanup evidence and
explicitly not the full clean-profile pass, which still has to be taken on a real
desktop. A red workflow run is a real finding about the published artifact.

### Stack detection

The stack detector reports `tauri` only when both
`Contents/Resources/ui/index.html` and
`Contents/Resources/binaries/engine/xpst-engine` are present. A bundle carrying
`Contents/Resources/xpst/desktop_app/qml/main.qml` or PySide6 is reported as
`legacy-pyside-qml`; it may render a native QML window, but it fails the
HTTP-engine and packaged-HTML assertions rather than being misreported as a
Tauri pass.

### Gatekeeper and quarantine

On macOS the harness records the exact output and exit status of:

```bash
codesign -dv --verbose=4 <installed-app>
spctl -a -vv <installed-app>
```

It only reads `com.apple.quarantine`; it never removes quarantine, disables
Gatekeeper, or changes signing. macOS release artifacts are not yet Developer ID
signed, so `spctl` rejecting an ad-hoc signed app is expected: the run records
`gatekeeper.ad_hoc_signed`, `gatekeeper.developer_id_signed: false` and the
user-facing remedy (Finder's contextual **Open**, or **Open Anyway** in System
Settings → Privacy & Security). A direct `exec` does not go through the
LaunchServices assessment, so the harness reports
`launchservices_assessment_exercised: false` rather than implying that
Gatekeeper acceptance was proven. `--require-gatekeeper-accepted` turns a
rejected `spctl` verdict into a failure, for the day a signed release exists.

## Published v1.1.0 record (measured 2026-09-14)

Run command:

```bash
scripts/e2e_install_from_artifact.sh --release v1.1.0 \
  --evidence-out /tmp/published-v1.1.0-macos-evidence.json
```

Measured result: **exit status 1**.

```text
resolved asset : xPST.dmg (126490269 bytes, release digest sha256:a03e6bb2…)
checksum       : PASS (matches macos-SHA256SUMS and the release asset digest)
sha256         : a03e6bb2a3f8f8e744597c619cfe4ee705885bd46bfac5af049407ff8c805ea2
installed stack: legacy-pyside-qml (no ui/index.html, no xpst-engine sidecar)
boot-to-visible: PASS (3.637 s <= 15 s, CoreGraphics on-screen window; 3.44-3.64 s across runs)
real process   : PASS (PID alive after boot; command line read back)
engine /health : FAIL (no loopback URL advertised; no HTTP 200 within 60 s)
packaged UI    : FAIL (no HTTP UI root)
xpst-engine after shutdown: 0
uninstall      : PASS (bundle, config dir, HOME profile and work dir removed)
XPST_CONFIG_DIR override  : NOT HONORED (config directory stayed empty)
```

The installed bundle identifies itself as version `1.1.0`, bundle identifier
`com.tysais.xpst`, executable `xPST`, and
`XPSTSourceCommit=28250c863b0ce03e66d04fb912584d49055df57a`. Its resource
markers are `Contents/Resources/xpst/desktop_app/qml/main.qml` and PySide6;
`Contents/Resources/ui/index.html` and
`Contents/Resources/binaries/engine/xpst-engine` are absent. The published
macOS asset is therefore the **legacy PySide6/QML app, not the Tauri build**.
A native window appeared, but this artifact cannot satisfy the Tauri engine/HTTP
UI contract. The Tauri shell lane has to publish the macOS installer for this
check to pass.

The isolated first-run profile created state under `HOME/.xpst` instead of the
requested `XPST_CONFIG_DIR`, including `config.yaml`, `analytics.db`,
`quotas.json`, `.state.lock`, `xpst-gui.lock`, and fallback credential files.
That is a stranger-facing config-isolation defect when an environment override
is expected. The harness removed the whole isolated profile afterward.

Signing evidence from the app copied out of the mounted DMG:

```text
$ codesign -dv --verbose=4 <installed xPST.app>
Executable=…/xPST.app/Contents/MacOS/xPST
Identifier=com.tysais.xpst
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20400 size=48456 flags=0x2(adhoc) hashes=1508+3 location=embedded
Signature=adhoc
TeamIdentifier=not set

$ spctl -a -vv <installed xPST.app>
<installed xPST.app>: rejected
```

The verdict is **ad-hoc code signature only** (no Team ID, not Developer ID);
Gatekeeper assessment was **rejected**. Notarization is **not proven**. The curl
download carried no `com.apple.quarantine` attribute, so this run did not hit a
quarantine launch block, and the direct-exec launch path does not exercise
LaunchServices. It did not disable Gatekeeper or strip quarantine. If a
browser-downloaded copy is blocked, the user-facing workaround is Finder's
contextual **Open**, or **Open Anyway** in System Settings → Privacy & Security,
subject to the user's own security decision.

## Published extensionless Linux asset (checked 2026-09-14)

The Linux lane publishes its binary as the extensionless asset `xPST`. The
harness now classifies it by magic bytes instead of its name, so it can be
resolved, verified and installed:

```bash
scripts/e2e_install_from_artifact.sh --release v1.1.0 --platform linux
```

Measured: the release API resolves `xPST` (254540568 bytes, release digest
`sha256:5cc28c52…`), the download matches `linux-SHA256SUMS`
(`sha256:5cc28c52a413e5b47994d3a21b8dd34e9827c199d28025bf348db9bb727d2dcd`), and
the payload sniffs as an ELF. Launching it on this macOS host then fails with
`Exec format error`, reported as `this published artifact is elf and this host
is macho; run the harness on elf's own platform to complete a real launch
smoke`. The launch smoke for this asset must therefore run on Linux.

## Published `.app` zip asset (checked 2026-09-14)

`v1.0.0` shipped the macOS bundle as a zip, `xPST-macos-arm64.zip`. It is **not listed** in that
release's `macos-SHA256SUMS`, so the harness verifies it against the asset digest the release API
reports and records `checksum_verdict_source: "release-api-digest"` (with a finding) instead of
pretending the lane checksum file covered it:

```bash
scripts/e2e_install_from_artifact.sh --release v1.0.0 --platform macos \
  --asset-name xPST-macos-arm64.zip
```

Measured: 95173091 bytes, `sha256:f5870f3c3598d54555cfe7cbeb9b022d435803261a018c94162a35fd84d2c07a`
(matching the release's own digest), extracted and copied as a bundle, **launched** with an on-screen
window in 0.83 s, a real running process, and a clean uninstall — exit status 1 only because the engine's
advertised loopback root (`http://127.0.0.1:62154/`) never answered `/health` with 200. Before the
executable-bit fix in this change, a zipped `.app` could never launch at all
(`Permission denied`), so this real published asset is the direct regression proof for that fix.

## Harness self-tests

`tests/test_e2e_install_from_artifact.py` runs the whole harness offline against
synthetic published artifacts: one whose executable serves `/health` itself, and
one shaped like the Tauri bundle (packaged UI plus a real `xpst-engine` sidecar
process). The Tauri-shaped case must reach `status: passed`, and the same
artifact with a wrong published checksum must fail with exit status 1 — so the
harness is proven able to both pass and fail for the right reasons.
