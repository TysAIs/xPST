# Release verification

## Stranger-install E2E

`scripts/e2e_install_from_artifact.sh` exercises one desktop artifact from the
same perspective as a new user. It accepts a local `.dmg`, `.zip`, `.exe`, or
`.AppImage`, a GitHub release asset URL, or a **published release tag**:

```bash
# Published release: resolves the platform asset and its SHA256SUMS from the
# GitHub release API and refuses to proceed unless it is a real published asset
scripts/e2e_install_from_artifact.sh \
  --release v1.1.0 --require-published \
  --evidence-out evidence/macos.json

# URL: the release directory and macos-SHA256SUMS are inferred
scripts/e2e_install_from_artifact.sh \
  https://github.com/TysAIs/xPST/releases/download/v1.1.0/xPST.dmg

# Local file: provide the matching release checksum asset
scripts/e2e_install_from_artifact.sh \
  --checksums https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS \
  /path/to/xPST.dmg
```

`--release <tag>` asks the GitHub release API for the tag's assets and scores
them per platform (macOS `.dmg`, Windows `.exe`/`.msi`, Linux
`.AppImage`/`.deb`/`.rpm` or the extensionless PyInstaller binary the Linux lane
publishes), so the harness always tests something that is actually published
and records the asset id, byte size and creation time it resolved. `--asset`
overrides that choice. `--require-published` turns "tested a local build" into
a failure. `--evidence-out <path>` writes the full machine-readable summary to
disk (it is printed to stdout too). Extensionless artifacts are identified by
magic bytes, not only by filename.

For a local asset without a checksum URL, pass both `--release-tag` and (when
needed) `--platform macos|windows|linux`; the default repository is
`TysAIs/xPST`. The checksum must contain an exact basename match; a renamed
local file can use `--checksum-asset xPST.dmg` (or another release basename).
The harness uses `curl -L --fail --retry 2` for URL inputs and compares the downloaded
SHA-256 before it mounts or launches anything.

The harness uses an automatically deleted `xpst-stranger-install-*` directory
under the operating system temporary directory. A macOS DMG is mounted
read-only with `hdiutil`, its `.app` is copied into that directory, and the
mount is detached. Zip files are extracted with path-traversal protection;
executables are copied into the same throwaway install root. The launched
process receives:

- `XPST_CONFIG_DIR=<temporary config directory>`;
- a temporary `HOME`/`USERPROFILE` and XDG/AppData directories;
- `XPST_NO_KEYRING=1`, so the test cannot read or write the tester's keychain.

The app's process group is terminated after the smoke. The test then requires:

1. an on-screen window measured by the macOS CoreGraphics window probe within
   the documented default boot budget of **15 seconds** (override with
   `--boot-budget-seconds`);
2. a loopback `/health` response of exactly HTTP 200 within **60 seconds**;
3. the healthy root URL to serve packaged HTML containing an xPST title marker;
4. no real `xpst-engine` processes after shutdown; and
5. the complete throwaway install/config root to be gone after cleanup.

The default health budget is intentionally separate from boot-to-visible:
Tauri's packaged sidecar has its own startup wait, while a visible window can
appear before the engine is healthy. The final line is one JSON object with
checksum, stack/resource markers, boot, health, UI, signing, process, cleanup,
findings, and failures. Any failed assertion exits non-zero. `--keep-work` is
a debugging escape hatch and explicitly reports that cleanup was not asserted;
it must not be used as release evidence.

The stack detector reports `tauri` only when both
`Contents/Resources/ui/index.html` and
`Contents/Resources/binaries/engine/xpst-engine` are present. A bundle carrying
`Contents/Resources/xpst/desktop_app/qml/main.qml` or PySide6 is reported as
`legacy-pyside-qml`; it may render a native QML window, but it fails the
HTTP-engine and packaged-HTML assertions rather than being misreported as a
Tauri pass.

On macOS the harness records the exact output and exit status of:

```bash
codesign -dv --verbose=4 <installed-app>
spctl -a -vv <installed-app>
```

It only reads `com.apple.quarantine`; it never removes quarantine, disables
Gatekeeper, or changes signing. An ad-hoc signature is reported separately
from Developer ID signing and notarization. If Finder/Gatekeeper blocks a
quarantined app, the run records the attribute and the user-facing remedy is
to use Finder's contextual **Open** approval (or **Open Anyway** in System
Settings → Privacy & Security), subject to the user's own security decision.

## Published v1.1.0 record (macOS arm64)

Run command (published-asset resolution, not a hand-copied URL):

```bash
scripts/e2e_install_from_artifact.sh \
  --release v1.1.0 --require-published \
  --evidence-out evidence/macos.json
```

