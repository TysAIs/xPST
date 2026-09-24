# Download and install xPST

This guide covers the standalone desktop artifacts published on the
[GitHub Releases page](https://github.com/TysAIs/xPST/releases). Do not construct
asset download URLs: open the release, expand **Assets**, and download the exact
filename shown there.

## Read this before downloading

The latest published release currently visible is **v1.1.0**. Its desktop
artifacts were built from an older tag than the current `main` branch, and they
are the retired PySide6/QML app (a 1.1.0-era build), not the Tauri shell that
`main` builds today. Newer releases publish the Tauri installers (`.dmg`,
NSIS `.exe`/`.msi`, `.deb`/`.AppImage`) instead. Treat the
release as a published build, not as proof that the current branch is packaged.
The macOS signing and notarization status is **not proven**. Verify the checksum
before opening any downloaded artifact.

There is no proven automatic-update channel today. Use the Releases page to
install a newer build manually; do not rely on an in-app update check.

## Choose the release asset

These are the v1.1.0 asset names verified on the Releases page:

| Operating system | Download this asset | Download this checksum manifest | Notes |
|---|---|---|---|
| macOS | `xPST.dmg` | `macos-SHA256SUMS` | Apple Silicon/arm64 desktop build. An Intel x86_64 build is not published here. |
| Windows | `xPST.exe` | `windows-SHA256SUMS` | Standalone Windows desktop executable produced by the repository's PyInstaller spec; it is not an MSI or NSIS installer. |
| Linux | `xPST` | `linux-SHA256SUMS` | Standalone Linux desktop executable. v1.1.0 does **not** publish an AppImage or `.deb`. |

The release also contains an aggregate `SHA256SUMS`. The platform-specific
manifest is more convenient when you have downloaded only one platform asset.
The manifest is the source of truth; never substitute a hash copied from a
third-party page.

The commands use the platform-specific manifest to avoid false failures from
missing assets. If you downloaded the aggregate release file named
`SHA256SUMS` instead, substitute `SHA256SUMS` for the platform manifest in the
macOS or Windows target-only command. On Linux, `sha256sum -c SHA256SUMS`
is appropriate only when every file named by that aggregate manifest is present
locally; otherwise select the exact asset line as above.

## FFmpeg prerequisite

The standalone app opens without FFmpeg, and xPST's video processing and
encoding paths resolve an external `ffmpeg` executable. FFmpeg is **not**
bundled inside the app (it was 87 MB of a 192 MB bundle).

xPST looks for, in order: `XPST_FFMPEG_PATH` (an explicit override), an FFmpeg
already installed on your machine, and finally a copy it downloads and
checksum-verifies itself on first use. The desktop app does that download
automatically when no FFmpeg is present; from the CLI:

```bash
xpst media status      # which ffmpeg/ffprobe xPST will use, and from where
xpst media fetch       # download a verified static build into ~/.xpst/bin
```

Installing FFmpeg with your OS package manager remains the best option when you
want to control the build:

- macOS: `brew install ffmpeg`
- Windows: install an FFmpeg build and add its directory containing
  `ffmpeg.exe` to `PATH`
- Debian/Ubuntu: `sudo apt install ffmpeg`

If your distribution uses another package manager, use its FFmpeg package.

## Verify SHA256

From the directory containing the downloaded artifact, download the matching
platform manifest from the same release. The commands below select the named
asset from that manifest, so they do not fail because other release assets are
not present locally.

### macOS

```bash
cd ~/Downloads
expected="$(awk '$2 == "xPST.dmg" { print $1 }' macos-SHA256SUMS)"
actual="$(shasum -a 256 xPST.dmg | awk '{ print $1 }')"
if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
  printf '%s\n' 'SHA256 mismatch — do not open xPST.dmg.' >&2
  exit 1
fi
printf 'SHA256 OK: %s\n' "$actual"
```

### Windows PowerShell

```powershell
$line = Get-Content .\windows-SHA256SUMS | Where-Object { $_ -match '\s+xPST\.exe$' }
$expected = ($line -split '\s+')[0].ToLowerInvariant()
$actual = (Get-FileHash .\xPST.exe -Algorithm SHA256).Hash.ToLowerInvariant()
if ([string]::IsNullOrEmpty($expected) -or $actual -ne $expected) {
    throw "SHA256 mismatch — do not run xPST.exe."
}
"SHA256 OK: $actual"
```

### Linux

For the v1.1.0 Linux asset, the platform manifest contains the one executable,
so the normal checksum-file check is sufficient:

```bash
sha256sum -c linux-SHA256SUMS
```

If a later release's Linux manifest contains several assets and you downloaded
only one, use the same target-only pattern as the macOS command: extract the
line for the exact filename shown on that release page, hash that local file
with `sha256sum`, and compare the two values.

## Install on macOS

1. Download `xPST.dmg` and `macos-SHA256SUMS` from the same release.
2. Verify the DMG using the macOS command above.
3. Double-click `xPST.dmg` in Finder.
4. Drag `xPST.app` into the **Applications** folder.
5. Eject the mounted DMG.

### Gatekeeper and an unsigned build

The public macOS signing and notarization status is not proven. Gatekeeper checks
an app downloaded from the internet; an unsigned or unnotarized build may warn
that macOS cannot verify the developer or may prevent the first launch. Approve
this one application; do **not** disable Gatekeeper globally:

1. In Finder, open **Applications**.
2. Control-click (or right-click) `xPST.app` and choose **Open**.
3. Read the warning and choose **Open** in that dialog.
4. If macOS still blocks the launch, try opening the app once, then open
   **System Settings → Privacy & Security**, find the blocked xPST notice, click
   **Open Anyway**, and confirm **Open**.

Only use this approval after verifying the checksum and only for the copy you
intend to run. Do not use a global Gatekeeper bypass.

### Uninstall on macOS

1. Quit xPST.
2. In Finder, move `/Applications/xPST.app` to the Trash. If you ran the app
   from another folder, remove that copy instead.
3. Empty the Trash if you want the application binary removed immediately.

Removing the app does not remove your xPST data. See [Configuration and
state](#configuration-and-state) if you also want to remove credentials and
local history.

## Install on Windows

1. Download `xPST.exe` and `windows-SHA256SUMS` from the same release.
2. Verify the executable with the PowerShell command above.
3. Double-click `xPST.exe` to run it, or start it from PowerShell in its download
   directory:

   ```powershell
   .\xPST.exe
   ```

The v1.1.0 asset is a standalone executable, not a conventional installer.
It does not create an MSI/NSIS installation entry or a separate uninstaller.
If a future release publishes a real installer, use the exact installer asset
listed on that release and its own uninstall entry.

### Uninstall on Windows

1. Quit xPST.
2. Delete the downloaded `xPST.exe` (and any shortcut you created).
3. If a later release was installed through a Windows installer, remove that
   version from **Settings → Apps → Installed apps** instead.

Removing the executable does not remove `%USERPROFILE%\.xpst`. See
[Configuration and state](#configuration-and-state) before deleting that data.

## Install on Linux

### The currently published v1.1.0 binary

The verified v1.1.0 Linux desktop asset is the standalone file `xPST`. No
`.AppImage` or `.deb` is attached to that release, so there is no package
filename to guess and no package-manager install to perform:

```bash
chmod +x ./xPST
./xPST
```

Keep the executable wherever you want to launch it from, or create your own
desktop shortcut after confirming it works.

### AppImage releases

When a future Releases page lists an AppImage, download the exact filename
shown there and its Linux SHA256 manifest. Replace the variable value below
with that exact filename; do not invent a filename or URL:

```bash
APPIMAGE_FILE='paste-the-exact-AppImage-filename-from-the-release-page'
chmod +x "./$APPIMAGE_FILE"
"./$APPIMAGE_FILE"
```

An AppImage is portable. Uninstalling it normally means quitting xPST and
deleting that AppImage file (plus any shortcut you created).

### Debian package releases

When a future Releases page lists a `.deb`, download the exact filename shown
there and its Linux SHA256 manifest. Verify it, then install it with `apt`:

```bash
DEB_FILE='paste-the-exact-.deb-filename-from-the-release-page'
sudo apt install "./$DEB_FILE"
```

To uninstall without guessing the Debian package name, read the package name
from the same file and pass that value to `apt`:

```bash
PACKAGE_NAME="$(dpkg-deb -f "./$DEB_FILE" Package)"
sudo apt remove "$PACKAGE_NAME"
```

Removing a Debian package does not remove `~/.xpst`.

## Configuration and state

xPST keeps its local configuration and state below `~/.xpst/` on macOS and
Linux. On Windows, `~` means the current user's home directory, so the
corresponding path is `%USERPROFILE%\.xpst`. Depending on what you use, this
directory can contain:

- `config.yaml` — configuration;
- `state.json` — posting state;
- `analytics.db` — local analytics history; and
- `credentials/` — credential-store data and platform-specific session files.

The `CredentialStore` uses an OS keychain when explicitly enabled with
`XPST_USE_KEYRING=1`, or a Fernet-encrypted `.enc` file fallback by default.
The fallback refuses to write a credential as plaintext if the cryptography
dependency is unavailable. Some platform flows also write owner-only token,
cookie, or session files and configuration fields under this directory; those
files are not all encrypted by the repository. Treat the entire directory as
sensitive, do not share it, and do not assume that an `.enc` copy makes every
other file encrypted.

To remove all local configuration, credentials, state, and analytics, first
make any backup you need, then delete `~/.xpst` (on Windows,
`%USERPROFILE%\.xpst`) using your file manager or OS-appropriate command. This
is destructive and is separate from uninstalling the application.

## First launch and CLI verification

The standalone desktop assets launch the graphical app. A source installation
can be checked without posting anything by running the module help command
from the repository or installed environment:

```bash
python -m xpst --help
```

The CLI's non-mutating health check is also available after a source install:

```bash
python -m xpst health --json
```

A successful help command proves that the Python package is importable; it does
not authenticate accounts or prove that a social-platform upload will work.
See the capability table below for the current verified scope.

## Capability truth table

This is an honest status snapshot, not a guarantee that a platform will keep
its API or session behavior unchanged. Platform authentication and health are
account-dependent; use the non-mutating health check after your own setup.

| Capability | Status | What that means today |
|---|---|---|
| YouTube | **Live-verified** | Current account authentication and live health were verified. Publishing still requires your own Google OAuth project/account and remains subject to YouTube limits. |
| X | **Live-verified** | Current account authentication and live health were verified. The path depends on your own account and chosen API/session method and remains subject to X enforcement and limits. |
| Instagram | **Live-verified** | Current account authentication and live health were verified. The official Graph API path requires your own Meta app and an eligible Creator/Business account; session-based modes have separate risks. |
| TikTok as a source | **Source-only (live-verified)** | TikTok source fetching through the downloader/cookie path was live-checked. Use it to source content for other destinations. |
| TikTok as a destination | **Not available yet** | Destination publishing awaits external TikTok developer review and approved app credentials. Do not present TikTok destination publishing as ready. |
| Threads | **Disabled / unauthenticated** | Threads is an opt-in integration and is currently disabled. It is not a live-verified destination. |
| Facebook Page | **Unauthenticated** | Page-scoped destination via a BYO Meta app (Facebook Login for Business). Implemented and unit-tested; no Meta app is configured in the current live environment. Personal profiles cannot publish. |
| Messenger | **Disabled / unauthenticated** | Messenger is an opt-in messaging/auto-reply integration and is currently disabled. It is not a video-posting destination. |
| `pip install xpst` | **Not yet** | The PyPI JSON endpoint currently returns HTTP 404, so the package is not published on PyPI. Use a GitHub release desktop asset or install from source instead. |

The repository also contains setup guides for integrations that are not in the
live-verified state above. Their existence documents code paths, not current
availability.
