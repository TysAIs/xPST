# Getting Started with xPST

> **Free · Local-first · Open-source · Cross-platform**
> The repository includes paths for YouTube, Instagram, X/Twitter, TikTok, Threads, and Messenger, but current capability is not uniform. YouTube, X, and Instagram are live-verified; TikTok is source-only; Threads and Messenger are disabled/unauthenticated. See [INSTALL.md](INSTALL.md#capability-truth-table).

xPST (Cross-Posting Suite) watches configured video sources, downloads new
videos, re-encodes them with FFmpeg, and cross-posts only to destinations that
are configured and currently available. Nothing about your accounts or media
leaves your computer except the uploads themselves.

---

## Table of Contents

1. [What you'll need](#what-youll-need)
2. [Install xPST](#install-xpst)
3. [First-run setup](#first-run-setup)
4. [Connect your accounts](#connect-your-accounts)
5. [Where your credentials live](#where-your-credentials-live)
6. [Your first cross-post](#your-first-cross-post)
7. [Keep it running](#keep-it-running)
8. [Where to go next](#where-to-go-next)

---

## What you'll need

| Requirement | Details |
|-------------|---------|
| **Python** | 3.10 or newer (3.11 recommended). The system Python 3.9 on macOS is **too old** — use a venv with 3.11+. |
| **FFmpeg** | Required for per-platform re-encoding. |
| **yt-dlp** | Installed automatically with xPST; used for source downloads. |
| **An account per platform** | You only need accounts for the platforms you want to post *to*. Sources (e.g. TikTok) often need no login at all. |

Install FFmpeg:

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg

# Windows
winget install Gyan.FFmpeg
```

> **macOS Python note:** the Homebrew `python@3.14` formula is currently broken on some setups. Use a working 3.10/3.11/3.12 install (e.g. `brew install python@3.12` or pyenv). System `/usr/bin/python3` is 3.9 and will **not** work.

---

## Install xPST

### From PyPI (not available yet)

`pip install xpst` is not available today: the PyPI JSON endpoint returns HTTP
404. For a packaged desktop install, use [INSTALL.md](INSTALL.md). For the
source path, use the checkout instructions below.

### From source

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify the install:

```bash
xpst version
# xPST 1.x.x — Cross-Posting Suite
```

---

## First-run setup

Run the interactive setup wizard. It creates your config file at `~/.xpst/config.yaml`, sets sane defaults for encoding, scheduling, and reliability, and walks you through each step.

```bash
xpst setup
```

The wizard will ask about:

- **Sources** — which accounts to *watch* for new videos (TikTok username, local folder, etc.).
- **Destinations** — which platforms to *post to*.
- **Encoding** — per-platform resolution/bitrate/profile (defaults are tuned for each platform's spec).
- **Scheduling** — how often to check for new videos (default: every 15 minutes).
- **Notifications** — optional Discord/Telegram alerts on errors or successful posts.

You can re-run `xpst setup` at any time, or edit `~/.xpst/config.yaml` directly. Every setting can also be overridden with an environment variable prefixed `XPST_`, e.g.:

```bash
XPST_MONITORING_LOG_LEVEL=DEBUG xpst run
XPST_ACCOUNTS_TIKTOK_USERNAME=myuser xpst run
```

---

## Connect your accounts

After setup, connect each destination platform. xPST uses a streamlined connection wizard:

```bash
# Connect one platform at a time
xpst connect youtube
xpst connect instagram
xpst connect x
xpst connect tiktok

# Connect selected/configured platforms in one guided session
xpst connect

# Test existing connections (no uploads)
xpst connect --test
```

Each platform has its own setup guide with screenshots-level detail:

| Platform | Current role/status | Auth method | Guide |
|----------|---------------------|-------------|-------|
| **YouTube** | Live-verified (account-dependent) | OAuth 2.0 (Google Cloud) | [setup-youtube.md](setup-youtube.md) |
| **Instagram** | Live-verified (account-dependent) | Meta Graph API (recommended) | [setup-instagram.md](setup-instagram.md) |
| **X / Twitter** | Live-verified (account-dependent) | Login via twikit (cookies saved) | [setup-x-twitter.md](setup-x-twitter.md) |
| **TikTok** | Source-only; destination pending external review | yt-dlp source path | [setup-tiktok.md](setup-tiktok.md) |
| **Threads** | Disabled / unauthenticated; opt-in destination | Meta Threads API | [setup-threads.md](setup-threads.md) |
| **Messenger** | Disabled / unauthenticated; opt-in messaging/auto-reply | Static Page Access Token | [setup-messenger.md](setup-messenger.md) |

> **Why so many auth methods?** Each platform exposes a different API. The
> table reflects the current live state; a setup guide can describe an
> implementation without proving that the integration is enabled or working
> today.

---

## Where your credentials live

xPST keeps configuration and state locally under `~/.xpst/`. The
`CredentialStore` protects credentials with Fernet-encrypted fallback files or
an OS keychain when explicitly enabled. Some platform flows also write
owner-only token, cookie, or session files and some credential fields in
`config.yaml`; the repository does not prove that every such file is encrypted.
Treat the whole directory as sensitive.

### Directory layout

```
~/.xpst/
├── config.yaml                  # Main config and provider settings
├── credentials/                 # Credential-store data and session files
│   ├── *.enc                         # Encrypted credential-store values
│   ├── .fallback_secret              # Per-install random key — 0600
│   └── .fallback_salt                # scrypt salt — 0600
├── downloads/                   # Cached source videos
├── logs/xpst.log                # Structured logs
└── state.json                   # Cross-post history
```

### Encryption model

- **Fallback store:** credentials written by `CredentialStore` are encrypted
  with Fernet; the key is derived with scrypt from per-install random material.
- **OS keychain (opt-in):** set `XPST_USE_KEYRING=1` to use the macOS Keychain,
  Windows Credential Locker, or Linux Secret Service when available.
- **No plaintext fallback:** if encrypted fallback storage is needed but the
  `cryptography` package is unavailable, xPST refuses to write the credential.
- **Owner-only files:** platform-specific token/session files are chmodded or
  otherwise restricted where the platform supports it, but they are not all
  encrypted by the repository. Do not share `~/.xpst/`.

To inspect the current auth state without posting, use:

```bash
xpst auth status --json
```

---

## Your first cross-post

### Manual post (quickest way to verify everything works)

```bash
# Post a single local video to all configured, available destinations
xpst post -v ~/Videos/my_clip.mp4 -c "First cross-post with xPST 🚀"

# Post to specific platforms only
xpst post -v ~/Videos/my_clip.mp4 -c "YouTube + IG only" -p youtube,instagram

# Dry run — show exactly what would happen, upload nothing
xpst post -v ~/Videos/my_clip.mp4 -c "test" --dry-run --json
```

### Automatic cross-posting

Once your sources and destinations are connected, run the engine once:

```bash
# Check for new videos from sources and cross-post any that are new
xpst run
```

Or watch continuously:

```bash
# Check every 15 minutes (default)
xpst watch

# Custom interval (seconds)
xpst watch --interval 300
```

### Check health

```bash
# Health check — tests configured platform auth, without uploads
xpst health

# Machine-readable
xpst health --json
```

---

## Keep it running

### As a background service (launchd / systemd)

See deployment guidance in docs/INSTALL.md and docs/QUICKSTART.md for Docker, launchd, systemd, and CI/CD patterns, plus the security checklist for production use.

### Logs and diagnostics

```bash
xpst logs                 # Tail recent logs
xpst diagnostics          # Export a REDACTED support bundle (secrets stripped)
```

---

## Where to go next

- 📺 [YouTube setup](setup-youtube.md) — Google Cloud OAuth, one-time
- 📸 [Instagram setup](setup-instagram.md) — Meta Graph API (ban-safe)
- 🐦 [X/Twitter setup](setup-x-twitter.md) — login-based, no cookie export
- 🎵 [TikTok setup](setup-tiktok.md) — source-only today; destination publishing awaits external review
- 🧵 [Threads setup](setup-threads.md) — opt-in, currently disabled/unauthenticated
- 💬 [Messenger setup](setup-messenger.md) — opt-in auto-reply, currently disabled/unauthenticated
- 🛠️ [Troubleshooting](troubleshooting.md) — common errors and fixes
- 🚀 [Quickstart](QUICKSTART.md) — install and first run

---

**xPST is and will always be free, local, and open-source.** Your accounts and your media stay on your machine. If something in this guide didn't work, please open an issue — we want it to be bulletproof.