The harness resolved `xPST.dmg` (asset id `562855127`, created
`2026-09-14T07:32:46Z`) out of 34 published assets, against
`https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS`.
It returned exit status **1**, with this measured result:

```text
checksum: PASS
asset bytes: 126490269
sha256: a03e6bb2a3f8f8e744597c619cfe4ee705885bd46bfac5af049407ff8c805ea2
boot-to-visible: PASS (3.714 seconds <= 15 seconds, CoreGraphics window count)
engine /health: FAIL (no loopback URL observed; no HTTP 200 within 60 seconds)
http_status: null
packaged HTTP UI: FAIL (no HTTP UI root)
process: pid alive at probe; shutdown exit code -15
xpst-engine processes after shutdown: 0
throwaway install/config cleanup: PASS (work directory removed, uninstalled)
XPST_CONFIG_DIR override: NOT HONORED (config directory stayed empty)
```

The installed bundle identifies itself as version `1.1.0`, bundle identifier
`com.tysais.xpst`, executable `xPST`, and
`XPSTSourceCommit=28250c863b0ce03e66d04fb912584d49055df57a` (current `main`).
Its resource markers are `Contents/Resources/xpst/desktop_app/qml/main.qml` and
PySide6; `Contents/Resources/ui/index.html` and
`Contents/Resources/binaries/engine/xpst-engine` are absent. The published
macOS asset is therefore the **legacy PySide6/QML app, not the Tauri build**. A
native window appeared and the process stayed up, but this artifact cannot
serve `/health` or the packaged HTTP UI, so the Tauri engine contract is
**unproven for the published macOS artifact**.

Why: the Tauri shell lane (`Tauri Shell Release` on tag `v1.1.0`) has failed on
every run so far — a truncated download from a third-party media mirror
(`curl: (18) Transferred a partial file` while fetching ffprobe) aborts the
build before any `.dmg` is produced, so no Tauri macOS installer has ever been
published. `scripts/fetch-media-binaries.sh` now retries that class of failure
(`--retry-all-errors`); until the lane goes green and a Tauri `.dmg` is
published, this E2E is expected to fail the health assertion on macOS and that
failure is the honest signal, not a harness bug.

The isolated first-run profile created state under `HOME/.xpst` instead of the
requested `XPST_CONFIG_DIR`, including `config.yaml`, `analytics.db`,
`quotas.json`, `.state.lock`, `xpst-gui.lock`, and fallback credential files.
That is a stranger-facing config-isolation defect when an environment override
is expected. The harness removed the whole isolated profile afterward.

Signing evidence from the app copied out of the mounted DMG:

```text
$ codesign -dv --verbose=4 <installed>/xPST.app
Executable=.../xPST.app/Contents/MacOS/xPST
Identifier=com.tysais.xpst
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20400 size=48456 flags=0x2(adhoc) hashes=1508+3 location=embedded
Signature=adhoc
TeamIdentifier=not set
Sealed Resources version=2 rules=13 files=3394

$ spctl -a -vv <installed>/xPST.app
<installed>/xPST.app: rejected
```

The verdict is **ad-hoc code signature only** (no Team ID,
not Developer ID); Gatekeeper assessment was **rejected**. Notarization is **not
proven**; the release's `macos-RELEASE_EVIDENCE.json` has no signing or
notarization fields. The curl download carried no `com.apple.quarantine`
attribute, so no Gatekeeper prompt appeared in this run; the evidence JSON
records the attribute, `spctl` acceptance, ad-hoc status and whether a launch
was actually blocked, plus the user-facing remedy. The harness never removes
quarantine, disables Gatekeeper, or changes signing. A browser-downloaded copy
does carry quarantine and shows the standard "unidentified developer" prompt;
the user-facing workaround is Finder's contextual **Open**, or **Open Anyway**
in System Settings → Privacy & Security, subject to the user's own security
decision.

## Automated run

`.github/workflows/published-artifact-install-e2e.yml` runs this harness against
the assets a release actually published, on macOS, Linux and Windows, when a
release is published (and on manual dispatch with a tag). It passes
`--require-published` so a local build can never substitute for a published
asset, uploads the evidence JSON per platform, and writes a Markdown summary to
the run's step summary. It does not gate pull requests. A red run is a real
finding about the published artifact.

`--require-visible auto` (the default) asserts an on-screen window only when a
GUI session exists (`launchctl managername` reports `Aqua` on macOS), so a
headless CI runner records the visibility probe instead of failing a healthy
bundle. `--require-visible yes` forces the assertion; the evidence records
`boot.visible_required` either way.

`tests/test_e2e_install_from_artifact.py` covers the asset ranking, magic-byte
classification, checksum parsing/verification, install safety, the health/UI
probe against a real loopback server, Gatekeeper reporting, evidence writing and
the summary renderer.
