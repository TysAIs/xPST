# Signing and notarizing the macOS build

An unsigned macOS app does not fail to run, but every user who downloads it meets a Gatekeeper
warning and has to right-click → Open. Signing plus notarization removes that, and it is also what
makes the app safe to distribute at all: macOS refuses to launch a hardened-runtime app whose
nested binaries are unsigned or signed out of order.

This document is the whole procedure. Steps 1–3 are one-time and require a human with an Apple
Developer Program membership; steps 4+ are automated and can be run by anyone or by CI.

## One-time setup (human)

1. **Apple Developer Program membership** — <https://developer.apple.com/programs/> (paid, under the
   owner's name). A free Apple ID cannot issue a Developer ID certificate.
2. **Create the Developer ID Application certificate.** Generate a Certificate Signing Request on the
   Mac that will hold the key (Keychain Access → Certificate Assistant → *Request a Certificate from a
   Certificate Authority*, or `openssl req -new -newkey rsa:2048 -nodes -keyout developerid.key -out developerid.csr`),
   then upload it at <https://developer.apple.com/account/resources/certificates> → **+** → *Developer ID
   Application*. Download the `.cer` and import it into the login keychain on the same Mac.
   Confirm with:

   ```bash
   security find-identity -v -p codesigning
   # expect: 1) <HASH> "Developer ID Application: <NAME> (<TEAMID>)"
   ```

3. **Create an app-specific password** at <https://appleid.apple.com> → Sign-In & Security →
   App-Specific Passwords. Then store it in the keychain so no script ever sees it:

   ```bash
   xcrun notarytool store-credentials xpst-notary \
       --apple-id "<APPLE-ID>" --team-id "<TEAMID>"
   ```

   `xpst-notary` is the profile name this repository's scripts expect (`XPST_NOTARY_PROFILE`
   overrides it).

## CI secrets (human, once)

Repository → Settings → Secrets and variables → Actions. The workflow reads only these names:

| Secret | Value |
| --- | --- |
| `APPLE_CERTIFICATE` | base64 of the exported `.p12` (`base64 -i cert.p12 \| pbcopy`) |
| `APPLE_CERTIFICATE_PASSWORD` | the password chosen when exporting the `.p12` |
| `APPLE_SIGNING_IDENTITY` | `Developer ID Application: <NAME> (<TEAMID>)` |
| `APPLE_ID` | the Apple ID used for notarization |
| `APPLE_PASSWORD` | the app-specific password from step 3 |
| `APPLE_TEAM_ID` | the 10-character team identifier |

Until these exist, the workflow's signing step must skip cleanly and the release must be labelled
unsigned rather than failing or claiming a signature it does not have.

## Signing a build

```bash
scripts/macos-sign-and-notarize.sh --app src-tauri/target/release/bundle/macos/xPST.app \
    --identity "Developer ID Application: <NAME> (<TEAMID>)"
```

What it does, in order — the order is the part that is easy to get wrong:

1. Enumerates every Mach-O binary inside the bundle (the Tauri binary, the PyInstaller engine, its
   native modules, `ffmpeg`, `ffprobe`, `yt-dlp`, and any dylib) with `file -b` rather than by
   filename, and signs the deepest paths first. Nested binaries must be signed **before** the bundle
   that contains them, otherwise `codesign --verify --deep` fails.
2. Signs the nested binaries with `--options runtime --timestamp`, applying
   `src-tauri/entitlements-engine.plist` to binaries under `Resources/binaries/engine/`.
3. Signs the bundle itself with `--options runtime --timestamp`, applying
   `src-tauri/entitlements.plist`.
4. Verifies with `codesign --verify --deep --strict`, then submits the app (zipped with `ditto`, which
   preserves the extended attributes the notary service needs) via `notarytool submit --wait`, staples
   the ticket with `stapler staple`, and validates it.
5. Prints a one-line JSON summary.

It exits non-zero with an explicit reason when the identity is missing, when the bundle has no Mach-O
binaries, or when a required entitlements file is absent. It never prints credentials — `notarytool`
reads them from the keychain profile.

Use `--skip-notarize` for a signed-but-unnotarized local build. Say so when you hand such a build to
anyone: it is not the same thing as a notarized release.

## Why these entitlements

See the comments in `src-tauri/entitlements.plist` and `src-tauri/entitlements-engine.plist`. In short:
the webview and the bundled Python runtime both generate executable memory, and the app loads native
modules from its own Resources directory — hardened runtime blocks all of that without
`allow-jit`/`allow-unsigned-executable-memory`/`disable-library-validation`.

## Verifying a downloaded artifact

```bash
codesign --verify --deep --strict --verbose=2 /Applications/xPST.app
spctl -a -vvv -t exec /Applications/xPST.app     # "accepted, source=Notarized Developer ID"
xcrun stapler validate /Applications/xPST.app
```

`spctl` reporting `source=Notarized Developer ID` is the only proof of notarization. A build that has
not been notarized shows `source=Unnotarized Developer ID` even when the signature is valid.
