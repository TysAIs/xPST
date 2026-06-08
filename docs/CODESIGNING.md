# Code Signing

xPST can be distributed unsigned for local development, but public desktop releases should be signed.

## Windows

Requirements:

- Windows SDK with `signtool.exe`
- Code signing certificate available in the certificate store, or a `.pfx` file

Certificate store signing:

```powershell
pwsh scripts/sign_windows.ps1 -Path dist/xPST.exe
```

PFX signing:

```powershell
$env:WINDOWS_CERTIFICATE_PATH="C:\secure\xpst-signing.pfx"
$env:WINDOWS_CERTIFICATE_PASSWORD="..."
pwsh scripts/sign_windows.ps1 -Path dist/xPST.exe
```

## macOS

Ad-hoc signing for local testing:

```bash
bash scripts/sign_macos.sh dist/xPST.app
```

Developer ID signing and notarization:

```bash
export MACOS_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_ID="apple-id@example.com"
export APPLE_TEAM_ID="TEAMID"
export APPLE_APP_PASSWORD="app-specific-password"
bash scripts/sign_macos.sh dist/xPST.app
```

## Release Rule

Unsigned artifacts are acceptable for development and CI smoke tests. Public GitHub Releases should include signed Windows/macOS artifacts, checksums, and SBOM files.

