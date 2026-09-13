# xPST release and updater procedure

This is the release-owner runbook for the Tauri shell and its updater channel.
The updater endpoint committed in `src-tauri/tauri.conf.json` is:

```text
https://tysais.github.io/xPST/updates/latest.json
```

The repository now has a reproducible `publish-updater.yml` Pages deployment.
It does not require a committed `gh-pages` branch: GitHub Pages is deployed from
the workflow artifact. The first deployment still requires the repository owner
to set **Settings → Pages → Source → GitHub Actions**. Do not describe the
endpoint as live until the post-release `curl` check below returns `200`.

## Owner-only signing and Pages setup

The public key already committed in `src-tauri/tauri.conf.json` is the trust
root for installed applications. Do not rotate it or replace it with a test
key. Maintainer must configure these GitHub Actions secrets before publishing a
release:

- `TAURI_SIGNING_PRIVATE_KEY` — the existing Tauri private key, stored as a
  secret, never committed or printed.
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` — the key password, including an empty
  value if the key has no password.

`bundle.createUpdaterArtifacts` is intentionally `true`. A release build fails
closed when the private key is absent; it must never silently produce an
unsigned updater artifact. The local E2E key, if created, is unrelated and must
never be used for a release.

## Normal release

1. Update all release version surfaces and run the version gate:

   ```bash
   python scripts/verify_release_version.py
   ```

2. Push a semver tag from the commit intended for release. The tag workflow
   runs four Tauri lanes:

   ```bash
   git tag v1.2.3
   git push origin v1.2.3
   ```

   The lanes produce and sign one updater payload per platform:

   - `darwin-aarch64` — macOS `.app.tar.gz`
   - `darwin-x86_64` — Intel macOS `.app.tar.gz`
   - `windows-x86_64` — Windows NSIS setup `.exe`
   - `linux-x86_64` — Linux `.AppImage`

   The workflow prefixes each GitHub Release asset with its platform key and
   uploads its `.sig` sidecar beside it. The payload bytes are not changed by
   the prefix; the signature remains valid.

3. After the release is published, `publish-updater.yml` runs automatically.
   It downloads the release assets, requires exactly one artifact and a
   non-empty `.sig` for each of the four platform keys, generates
   `latest.json`, validates it, deploys `updates/latest.json` to GitHub Pages,
   and keeps a copy of `latest.json` on the GitHub Release. It is also
   rerunnable for an existing release:

   ```bash
   gh workflow run publish-updater.yml --repo TysAIs/xPST -f release_tag=v1.2.3
   ```

4. Verify the live endpoint and the release assets. Record the actual output
   with the release evidence; a workflow success alone is not publication
   proof:

   ```bash
   curl -sS -o /tmp/xpst-latest.json \
     -w 'HTTP %{http_code}\n' \
     https://tysais.github.io/xPST/updates/latest.json
   python -m json.tool /tmp/xpst-latest.json >/dev/null
   gh release view v1.2.3 --repo TysAIs/xPST --json assets
   ```

   For every platform, download the URL from the manifest and compare its
   bytes to the manifest's `sha512` value:

   ```bash
   python - <<'PY'
   import hashlib, json, urllib.request

   manifest = json.load(open('/tmp/xpst-latest.json', encoding='utf-8'))
   for platform, entry in manifest['platforms'].items():
       payload = urllib.request.urlopen(entry['url']).read()
       digest = hashlib.sha512(payload).hexdigest()
       assert digest == entry['sha512'], platform
       print(platform, 'SHA512 OK', digest)
   PY
   ```

   The Tauri updater uses `signature` plus the public key in the app config to
   authenticate the payload. `sha512` is an additional published integrity
   field and release-audit check; it does not replace the Tauri signature.

## Manual manifest generation

`gen-updater-manifest.py` is the single manifest formatter. A normal release
invocation must provide all four platform artifacts. For each artifact it
reads `PATH.sig` by default, copies the complete signature text into the JSON,
and computes SHA-512 over the exact artifact bytes. It reads the public key from
`src-tauri/tauri.conf.json` and refuses an empty or missing trust root. It never
creates a signature.

Example using assets downloaded from a release:

```bash
python scripts/gen-updater-manifest.py \
  --version 1.2.3 \
  --notes-file RELEASE_NOTES.md \
  --base-url https://github.com/TysAIs/xPST/releases/download/v1.2.3 \
  --output latest.json \
  --artifact darwin-aarch64=/path/darwin-aarch64-xPST.app.tar.gz \
  --artifact darwin-x86_64=/path/darwin-x86_64-xPST.app.tar.gz \
  --artifact windows-x86_64=/path/windows-x86_64-xPST-setup.exe \
  --artifact linux-x86_64=/path/linux-x86_64-xPST.AppImage
```

A missing or empty sidecar is a hard error, for example:

```text
gen-updater-manifest: error: missing updater signature for linux-x86_64: expected ...
```

The `--platform` option exists only for the local macOS E2E, where the test
machine intentionally builds one platform. It must not be used for a release
manifest.

To stage an already-generated full manifest for Pages:

```bash
scripts/publish-updater.sh --manifest latest.json --site-dir /tmp/xpst-pages
```

The script validates all four entries, URLs, signatures, and 128-hex-character
SHA-512 values, then writes `/tmp/xpst-pages/updates/latest.json`. The workflow
passes that directory to `actions/upload-pages-artifact` and
`actions/deploy-pages`; this is the actual publication step for the configured
HTTPS endpoint.

## Local updater round-trip

The harness is intentionally opt-in and local-only. It builds version A and B,
serves a manifest and payload from a loopback HTTP server, launches A, and
asserts that the app-written boot marker for B appears after
`download_and_install()` and restart:

```bash
# One-time local-only key; never use it for a release.
cargo tauri signer generate \
  -w .tauri/xpst-updater-e2e.key --password "" --ci

XPST_PYTHON=/Users/itxji/xPST/.venv/bin/python \
  scripts/updater-e2e.sh \
  --manifest-url http://127.0.0.1:9555/updates/latest.json
```

The script accepts only `http://127.0.0.1` or `http://localhost` URLs ending in
`/updates/latest.json`. It injects that endpoint and the local public key via a
Tauri config overlay; the committed production endpoint and public key are not
mutated. Exit `0` means both app versions booted, with B booted from the
updater's relaunch. Exit `2` with `E2E signing key missing` means the proof is
blocked on creating/providing the local test key, not on a production signing
key.

The round-trip runs unsigned macOS app bundles from a direct binary path, so it
does not prove Apple Developer ID signing or notarization. Those remain separate
release-owner requirements.

## Current endpoint baseline

Before the Pages workflow has been run for a release, the known baseline is:

```text
curl ... https://tysais.github.io/xPST/updates/latest.json
HTTP 404
```

There was no `gh-pages` branch at that measurement. This is expected until the
owner supplies the private signing secret, publishes a four-platform tagged
release, enables the Pages source, and the deployment workflow completes.
