# Getting Started with xPST

> **Free · Local-first · Open-source · Cross-platform**
> Distribute short-form video to YouTube, Instagram, X/Twitter, TikTok, Threads, and LinkedIn — from one tool, on your machine.

xPST (Cross-Posting Suite) watches your video sources (TikTok, YouTube, Instagram, X, or local files), downloads new videos, re-encodes them per-platform with FFmpeg, and cross-posts to every connected destination. Nothing about your accounts or media ever leaves your computer except the uploads themselves — xPST is a local tool that talks directly to each platform's official API.

This guide gets you from zero to your first cross-post in under 15 minutes.

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

### From PyPI (recommended)

```bash
# Create an isolated environment (optional but recommended)
python3.12 -m venv ~/.venvs/xpst
source ~/.venvs/xpst/bin/activate

pip install xpst
```

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

# Connect all platforms in one guided session
xpst connect

# Test every existing connection (no uploads)
xpst connect --test
```

Each platform has its own setup guide with screenshots-level detail:

| Platform | Auth method | Guide |
|----------|-------------|-------|
| **YouTube** | OAuth 2.0 (Google Cloud) | [setup-youtube.md](setup-youtube.md) |
| **Instagram** | Meta Graph API (recommended) | [setup-instagram.md](setup-instagram.md) |
| **X / Twitter** | Login via twikit (cookies saved) | [setup-x-twitter.md](setup-x-twitter.md) |
| **TikTok** | yt-dlp browser cookies (source only) | [setup-tiktok.md](setup-tiktok.md) |
| **Threads** | Meta Threads API (long-lived token) | [setup-threads.md](setup-threads.md) |
| **LinkedIn** | LinkedIn OAuth 2.0 | See platform setup docs for current auth instructions |

> **Why so many auth methods?** Each platform exposes a different API. xPST always prefers the **official, ban-safe API** when one exists. Instagram in particular defaults to the official Meta Graph API rather than unofficial private-API clients, which historically get accounts banned.

---

## Where your credentials live

xPST takes credential security seriously. **Your tokens, cookies, and secrets never leave your machine** and are never written in plaintext.

### Directory layout

```
~/.xpst/
├── config.yaml                  # Main config (no secrets — only file paths & flags)
├── credentials/                 # All auth material lives here
│   ├── youtube_client_secrets.json   # OAuth client ID (from Google Cloud) — 0600
│   ├── youtube_token.json            # OAuth user token (auto-refreshed) — 0600
│   ├── x_cookies.json                # X/Twitter session cookies — 0600
│   ├── instagram_session.json        # instagrapi session (fallback only) — 0600
│   ├── *.enc                         # Encrypted secrets (Fernet)
│   ├── .fallback_secret              # Per-install random key — 0600
│   └── .fallback_salt                # scrypt salt — 0600
├── downloads/                   # Cached source videos
├── logs/xpst.log                # Structured logs
└── state.json                   # Cross-post history (what's been posted where)
```

### Encryption model

- **Primary store:** every secret is encrypted with **Fernet** (AES-128-CBC + HMAC). The Fernet key is derived from a per-install random secret using the **scrypt** KDF (RFC 7914 cost factors). The secret and salt are generated on first use and stored with `0600` (owner-only) permissions.
- **Encrypted files:** secrets are written to `~/.xpst/credentials/<key>.enc`. Files you can read (like `youtube_token.json` and `x_cookies.json`) are also locked to `0600`.
- **OS keychain (opt-in):** on macOS the non-code-signed CLI would trigger a Keychain password prompt on *every* access, so the encrypted file fallback is the **default**. To use the macOS Keychain / Windows Credential Locker / Linux Secret Service instead, set `XPST_USE_KEYRING=1`. To force-disable keyring, set `XPST_NO_KEYRING=1`.
- **No plaintext, ever:** if neither the OS keychain nor the `cryptography` package is available, xPST **refuses to store** the credential and raises `PlaintextStorageError` rather than silently writing it in the clear.

```bash
# See what's stored and how
xpst auth status
# Credential Storage: File Storage (fallback)
# Stored Credentials: 4
#   🔑 instagram_graph_token
#   🔑 instagram_graph_user_id
#   🔑 x_cookies
#   🔑 youtube_token
```

---

## Your first cross-post

### Manual post (quickest way to verify everything works)

```bash
# Post a single local video to all enabled destinations
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
# Full health check — tests every platform's auth, no uploads
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
- 🎵 [TikTok setup](setup-tiktok.md) — source only, browser cookies
- 🧵 [Threads setup](setup-threads.md) — Meta Threads API
- 💼 [LinkedIn setup](setup-linkedin.md) — LinkedIn OAuth 2.0
- 🛠️ [Troubleshooting](troubleshooting.md) — common errors and fixes
- 🚀 [Quickstart](QUICKSTART.md) — install and first run

---

**xPST is and will always be free, local, and open-source.** Your accounts and your media stay on your machine. If something in this guide didn't work, please open an issue — we want it to be bulletproof.
