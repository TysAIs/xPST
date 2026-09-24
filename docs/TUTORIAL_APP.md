# xPST Desktop App Tutorial

> Walkthrough of the xPST desktop app — the **Tauri 2 shell** (`src-tauri/`)
> that opens a native window on the local dashboard UI and runs the Python
> engine as its bundled sidecar.

---

## Table of Contents

1. [Installation](#installation)
2. [Launching](#launching)
3. [The app window](#the-app-window)
4. [Connecting Platforms](#connecting-platforms)
5. [Composing & Posting](#composing--posting)
6. [The dashboard HTTP surface](#the-dashboard-http-surface)
7. [Running from a checkout](#running-from-a-checkout)
8. [Troubleshooting](#troubleshooting)

---

## Installation

### Download a published installer

Open the [GitHub Releases page](https://github.com/TysAIs/xPST/releases), expand
**Assets**, and download the installer for your OS:

| Operating system | Asset |
|---|---|
| macOS (arm64) | the `.dmg` |
| Windows | the NSIS setup `.exe` (or `.msi`) |
| Linux | `.AppImage` or `.deb` |

Verify the release's checksum manifest before opening an artifact, and read
[INSTALL.md](INSTALL.md) for the current signing/notarization status and the
per-platform uninstall steps.

### Prerequisites

- **FFmpeg** is not bundled: the engine uses a system `ffmpeg`, or downloads a
  pinned, checksum-verified static build on first use (`xpst media fetch`).
- A Python installation is **not** required — the engine ships inside the app as
  a frozen sidecar.

---

## Launching

From the app icon, or from the CLI when the app is installed:

```bash
xpst app
```

`xpst app` locates the installed shell (`/Applications/xPST.app` and
`~/Applications/xPST.app` on macOS; the installed executable on Windows/Linux),
launches it, and exits. If no build is installed it prints where to get one
instead of raising a traceback.

---

## The app window

The shell is a window around the same web UI the browser serves: navigation
lives in the sidebar and every section is a dashboard route.

| Section | What it does |
|---------|--------------|
| **Dashboard** | Overview of posted content, per-platform health, and quota status |
| **Setup** | First-run wizard: connect accounts and complete onboarding |
| **Connect** | Connect and manage configured social accounts; live status is in the [capability table](INSTALL.md#capability-truth-table) |
| **Compose** | Pick a video file, write a caption, choose destinations, and submit |
| **Preflight** | Check a media file against each destination's specs before uploading |
| **Last post** | Per-destination results of the most recent post |
| **Analytics** | Cross-platform engagement metrics with trend history |
| **Videos** | Per-video performance detail |
| **Accounts** | Account and credential state per platform |
| **Schedule** | Create, view, and remove upcoming and recurring posts |
| **Activity** | Failed posts and the dead-letter queue |
| **Library** | Browse the downloaded content library |
| **About** | Version info, dependency versions, links to docs and source |
| **Settings** | Encoding profiles, rate limits, notifications, and preferences |

Writes from the shell are authorised automatically; see
[Authentication inside the shell](#authentication-inside-the-shell).

---

## Connecting Platforms

Setup lives on the **Connect** and **Setup** sections. Here is a summary:

### YouTube (OAuth 2.0)

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project and enable **YouTube Data API v3**
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download the `client_secret_*.json` file
5. Place it at `~/.xpst/credentials/youtube_client_secret.json`
6. Click **Connect YouTube** — a browser window opens for OAuth consent
7. Authorize the app — your token is stored encrypted locally

> **Note:** 2FA/2SV on your Google account works fine — the OAuth flow handles it. Default quota: 10,000 units/day.

### Instagram (Meta Graph API, session fallback)

The **primary** Instagram auth is the official Meta Graph API (`auth_mode: graph_api`), using a `graph_access_token` and `graph_ig_user_id`. The instagrapi session method (`auth_mode: session`) is an unofficial **fallback** that carries ban risk.

**Graph API (recommended):**
1. Configure your `graph_access_token` and `graph_ig_user_id` in `~/.xpst/config.yaml`

**Session fallback:**
1. Log into [instagram.com](https://instagram.com) in your browser first
2. Enter your Instagram username and password in the Connect fields
3. Click **Connect Instagram**
4. Credentials are stored encrypted in `~/.xpst/credentials/`

> **Note:** Use a dedicated account for the session method. Max caption 2200 chars. Carousel uploads support up to 10 images/videos.

### X / Twitter (Cookie-based)

1. Log into [x.com](https://x.com) in your browser
2. Export cookies using a browser extension (e.g., EditThisCookie) or DevTools
3. Place cookies JSON at `~/.xpst/credentials/x_cookies.json`
4. Or paste the JSON into the Connect fields
5. Alternatively, run `xpst auth x` in a terminal for guided setup

> **Note:** X destination uses twikit cookies (community/unofficial mode). Max caption 280 chars, video up to 140s. Carousels post as threads.

### TikTok (source-only today)

TikTok is currently a **source-only** path for monitoring and downloading
content to cross-post elsewhere. Destination publishing awaits external
TikTok developer review and approved app credentials.

1. Log into [tiktok.com](https://tiktok.com) in your browser to enable cookie-based source downloads (HD / no-watermark)
2. Export cookies to `~/.xpst/credentials/tiktok_cookies.txt`, or run `xpst auth tiktok` in a terminal
3. Do not configure TikTok as a destination until the external review is complete

### Threads (currently disabled)

Threads is an opt-in destination implementation using Meta's Threads API, but
it is currently disabled/unauthenticated. Do not treat the configuration path
as proof that posting is ready.

1. Configure OAuth credentials only after explicit enablement
2. Run `xpst auth threads` or set credentials in `~/.xpst/config.yaml` when enabled

---

## Composing & Posting

Posting runs through the **Compose** section (preview and submit) and the
**Preflight** section (checks a media file against each destination's specs
before the upload is attempted).

1. **Choose a video** — pick a local file
2. **Write a caption** — the character budget per destination is enforced
3. **Select destinations** — only configured, currently available ones can be chosen (YouTube, Instagram, and X are live-verified; TikTok is source-only; Threads is disabled)
4. **Submit** — the engine encodes once per destination profile and fans the uploads out

Progress and per-destination results land on **Last post**; failures are
tracked in the dead-letter queue on **Activity** and can be retried with
`xpst failures retry` or `xpst backfill`.

### Authentication inside the shell

Mutating routes require the xPST API token. The shell never reads the stored
credential: it mints a **per-launch** token, passes it to the engine as
`XPST_UI_TOKEN`, and opens its webview at
`http://127.0.0.1:<port>/#xpst_token=<token>`. The UI reads the fragment, sends
`X-API-Token` on writes, and strips the fragment from the URL immediately.

Consequences worth knowing:

- The token is regenerated on every launch; nothing is persisted for the shell.
- A plain browser session is *not* authenticated for writes. Print the stored
  token with `xpst auth api-token` when you need to drive those routes yourself.
- The engine binds loopback only. Keep it that way: an API token on a routable
  interface is a local-process-to-network escalation.

---

## The dashboard HTTP surface

Every page in the shell is a route served by the engine. The full table —
endpoints, auth requirements, and response shapes — is in
[DASHBOARD.md](DASHBOARD.md). The same UI is available in a browser with:

```bash
xpst dashboard                # http://127.0.0.1:8080
```

---

## Running from a checkout

Developers build the two halves themselves. Full instructions (prerequisites,
bundle contents, CI lanes, size budgets) are in [PACKAGING.md](PACKAGING.md):

```bash
python -m pip install -e ".[full]" pyinstaller
cd ui && npm ci && npm run build && cd ..
PYTHON="$(command -v python3 || command -v python)" scripts/build-engine.sh
export PATH="$HOME/.cargo/bin:$PATH"
cd src-tauri && cargo tauri build --bundles app
```

Cross-compilation is not supported: the sidecar is a PyInstaller **host-OS**
build, so each platform is packaged on its own machine — which is exactly what
`.github/workflows/tauri-release.yml` does per tag.

---

## Troubleshooting

### The window opens and closes immediately

The shell holds a single-instance lock at `<temp dir>/xpst-shell-single-instance.lock`.
A stale copy of the app (for example a leftover test build) keeps the lock alive
and every fresh launch exits at once. Quit the other copy — `pgrep -fl xPST` —
and relaunch.

### `xpst app` says "xPST desktop app not found."

No shell build was found on disk. Install a published bundle from the
[Releases page](https://github.com/TysAIs/xPST/releases), or build one from a
checkout (see [PACKAGING.md](PACKAGING.md)).

### A write is rejected

Writes need the API token. In the shell this is automatic; in a browser, set
`XPST_API_TOKEN` or sign in with the dashboard credentials.

### FFmpeg not found

```bash
# macOS
brew install ffmpeg

# Linux
sudo apt install ffmpeg

# Or point xPST at an existing build
export XPST_FFMPEG_PATH=/path/to/ffmpeg

xpst media status   # which ffmpeg/ffprobe xPST will use, and from where
xpst media fetch    # download a verified static build into ~/.xpst/bin
```

### Platform connection fails

- **YouTube**: Ensure the OAuth consent screen is configured. Check that `youtube_client_secret.json` is at `~/.xpst/credentials/`
- **Instagram**: For the Graph API, verify `graph_access_token` and `graph_ig_user_id` are valid. For the session fallback, make sure you're logged into instagram.com in your browser first
- **X**: Verify cookies are valid and not expired. Re-export if needed
- **TikTok**: For source downloads, cookies expire — re-export periodically. For posting, ensure the Content Posting API OAuth tokens are valid
- **Threads**: Verify the Meta Threads API OAuth credentials are valid

### Nothing posts

Check `xpst health --json` and the capability truth table in
[INSTALL.md](INSTALL.md#capability-truth-table) — several integrations are
source-only or disabled pending platform review.

### State corruption

If the app behaves unexpectedly:

```bash
# Back up state
xpst state export ~/xpst-backup.json

# Reset state
rm ~/.xpst/state.json

# Restart the app
xpst app
```

### Crash recovery

If the app crashes mid-upload, xPST detects the incomplete upload on the next
launch and can retry or skip the failed posts. Already-uploaded posts are
tracked in state, so a retry cannot double-post.

---

## Getting Help

- 📖 [CLI Tutorial](TUTORIAL_CLI.md)
- 🤖 [MCP Tutorial](TUTORIAL_MCP.md)
- 📦 [Packaging the desktop app](PACKAGING.md)
- 🐛 [Report Issues](https://github.com/TysAIs/xPST/issues)
- 📚 [Documentation](https://github.com/TysAIs/xPST#readme)