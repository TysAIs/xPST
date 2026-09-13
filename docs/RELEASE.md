# Release verification

## Stranger-install E2E

`scripts/e2e_install_from_artifact.sh` exercises one desktop artifact from the
same perspective as a new user. It accepts a local `.dmg`, `.zip`, `.exe`, or
`.AppImage`, or a GitHub release asset URL:

```bash
# URL: the release directory and macos-SHA256SUMS are inferred
scripts/e2e_install_from_artifact.sh \
  https://github.com/TysAIs/xPST/releases/download/v1.1.0/xPST.dmg

# Local file: provide the matching release checksum asset
scripts/e2e_install_from_artifact.sh \
  --checksums https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS \
  /path/to/xPST.dmg
```

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

## Published v1.1.0 record

Run command:

```bash
scripts/e2e_install_from_artifact.sh \
  https://github.com/TysAIs/xPST/releases/download/v1.1.0/xPST.dmg
```

Release checksum source:

```text
https://github.com/TysAIs/xPST/releases/download/v1.1.0/macos-SHA256SUMS
```

The macOS run was executed against the URL above. It returned exit status 1,
with this measured result:

```text
checksum: PASS
asset bytes: 126968730
sha256: 1ceac2ba9568e6bd4283d38c45728674dddefafe0fb06da6be7bc7b87234efa1
boot-to-visible: PASS (5.432298 seconds <= 15 seconds)
engine /health: FAIL (no loopback URL observed; no HTTP 200 within 60 seconds)
packaged HTTP UI: FAIL (no HTTP UI root)
xpst-engine processes after shutdown: 0
throwaway install/config cleanup: PASS (work directory removed)
XPST_CONFIG_DIR override: NOT HONORED (config directory stayed empty)
```

The installed bundle identifies itself as version `1.1.0`, bundle identifier
`com.tysais.xpst`, executable `xPST`, and
`XPSTSourceCommit=d42e8602d08137ce87da4768e9da08b3426aa7d6`. Its resource
markers are `Contents/Resources/xpst/desktop_app/qml/main.qml` and PySide6;
`Contents/Resources/ui/index.html` and
`Contents/Resources/binaries/engine/xpst-engine` are absent. The published
asset is therefore the **legacy PySide6/QML app, not the Tauri build**. A native
window appeared, but this artifact cannot satisfy the Tauri engine/HTTP UI
contract.

The isolated first-run profile created state under `HOME/.xpst` instead of the
requested `XPST_CONFIG_DIR`, including `config.yaml`, `analytics.db`,
`quotas.json`, `.state.lock`, `xpst-gui.lock`, and fallback credential files.
That is a stranger-facing config-isolation defect when an environment override
is expected. The harness removed the whole isolated profile afterward.

Signing evidence from the app copied out of the mounted DMG:

```text
$ codesign -dv --verbose=4 /Volumes/xPST/xPST.app
Executable=/Volumes/xPST/xPST.app/Contents/MacOS/xPST
Identifier=com.tysais.xpst
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20400 size=48232 flags=0x2(adhoc) hashes=1501+3 location=embedded
VersionPlatform=1
VersionMin=720896
VersionSDK=786688
Hash type=sha256 size=32
CandidateCDHash sha256=b3bf6010281401c999ba588b837cb270ddb1be53
CandidateCDHashFull sha256=b3bf6010281401c999ba588b837cb270ddb1be53662d9fb930016f4459ef3465
Hash choices=sha256
CMSDigest=b3bf6010281401c999ba588b837cb270ddb1be53662d9fb930016f4459ef3465
CMSDigestType=2
Executable Segment base=0
Executable Segment limit=49152
Executable Segment flags=0x1
Page size=16384
CDHash=b3bf6010281401c999ba588b837cb270ddb1be53
Signature=adhoc
Info.plist entries=13
TeamIdentifier=not set
Sealed Resources version=2 rules=13 files=3393
Internal requirements count=0 size=12
Total signatures=1
Chosen signature=1

$ spctl -a -vv /Volumes/xPST/xPST.app
/Volumes/xPST/xPST.app: rejected
```

The verdict is **ad-hoc code signature only** (no Team ID,
not Developer ID); Gatekeeper assessment was **rejected**. Notarization is **not
proven**; the release's `macos-RELEASE_EVIDENCE.json` has no signing or
notarization fields. The curl download had no `com.apple.quarantine` attribute,
so this run did not hit a quarantine launch block. It did not disable
Gatekeeper or strip quarantine. If a browser-downloaded copy is blocked, the
user-facing workaround is Finder's contextual **Open**, or **Open Anyway** in
System Settings → Privacy & Security, subject to the user's security decision.
