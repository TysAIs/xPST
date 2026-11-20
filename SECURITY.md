# Security Policy

## Credential Storage

> Nothing here is exempt: the encrypted store protects the values *inside the
> store*. It does not protect the library-native JSON files that xPST also
> writes into the same directory — those are plaintext by design. See
> [Plaintext credential files](#plaintext-credential-files-library-native-required-at-runtime)
> before you assume every credential under `~/.xpst/` is encrypted.

xPST takes credential security seriously. Authentication tokens, session cookies and
API keys are stored using a layered security approach: an encrypted credential store
(Fernet files or the opt-in OS keychain) **plus** owner-only library-native files
whose contents are plaintext.

### Encrypted credential store (default)

By default, xPST stores credentials in encrypted files under
`~/.xpst/credentials/`. Credentials are written as `.enc` files and encrypted
with **Fernet** (AES-128-CBC + HMAC-SHA256). The `cryptography` dependency is
part of the normal xPST install.

The Fernet key is **not** derived from any world-readable machine identifier.
Instead, on first use xPST generates a random 32-byte per-install secret and a
random 16-byte salt, both written with `0600` (owner read/write only)
permissions to `~/.xpst/credentials/`. The Fernet key is then derived from that
secret using the **scrypt** key-derivation function (N=2¹⁴, r=8, p=1 — RFC 7914
interactive cost factors). Encrypted `.enc` files are also written `0600`.

### Optional: OS Keychain

On platforms where the OS keychain is practical (Linux with Secret Service, or a
code-signed macOS build), set the environment variable `XPST_USE_KEYRING=1` to
opt in to OS-native secure storage:

- **macOS**: Keychain (encrypted, requires Touch ID or password to access)
- **Windows**: Credential Locker (encrypted, tied to user account)
- **Linux**: Secret Service (GNOME Keyring, KWallet)

> **Why not default?** On macOS, non-code-signed CLI apps trigger a Keychain
> password dialog on *every* access — store, retrieve, and even the availability
> probe — making the OS keychain unusable for a CLI tool. The encrypted file
> fallback (Fernet + scrypt) is the default. Set `XPST_NO_KEYRING=1` to
> explicitly disable keyring even when it would otherwise work (takes
> precedence over `XPST_USE_KEYRING`).

OS keychain provides:
- Encryption at rest for every value the credential store holds
- OS-level access control
- No store-managed secret in a file the keychain has to unlock
- Integration with system security policies

It does **not** remove the plaintext library-native files described below: those
are written by the platform flows regardless of which backend the store uses, so
"keychain enabled" does not mean "nothing sensitive on disk".

If `cryptography` is deliberately removed or unavailable **and** the OS keychain
is also unusable, xPST **refuses to store the credential** (raising a
`PlaintextStorageError` and logging it) rather than ever writing the secret to
disk in cleartext.

### Plaintext credential files (library-native, required at runtime)

<!-- xpst-plaintext-credentials: youtube_token.json x_cookies.json instagram_session.json -->

Three credential files under `~/.xpst/credentials/` are **not encrypted**. They
are plaintext JSON, they are live, and the runtime genuinely requires them:

| File | In the clear |
|---|---|
| `youtube_token.json` | the google-auth OAuth2 token document, including the refresh token |
| `x_cookies.json` | the twikit cookie jar, including the `auth_token` / `ct0` auth cookies |
| `instagram_session.json` | the instagrapi session document, including `sessionid` |

Why they exist: google-auth, instagrapi and twikit parse and rewrite their
credentials from a JSON document, and xPST's analytics collectors construct
their own clients from these files. They are written when you connect or
authenticate, and **rewritten on every successful token/cookie/session refresh**
(`xpst/utils/sessions.py`, `connect.py`, `auth_flow.py`, `token_refresh.py`), so
they are never a stale leftover — they hold the current credential.

What reads them, i.e. why you cannot simply delete them:

- `xpst/analytics.py` — the YouTube ownership/analytics path, the Instagram
  collector and the X collector read these files **only**. There is no
  encrypted-store fallback there: with the file gone, analytics silently
  collects nothing.
- `XUploader._get_client_direct` and `InstagramUploader._get_client_direct`
  raise `FileNotFoundError` when the file is missing (the direct path used when
  no `SessionManager` is injected).
- `SessionManager` reads the encrypted store first and falls back to this file
  when the store has no value; on a successful refresh it writes both, so the
  encrypted copy stays authoritative for that path.

Every one of these files is written **`0600`** (owner read/write only) through
`xpst/utils/secure_io.py`, and the credentials directory is created `0700`.
`xpst security-audit` reports a failing permission check if any of them is
group- or world-readable. That protects them from other local users — it does
**not** protect them from:

- **backups and dotfile sync** — `~/.xpst` copied into Time Machine, a cloud
  backup, a git dotfiles repo or a synced folder ships live tokens in the clear;
- any process running as your user (including malware);
- a Docker volume or a support bundle built from that directory.

So: treat `~/.xpst/credentials/` itself as secret material. Exclude it from
dotfile sync, or encrypt the destination, and prefer re-authenticating over
restoring someone else's credential file.

Two further files in the same directory are plaintext but not store secrets:

- `youtube_client_secrets.json` — the OAuth **client** secret you download from
  Google Cloud Console. xPST does not write it; it is plaintext by definition
  and is the same for every user of that client.
- `_keyring_index.json` — credential *key names* only (no values), written
  `0600` when the OS keychain is enabled.

### What Is Stored

"Encrypted store" is the Fernet `.enc` file (or OS keychain with
`XPST_USE_KEYRING=1`). "Plaintext file" is the library-native file the same
credential is also written to, in the clear, `0600`.

| Credential | Store key | Encrypted store | Plaintext file also written |
|---|---|---|---|
| YouTube OAuth Token | `youtube_token` | Encrypted file (`.enc`) by default; OS keychain with `XPST_USE_KEYRING=1` | `youtube_token.json` |
| X/Twitter Cookies | `x_cookies` | Encrypted file (`.enc`) by default; OS keychain with `XPST_USE_KEYRING=1` | `x_cookies.json` |
| Instagram Session | `instagram_session` | Encrypted file (`.enc`) by default; OS keychain with `XPST_USE_KEYRING=1` | `instagram_session.json` |
| Dashboard API token | `dashboard_api_token` | Encrypted file (`.enc`) by default; OS keychain with `XPST_USE_KEYRING=1` | — (store only) |

### What Is NOT Stored

- Passwords are **never** stored by xPST
- API keys are read from environment variables or config files
- OAuth client secrets (`client_secrets.json`) must be provided by the user
- The dashboard API token is never written to `config.yaml`

## Local HTTP surface (dashboard / desktop app)

The dashboard binds loopback (`127.0.0.1`) and is embedded by the desktop app.
**Loopback is not an authorisation boundary**: any process running as your user,
and any page open in your browser, can reach `127.0.0.1:<port>`. The server
therefore treats every mutating route as privileged:

- `POST /api/post`, `POST /api/connect/{platform}`, `POST /api/onboarding`,
  `POST /api/onboarding/complete`, `POST /api/preflight` and `POST /bio/edit`
  **always** require a credential — either the dashboard API token or, when
  configured, the dashboard Basic login. Without one they answer `401`.
- Read-only routes keep their previous behaviour (`/health`, `/metrics`, `/bio`
  and the OAuth callback stay public; everything else requires Basic auth only
  when a dashboard password is configured), so the UI is never locked out.
- `POST /oauth/callback` and the optional Messenger webhook stay public by
  design — they are reached by browser redirects and by Meta, neither of which
  can attach a header — and validate their own payloads.

The API token is generated on first run and stored in the encrypted credential
store (`dashboard_api_token`), never in `config.yaml`; no default value ships
with the project. Print or rotate it with `xpst auth api-token` /
`xpst auth api-token --rotate`.

The token is handed to browser contexts out-of-band and is never embedded in
served HTML:

- the desktop shell mints a per-launch token, passes it to the engine as
  `XPST_UI_TOKEN` and opens its own webview at
  `http://127.0.0.1:<port>/#xpst_token=<token>`; the SPA reads the fragment and
  immediately strips it from the address bar (fragments are never sent to the
  server, so the token cannot reach an access log),
- `xpst ui` does the same for the browser it opens,
- CLI/agent callers use `xpst auth api-token` or `XPST_API_TOKEN`.

The one place a token does appear in a URL is the link-in-bio editor form
(`?token=`), because a plain HTML form cannot send a header. The server
therefore filters `token=` out of uvicorn's access log before it is written
(`AccessLogRedactionFilter`), so the console/CI log cannot capture it.

Scope and limits, stated plainly: this stops unauthenticated writes from other
processes, other users, and cross-origin/CSRF attempts from web pages. It is
**not** a defence against malware already running as your user, which can read
`~/.xpst/` (as it could read any other file of yours) and therefore the token.

## Configured bind host

Binding to a non-loopback address exposes the dashboard to the network. In that
configuration set `monitoring.dashboard_username` + `dashboard_password_hash` so
reads are protected too: mutating routes are always credential-gated, but
read-only analytics/state would otherwise be world-readable on your LAN.

## .gitignore Protection

The repository `.gitignore` is configured to prevent accidental commits of:

- All credential files (`*.json` patterns for tokens, cookies, sessions)
- Environment files (`.env`, `.env.*`)
- State files containing user-specific data
- Session directories
- The entire `~/.xpst/` runtime directory

## Reporting a Vulnerability

If you discover a security vulnerability in xPST, please report it responsibly:

1. **Do NOT** open a public GitHub issue
2. Email the maintainers privately (see README.md for contact info)
3. Include a description of the vulnerability and steps to reproduce
4. Allow reasonable time for a fix before public disclosure

## Security Best Practices for Users

1. **Encrypted file storage is the default** — the `.enc` credential store is encrypted at rest with Fernet + scrypt. Three library-native files in the same directory (`youtube_token.json`, `x_cookies.json`, `instagram_session.json`) are plaintext by design, so keep the whole directory private and out of backups/sync unless you encrypt them
2. **Opt into OS keychain** — set `XPST_USE_KEYRING=1` for OS-native secure storage (recommended on Linux with Secret Service or code-signed macOS builds). This encrypts store values; it does not remove the plaintext library-native files
3. **Rotate credentials regularly** — re-authenticate platforms periodically
4. **Use environment variables** for API keys in production/deployment
5. **Review `.gitignore`** before pushing to ensure no secrets are tracked
6. **Run `xpst auth status`** to verify credential storage health
7. **Never share** your `~/.xpst/` directory or credential files
8. **Check modes after a restore** — `ls -l ~/.xpst/credentials/` must show `-rw-------` for every file; a backup tool can restore them group- or world-readable

## Dependencies

xPST's security depends on:

- [`keyring`](https://pypi.org/project/keyring/) — OS keychain integration
- [`google-auth`](https://pypi.org/project/google-auth/) — OAuth2 token management
- [`twikit`](https://pypi.org/project/twikit/) — X/Twitter session management
- [`instagrapi`](https://pypi.org/project/instagrapi/) — Instagram session management

## Platform Terms of Service Compliance

### ⚠️ Important Notice

xPST uses unofficial APIs and third-party libraries to interact with social media platforms. Users are solely responsible for ensuring their use complies with platform Terms of Service.

### Unofficial APIs Used

| Platform | Library | Status | Risk Level |
|----------|---------|--------|------------|
| Instagram | instagrapi | Unofficial API | High |
| X/Twitter | twikit | Unofficial API | High |
| YouTube | google-api-python-client | Official API | Low |
| TikTok | yt-dlp | Unofficial/Scraping | Medium |

### Platform-Specific Risks

#### Instagram (via instagrapi)
- Uses unofficial private API endpoints
- May violate Instagram's Terms of Service
- Risk of account suspension or ban
- Instagram actively detects and blocks unofficial API usage

#### X/Twitter (via twikit)
- Uses unofficial API endpoints
- May violate X/Twitter's Terms of Service
- Risk of account suspension or ban
- X/Twitter has been increasingly restrictive with API access

#### TikTok (via yt-dlp)
- Uses video scraping/downloading techniques
- May violate TikTok's Terms of Service
- Risk of IP-based rate limiting or blocking
- TikTok actively works to prevent unauthorized downloading

#### YouTube (via google-api-python-client)
- Uses official, Google-approved API
- Complies with YouTube's Terms of Service
- Subject to API quota limits
- Lowest risk of account issues

### Recommendations

1. **Use Official APIs When Possible**
   - YouTube: Already using official API ✅
   - Consider official APIs for other platforms if available

2. **Respect Rate Limits**
   - Use xPST's built-in rate limiting features
   - Avoid excessive posting frequency
   - Monitor platform notifications

3. **Account Separation**
   - Consider using dedicated accounts for automation
   - Don't use your primary personal accounts
   - Be prepared for potential account restrictions

4. **Stay Informed**
   - Monitor platform Terms of Service changes
   - Follow library communities for updates
   - Be aware of platform enforcement actions

5. **Legal Compliance**
   - Ensure compliance with local laws regarding automation
   - Respect copyright and intellectual property rights
   - Don't use for spam, harassment, or abusive purposes

### Disclaimer

THE DEVELOPERS OF xPST ARE NOT RESPONSIBLE FOR:
- Account suspensions, bans, or restrictions
- Loss of data or content
- Legal consequences of ToS violations
- Any damages resulting from platform enforcement actions

USE THIS SOFTWARE AT YOUR OWN RISK AND IN COMPLIANCE WITH ALL APPLICABLE TERMS OF SERVICE AND LAWS.
