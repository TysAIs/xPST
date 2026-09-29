# Download and install xPST

This guide covers the standalone desktop artifacts published on the
[GitHub Releases page](https://github.com/TysAIs/xPST/releases). Do not construct
asset download URLs: open the release, expand **Assets**, and download the exact
filename shown there.

## Read this before downloading

The latest published release is **v1.2.1**. Its desktop artifacts are the
current Tauri shell installers (`.dmg`, NSIS `.exe`/`.msi`, `.deb`/`.AppImage`).
Older v1.1.x releases carried the retired PySide6/QML app; prefer the newest
release. The macOS build is ad-hoc signed and not notarized, so a browser
download will trigger a Gatekeeper approval on first launch — see
[Gatekeeper and an unsigned build](#gatekeeper-and-an-unsigned-build). Verify
the checksum before opening any downloaded artifact.

Automatic updates through the Tauri updater manifest (`latest.json`) are wired
for the `.app.tar.gz` channel; when in doubt, use the Releases page to install
a newer build manually.

## Choose the release asset

These are the v1.2.1 asset names verified on the Releases page:

| Operating system | Download this asset | Verify against | Notes |
|---|---|---|---|
| macOS (Apple Silicon) | `xPST_1.2.1_aarch64.dmg` | `SHA256SUMS` (aggregate) | Tauri desktop build. An Intel x86_64 macOS build is not published. |
| Windows (x64) | `xPST_1.2.1_x64-setup.exe` or `xPST_1.2.1_x64_en-US.msi` | `SHA256SUMS` (aggregate) | NSIS installer and Windows Installer package. |
| Linux (x64) | `xPST_1.2.1_amd64.deb` or `xPST_1.2.1_amd64.AppImage` | `SHA256SUMS` (aggregate) | Debian package or portable AppImage. |

Checksum manifests on the release page are named per lane, not per operating
system: the aggregate `SHA256SUMS`/`SHA512SUMS` cover the desktop installers,
and `python-SHA256SUMS`/`python-SHA512SUMS` cover the Python wheel and sdist
(`xpst-1.2.1-py3-none-any.whl`, `xpst-1.2.1.tar.gz`). The manifest is the
source of truth; never substitute a hash copied from a third-party page.

If you downloaded a Python artifact instead, verify it against
`python-SHA256SUMS` the same way as below, substituting the wheel or sdist
filename.

## Verify SHA256

From the directory containing the downloaded artifact, download the
aggregate `SHA256SUMS` from the same release. The commands below select the
named asset from that manifest, so they do not fail because other release
assets are not present locally.

### macOS

```bash
cd ~/Downloads
DMG_FILE='xPST_1.2.1_aarch64.dmg'   # the exact filename you downloaded
expected="$(awk -v f="$DMG_FILE" '$2 == f { print $1 }' SHA256SUMS)"
actual="$(shasum -a 256 "$DMG_FILE" | awk '{ print $1 }')"
if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
  printf '%s\n' "SHA256 mismatch — do not open $DMG_FILE." >&2
  exit 1
fi
printf 'SHA256 OK: %s\n' "$actual"
```

### Windows PowerShell

```powershell
$installerFile = "xPST_1.2.1_x64-setup.exe"   # the exact filename you downloaded
$line = Get-Content .\SHA256SUMS | Where-Object { $_ -match "(?s)\s$([regex]::Escape($installerFile))$" }
$expected = ($line -split '\s+')[0].ToLowerInvariant()
$actual = (Get-FileHash ".\$installerFile" -Algorithm SHA256).Hash.ToLowerInvariant()
if ([string]::IsNullOrEmpty($expected) -or $actual -ne $expected) {
    throw "SHA256 mismatch — do not run $installerFile."
}
"SHA256 OK: $actual"
```

### Linux

Use the same target-only selection so a partial download set does not fail the
check:

```bash
FILE='xPST_1.2.1_amd64.deb'   # or xPST_1.2.1_amd64.AppImage
expected="$(awk -v f="$FILE" '$2 == f { print $1 }' SHA256SUMS)"
actual="$(sha256sum "$FILE" | awk '{ print $1 }')"
if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
  printf '%s\n' "SHA256 mismatch — do not run $FILE." >&2
  exit 1
fi
printf 'SHA256 OK: %s\n' "$actual"
```

`sha256sum -c SHA256SUMS` works only when every file named by the manifest was
downloaded; the target-only commands above are safer.

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

## Install on macOS

1. Download `xPST_1.2.1_aarch64.dmg` and `SHA256SUMS` from the same release.
2. Verify the DMG using the macOS command above.
3. Double-click the DMG in Finder.
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

1. Download `xPST_1.2.1_x64-setup.exe` (NSIS installer) or
   `xPST_1.2.1_x64_en-US.msi` (Windows Installer) and `SHA256SUMS` from the
   same release.
2. Verify the file with the PowerShell command above.
3. Run the installer and follow its prompts. The `.exe` registers an
   uninstall entry; the `.msi` is managed by Windows Installer.

### Uninstall on Windows

1. Quit xPST.
2. Remove it from **Settings → Apps → Installed apps** (both the NSIS and MSI
   packages register there).

Uninstalling does not remove `%USERPROFILE%\.xpst`. See
[Configuration and state](#configuration-and-state) before deleting that data.

## Install on Linux

### Debian package (`xPST_1.2.1_amd64.deb`)

Verify the file against `SHA256SUMS`, then install it with `apt`:

```bash
sudo apt install ./xPST_1.2.1_amd64.deb
```

To uninstall without guessing the package name, read it from the same file:

```bash
PACKAGE_NAME="$(dpkg-deb -f ./xPST_1.2.1_amd64.deb Package)"
sudo apt remove "$PACKAGE_NAME"
```

### AppImage (`xPST_1.2.1_amd64.AppImage`)

Verify the file against `SHA256SUMS`, then:

```bash
chmod +x ./xPST_1.2.1_amd64.AppImage
./xPST_1.2.1_amd64.AppImage
```

On some distros an AppImage needs FUSE 2; if it refuses to start, extract it
with `--appimage-extract` and run the inner binary instead.

An AppImage is portable. Uninstalling it normally means quitting xPST and
deleting that AppImage file (plus any shortcut you created).

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
| TikTok as a destination | **Browser-native publisher (opt-in, unofficial) + Draft mode fallback** | Public posting without a developer-app audit is possible through the opt-in browser-native publisher: set `publish_mode: browser` (or `browser_only`) and xPST publishes through your logged-in TikTok Studio web session (labelled *browser-native publisher (unofficial)*, same same-origin pattern as Instagram's session mode; needs `pip install 'xpst[browser]'` + `python -m playwright install chromium`; PR t_a56a0fdf). Route preference: official Direct Post (if audited) > browser-native > inbox-draft. Draft mode (`draft_mode: auto`/`always`; PR #266) remains the no-browser fallback: uploads land in your TikTok inbox as drafts you finish in the app and report PENDING, never as published posts. Official public Direct Post still awaits TikTok's developer-app audit. |
| Threads | **Disabled / unauthenticated** | Threads is an opt-in integration and is currently disabled. It is not a live-verified destination. |
| Facebook Page | **Unauthenticated** | Page-scoped destination via a BYO Meta app (Facebook Login for Business). Implemented and unit-tested; no Meta app is configured in the current live environment. Personal profiles cannot publish. |
| Messenger | **Disabled / unauthenticated** | Messenger is an opt-in messaging/auto-reply integration and is currently disabled. It is not a video-posting destination. |
| `pip install xpst` | **Not yet** | The PyPI JSON endpoint currently returns HTTP 404, so the package is not published on PyPI. Use a GitHub release desktop asset or install from source instead. |

The repository also contains setup guides for integrations that are not in the
live-verified state above. Their existence documents code paths, not current
availability.
