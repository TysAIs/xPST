# macOS Hermes Validation

Use this handoff to get xPST to 100% on macOS before moving to Linux.

## Goal

Prove that the same release branch works on macOS as a Python package, CLI, MCP server, desktop/QML app, and packaged `.app` bundle.

## Setup

Run on a clean macOS Hermes worker with:

- macOS 13 or newer
- Python 3.10, 3.11, 3.12, and 3.13 available for matrix testing if possible
- Xcode Command Line Tools
- Homebrew
- FFmpeg

```bash
xcode-select --install || true
brew install ffmpeg
git clone https://github.com/TysAIs/xPST.git
cd xPST
git checkout codex/windows-readiness-audit
```

## Primary Validation

Run the repo-native validation script:

```bash
bash scripts/verify_macos.sh
```

Expected result:

- `pytest` passes
- `ruff check src tests` passes
- `mypy src/xpst` passes
- `pip-audit` reports no known vulnerabilities
- `python -m build` creates wheel and sdist
- `xpst version --json` emits valid JSON
- `xpst health --json` emits valid JSON and clearly reports missing credentials instead of crashing
- QML smoke test prints `root_objects 1`
- PyInstaller creates `dist/xPST.app`
- `dist/SHA256SUMS` and `dist/xpst-sbom.cdx.json` exist

## Manual Desktop Smoke

After the script:

```bash
open dist/xPST.app
```

Verify:

- App opens without a Python traceback.
- Sidebar navigation works.
- About page links point to `https://github.com/TysAIs/xPST`.
- Settings page opens.
- Connect page displays platform auth status without crashing.
- Quit from the menu/tray works.

## Signing And Notarization

Ad-hoc signing:

```bash
bash scripts/sign_macos.sh dist/xPST.app
```

Developer ID signing:

```bash
export MACOS_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_ID="apple-id@example.com"
export APPLE_TEAM_ID="TEAMID"
export APPLE_APP_PASSWORD="app-specific-password"
bash scripts/sign_macos.sh dist/xPST.app
```

Expected result:

- `.app` verifies with `codesign`
- `.dmg` is created
- If Apple credentials are set, the DMG is notarized and stapled

## Account-Owner Auth Tests

These require Owner-owned credentials and should be run manually:

```bash
xpst auth youtube
xpst auth instagram
xpst auth x
xpst health --json
```

Expected result:

- YouTube reports authenticated when `client_secrets.json` and OAuth flow are complete.
- Instagram reports session valid after login.
- X reports cookies/session valid after auth.
- TikTok/source health reports available when username/cookies are configured.

## Return To Windows Agent

Send back:

- macOS version and CPU architecture
- Python version used
- Full pass/fail summary
- Any failing command output
- `dist` artifact list
- Whether the app opened manually
- Whether signing/notarization was ad-hoc or Developer ID

