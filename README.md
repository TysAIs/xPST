<p align="center">
  <img src="assets/logos/banner-logo.png" alt="xPST — Cross-Posting Suite" width="700">
</p>

<p align="center">
  <strong>Post once, publish everywhere. Enterprise-grade, local-first, open-source cross-posting for short-form video.</strong>
</p>

<p align="center">
  <a href="https://www.python.org"><img alt="Python" src="https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13-blue"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-green"></a>
  <a href="#"><img alt="Tests" src="https://img.shields.io/badge/tests-1419%20passing-brightgreen"></a>
  <a href="#"><img alt="Platforms" src="https://img.shields.io/badge/platforms-7-blue"></a>
  <a href="#"><img alt="Platform" src="https://img.shields.io/badge/os-Linux%20|%20macOS%20|%20Windows-lightgrey"></a>
  <a href="#"><img alt="MCP Server" src="https://img.shields.io/badge/MCP-23%20tools-orange"></a>
  <a href="#"><img alt="Desktop" src="https://img.shields.io/badge/desktop-PySide6%2FQML-blueviolet"></a>
</p>

---

## Table of Contents

- [What is xPST](#what-is-xpst)
- [Features](#features)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [CLI Reference](#cli-reference)
- [Desktop App Guide](#desktop-app-guide)
- [Dashboard Guide](#dashboard-guide)
- [MCP Integration Guide](#mcp-integration-guide)
- [Platform Setup Guides](#platform-setup-guides)
- [Configuration Reference](#configuration-reference)
- [Architecture Overview](#architecture-overview)
- [Development Guide](#development-guide)
- [Security Practices](#security-practices)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## What is xPST

**xPST** (Cross-Posting Suite) is a local-first, open-source automation tool that takes a creator's short-form video from one source platform and republishes it — at full native fidelity — to every other platform they own. It tracks per-post performance across all of them in one place, and feeds the creator's published content into a personal knowledge base that any connected AI agent can semantically query.

xPST works across **seven platforms** — YouTube, Instagram, X/Twitter, TikTok, Threads, LinkedIn, and (opt-in) Facebook Messenger — and every one of them is supported end-to-end: in the posting engine, the desktop UI, the analytics layer, and the connection wizard.

It runs three ways:
- **Desktop GUI** — PySide6/QML native app with 8 pages
- **CLI** — 34 top-level commands covering the entire workflow
- **MCP server** — 23 tools so AI agents can drive the entire product

No subscriptions, no cloud servers, no vendor lock-in. Your content and credentials never leave your machine.

**Privacy: zero personal data in the distributable tools.** xPST ships no
telemetry, analytics endpoint, or hosted account. Everything — videos,
captions, upload state, cookies, OAuth tokens — lives on your machine under
`~/.xpst/` (credentials in the OS keychain, encrypted-file fallback). The only
network traffic is the platform API calls you configure. See
[docs/PRIVACY.md](docs/PRIVACY.md) for the full model.

---

## Features

### Core Cross-Posting
- **Six video platforms + Messenger** — YouTube, Instagram, X/Twitter, TikTok, Threads, and LinkedIn as posting destinations, plus opt-in Facebook Messenger auto-reply
- **Full-fidelity fan-out** — One source video downloads once and uploads to every connected destination, with orientation-aware encoding that never degrades quality
- **Bidirectional cross-posting** — Monitor ALL connected sources for new content and distribute to every connected destination (not just one direction)
- **Smart passthrough** — A probe checks whether the source already satisfies the platform profile and skips the re-encode entirely, saving a generation loss
- **Circuit breakers** — One platform failing never blocks the others; repeat offenders are disabled and recover automatically
- **Crash recovery** — Partially-completed uploads are detected and queued for retry on next launch

### Unified Analytics
- **6-platform coverage** — Engagement metrics normalized into one schema across YouTube, Instagram, X, TikTok, Threads, and LinkedIn
- **Per-post engagement metrics** — Views, likes, comments, shares, and platform-specific signals in one normalized schema
- **Follower tracking** — Per-platform follower counts with growth history (`xpst followers`)
- **Best-time recommendations** — Suggested posting windows derived from your own engagement history (`xpst best-time`)
- **Cross-post correlation** — See how the same video performs across every platform it landed on
- **Persistent history** — Every collection run appends per-post snapshots to a local SQLite store (`~/.xpst/analytics.db`), so trends come from real history
- **Honest capability matrix** — Clear documentation of what each platform actually exposes (and what it does not)
- **Export** — Analytics data exportable to JSON or CSV

### Personal Content Knowledge Base
- **Transcription** — Every ingested video is transcribed via faster-whisper
- **Cited knowledge nuggets** — Content distilled into cited knowledge fragments with provenance (source URL, timestamps)
- **Vector search** — Semantic search over your content with LanceDB embeddings (with substring-match fallback)
- **Knowledge areas** — Organize nuggets into areas, order by difficulty into a course outline
- **AI captions** — Generate caption suggestions from a video file (`xpst suggest-caption`)
- **Agent-queryable** — Query your back catalog from the CLI or any AI agent over MCP

### Three Drivable Surfaces
- **Desktop GUI** — PySide6/QML app with Dashboard, Compose, Content, Analytics, Connect, Schedule, Settings, and About pages + DetailPanel
- **CLI** — 34 Click-based commands with `--json` output, `--dry-run` mode, and meaningful exit codes
- **MCP server** — 23 tools (19 `xpst_*` + 4 `kb_*`) for AI agent integration

### Enterprise Hardening
- **Encrypted credentials** — OS keychain storage (macOS Keychain, Linux Secret Service, Windows Credential Manager) with encrypted `.enc` file fallback (Fernet + scrypt)
- **Atomic state writes** — Write-then-rename and pidfile locking prevent corruption
- **Anti-bot pacing** — Randomized delays, time-of-day awareness, rate limits, User-Agent rotation
- **Quota management** — Configurable daily upload limits per platform (`xpst quota`)
- **Dead-letter queue** — Failed uploads tracked and retryable
- **Security audit** — Automated check of your installation's credential hygiene and permissions (`xpst security-audit`)
- **Diagnostics bundles** — Redacted export for support
- **State backup/restore** — Export, import, and snapshot the posting state
- **Config validation** — Detect and auto-fix common configuration issues
- **Plugin system** — Extend with custom uploaders and sources
- **i18n** — Translations supported via `~/.xpst/translations/`

---

## Quick Start

Three commands to get going:

```bash
# 1. Install (uv recommended)
git clone https://github.com/TysAIs/xPST.git && cd xPST
uv venv && uv pip install -e ".[full]"

# 2. Setup (interactive wizard connects your platforms)
xpst setup

# 3. Run (check for new videos and cross-post them)
xpst run
```

That's it. Your videos are now cross-posted to every connected platform.

Other entry points:

```bash
xpst dashboard    # local web dashboard at http://localhost:8080
xpst app          # native desktop app (PySide6/QML)
xpst mcp          # MCP server for AI agents (stdio)
xpst auth status  # check which platforms are connected
```

---

## Installation

### Prerequisites

- **Python 3.10+** (3.11–3.13 recommended)
- **FFmpeg** on PATH (or set `FFMPEG_BINARY` in your config)
- **uv** (recommended) or plain pip

### Option 1: Install from source with uv (recommended)

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
uv venv && uv pip install -e ".[full]"
xpst setup
xpst run
```

### Option 2: Install from source with pip

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"
xpst setup
xpst run
```

### Option 3: Minimal install (CLI only, no desktop/KB)

```bash
pip install -e .
```

### Optional extras

| Extra | What it adds |
|-------|-------------|
| `mcp` | MCP server (`xpst mcp`, `xpst-mcp`) |
| `knowledge` | KB transcription/embeddings/LanceDB |
| `pyside6` | Native desktop GUI (PySide6/QML) |
| `dashboard` | Web dashboard (FastAPI + WebSocket) |
| `desktop` | pywebview fallback for desktop |
| `windows` | Windows-specific pywin32/winshell |
| `dev` | pytest, ruff, mypy, import-linter |
| `full` | Everything (`mcp,desktop,pyside6,dashboard,windows,knowledge`) |

### PyPI (v1.0.0 and later)

```bash
pip install "xpst[full]"
xpst setup
```

The wheel is a pure-Python `py3-none-any` package; the `xpst` and `xpst-mcp`
console scripts are installed with it.

### Docker

xPST ships with a `Dockerfile` and `docker-compose.yml` for containerized CLI and MCP server usage:

```bash
# Build the image
docker build -t xpst .

# Run the MCP server
docker run -i xpst xpst mcp

# Or use docker-compose
docker-compose up
```

See `Dockerfile` and `docker-compose.yml` for details.

---

## CLI Reference

xPST provides 34 top-level commands. Run `xpst --help` for the full list. Most commands accept `--json` for machine-readable output, and the CLI auto-enables JSON mode when stdout is piped (non-TTY).

### Setup & Accounts

| Command | Description |
|---------|-------------|
| `xpst setup` | Interactive first-time setup wizard (connects platforms, writes config) |
| `xpst connect [PLATFORM]` | Streamlined account connection wizard; use `--test` to test existing |
| `xpst auth [PLATFORM]` | Authenticate with a specific platform (youtube/x/instagram/tiktok/threads/linkedin) |
| `xpst auth status` | Show authentication and quota status for all platforms |
| `xpst config show` | Display current configuration as YAML (sensitive values masked) |
| `xpst config set KEY VALUE` | Set a config value using dotted keys (e.g. `rate_limits.youtube 10`) |
| `xpst config validate` | Validate configuration for errors (exit 0 if valid, 4 if invalid) |
| `xpst config fix` | Detect and auto-fix common configuration issues |
| `xpst config export FILE` | Export configuration to a file |
| `xpst config import FILE` | Import configuration (merge or replace, with diff preview) |
| `xpst readiness` | Show first-run readiness and next actions; use `--fix` to create missing dirs |
| `xpst providers` | Show supported source and destination providers with capabilities |

### Core Posting

| Command | Description |
|---------|-------------|
| `xpst run` | One-shot: check for new videos from a source and cross-post them |
| `xpst run --source all` | Bidirectional: check ALL sources and distribute to ALL destinations |
| `xpst run --dry-run` | Show what would happen without uploading |
| `xpst watch` | Continuous monitoring loop (runs until Ctrl+C) |
| `xpst watch --interval 300` | Check every 300 seconds (default: from config) |
| `xpst post -v VIDEO -c CAPTION` | Manually post a video file; use multiple `-v` for carousel |
| `xpst post -v v.mp4 -c 'text' -p youtube,x,threads` | Post to specific platforms only |
| `xpst backfill` | Retry failed or incomplete posts from history |
| `xpst backfill --dry-run` | Show what would be backfilled without uploading |
| `xpst delete VIDEO_ID` | Delete a posted video from platforms; use `--platform` to target one |
| `xpst schedule add FILE --caption TEXT --at TIME` | Schedule a post for later publishing |
| `xpst schedule list` | List all scheduled posts |
| `xpst schedule remove ID` | Remove a scheduled post by ID |
| `xpst schedule run` | Process all due scheduled posts (typically called by cron) |
| `xpst schedule install` | Install an OS-level scheduler (macOS LaunchAgent / Linux cron / Windows Task) |

### Analytics & Observability

| Command | Description |
|---------|-------------|
| `xpst analytics` | Show cross-platform analytics summary (views, likes, comments, shares) |
| `xpst analytics --refresh` | Force refresh (ignore cache) |
| `xpst analytics export -o FILE` | Export analytics to JSON or CSV (`--format csv`) |
| `xpst followers` | Show follower counts per platform with growth history |
| `xpst best-time` | Show recommended posting times based on engagement history |
| `xpst quota` | Show API quota usage and remaining uploads per platform |
| `xpst status` | Show cross-posting statistics and health status |
| `xpst health` | Test connectivity to all platforms (no uploads) |
| `xpst security-audit` | Run an automated security check on the xPST installation |
| `xpst logs` | View recent logs (last 50 lines) |
| `xpst diagnostics` | Export a redacted local diagnostics bundle (zip) |
| `xpst failures list` | List failed uploads from the dead-letter queue |
| `xpst failures retry VIDEO_ID --platform P` | Retry one failed upload by re-posting its source file |

### Knowledge Base & Content

| Command | Description |
|---------|-------------|
| `xpst kb add SOURCE` | Ingest a local file or URL: transcribe, extract nuggets, embed, store |
| `xpst kb query TEXT` | Semantic search over your content (substring fallback, cited) |
| `xpst kb organize` | Discover areas, tag difficulty, and assign nuggets |
| `xpst kb areas` | List discovered knowledge areas in course order (beginner → advanced) |
| `xpst kb course` | Emit the organized, cited outline |
| `xpst kb doctor` | Read-only health check of the knowledge workspace |
| `xpst kb reembed` | Re-embed all nuggets with the configured embedding model |
| `xpst kb migrate-store` | Migrate the store format |
| `xpst search TEXT` | Search the knowledge base for nuggets and topics |
| `xpst transcript ID` | Get the transcript for a video by ID or content hash |
| `xpst suggest-caption -v VIDEO` | Generate AI caption suggestions from a video file |

### Surfaces

| Command | Description |
|---------|-------------|
| `xpst app` | Launch native desktop app (PySide6/QML); appears in your dock |
| `xpst dashboard` | Launch local web API dashboard at `http://localhost:8080` |
| `xpst mcp` | Start MCP (Model Context Protocol) server over stdio |

### State Management

| Command | Description |
|---------|-------------|
| `xpst state export OUTPUT` | Export state.json to OUTPUT (validated copy) |
| `xpst state import SOURCE` | Restore state.json (current state backed up first) |
| `xpst state backup` | Snapshot state.json into `~/.xpst/backups/` with rotation |

### Maintenance

| Command | Description |
|---------|-------------|
| `xpst update` | Update xPST dependencies to latest versions |
| `xpst update --check` | Check for updates without installing |
| `xpst update --components` | Show app, helper, and provider metadata update status |
| `xpst version` | Show xPST version and all dependency versions |
| `xpst plugins list` | List installed plugins |
| `xpst plugins docs` | Generate markdown documentation for installed plugins |
| `xpst build` | Build a standalone executable using PyInstaller |
| `xpst build --target macos` | Cross-compile for a different OS via Docker |

### Global Options

| Option | Description |
|--------|-------------|
| `--config / -c` | Path to config file |
| `--verbose / -v` | Enable verbose (DEBUG) logging |
| `--quiet / -q` | Suppress decorative output |
| `--json` | Output in JSON format (auto-enabled when piped) |
| `--version` | Show version |

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | General error |
| `2` | Authentication failure |
| `3` | Rate limit exceeded |
| `4` | Configuration error |
| `10` | Platform unavailable |

### Dry-Run Mode

All posting commands (`run`, `post`, `backfill`) support `--dry-run` to preview what would happen without uploading. The CLI shows what would be posted and to which platforms, then exits without making any network calls.

### JSON Output Mode

All commands accept `--json` for machine-readable output. Additionally, the CLI **auto-enables JSON mode when stdout is not a TTY** (piped to another process), making it ideal for scripting and agent integration:

```bash
xpst status --json | jq '.stats.posted'
xpst run --dry-run --json | jq '.videos[].video_id'
```

---

## Desktop App Guide

The native desktop app is built with PySide6/QML and provides a polished, Apple-like UI with light/dark mode, the Inter font, and full accessibility support.

```bash
xpst app          # launch the native desktop app
xpst app --no-splash  # skip the splash screen
```

### 8 Pages

| Page | What it does |
|------|-------------|
| **Dashboard** | Overview of posted content, per-platform health, and quota status at a glance |
| **Compose** | Compose a new post: select a video file, write a caption, choose target platforms, and submit |
| **Content** | Browse your content library of posted videos with thumbnails, captions, and per-platform status |
| **Analytics** | View cross-platform engagement metrics (views, likes, comments, shares) with trend history |
| **Connect** | Connect and manage your social accounts across all six platforms (YouTube, Instagram, X/Twitter, TikTok, Threads, LinkedIn) |
| **Schedule** | Manage scheduled posts: create, view, and remove upcoming and recurring posts |
| **Settings** | Customize xPST settings: encoding profiles, rate limits, notifications, and preferences |
| **About** | Version info, dependency versions, links to docs and source, acknowledgments |

### DetailPanel

The DetailPanel is a slide-out panel that shows the full details of a selected post: all per-platform upload results, URLs, error messages, analytics metrics, and timestamps.

### First-Run Welcome

On first launch, a welcome dialog guides you to the Connect page to set up your platform accounts. The app detects whether `~/.xpst/config.yaml` exists and routes you accordingly.

> **Screenshots:** Product banner and app icons ship under `docs/assets/`; page-level screenshots are a follow-up, not a blocker. Run `xpst app` to see the UI live, or see [docs/TUTORIAL_APP.md](docs/TUTORIAL_APP.md) for a full walkthrough.

---

## Dashboard Guide

xPST ships a lightweight web API dashboard (FastAPI + uvicorn, no extra UI
framework needed). It is loopback-only by default (`127.0.0.1`) and protects
all endpoints with HTTP Basic auth when dashboard credentials are configured.

```bash
xpst dashboard                # http://127.0.0.1:8080
xpst dashboard --port 9000    # custom port
```

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | none | Aggregated per-platform health (`healthy` / `degraded`) |
| `GET /metrics` | none | Prometheus text-format metrics |
| `GET /state` | Basic | Posting summary: totals, per-platform counts, health, best platform |
| `GET /webhook/messenger` | none | Meta webhook handshake (only when Messenger is enabled) |
| `POST /webhook/messenger` | none | Messenger events, verified with `X-Hub-Signature-256` |

Set the dashboard password (stored as a bcrypt hash):

```bash
xpst config set monitoring.dashboard_password mypassword
```

For the full graphical experience use the native desktop app (`xpst app`,
requires the `pyside6` extra) or the NiceGUI dashboard (requires the
`dashboard` extra). Endpoints, auth, and response shapes are documented in
[docs/DASHBOARD.md](docs/DASHBOARD.md).

---

## MCP Integration Guide

xPST is designed to be driven end-to-end by AI agents over the [Model Context Protocol](https://modelcontextprotocol.io).

### Setup

```bash
pip install "xpst[mcp]"
```

Add to your MCP client config (Claude Desktop, Claude Code, etc.):

```json
{
  "mcpServers": {
    "xpst": {
      "command": "xpst-mcp",
      "transport": "stdio"
    }
  }
}
```

### 23 Tools

**Posting & operations (10 tools):**

| Tool | Description |
|------|-------------|
| `xpst_run` | Check for new videos and cross-post them (supports `dry_run`, `source`, `max_posts`) |
| `xpst_post` | Post a specific local video file or carousel to platforms |
| `xpst_backfill` | Retry failed or incomplete posts from history |
| `xpst_delete` | Delete a post from a platform |
| `xpst_health` | Test connectivity to all platforms and sources (no uploads) |
| `xpst_status` | Show cross-posting statistics and system status |
| `xpst_config_show` | Display current configuration (sensitive values masked) |
| `xpst_auth_status` | Show authentication status for all platforms |
| `xpst_providers` | List supported content sources and posting destinations with capabilities |
| `xpst_security_audit` | Run an automated security audit of the installation |

**Analytics & insights (4 tools):**

| Tool | Description |
|------|-------------|
| `xpst_analytics` | Per-post and per-platform engagement metrics with persistent history (`live=false` for offline, `live=true` to refresh from APIs) |
| `xpst_cross_post_analytics` | Cross-post correlation analytics — how one video performed across every platform |
| `xpst_followers` | Follower counts per platform with growth history |
| `xpst_best_time` | Recommended posting times derived from engagement history |

**Content & creative (3 tools):**

| Tool | Description |
|------|-------------|
| `xpst_suggest_caption` | Generate AI caption suggestions from a video file |
| `xpst_transcript` | Get the transcript for a video by ID or content hash |
| `xpst_search` | Search transcripts and content across the knowledge base |

**Scheduling (2 tools):**

| Tool | Description |
|------|-------------|
| `xpst_schedule_list` | List scheduled posts (pending, completed, failed) with times and targets |
| `xpst_schedule_add` | Schedule a post: local video + caption + ISO-8601 time + optional platform list and repeat rule |

**Knowledge base (4 tools, deprecated in favor of the `xpst_*` content tools):**

| Tool | Description |
|------|-------------|
| `kb_add` | Ingest a local file or URL into the knowledge base (transcribe, extract nuggets, embed, store) |
| `kb_query` | Return stored knowledge nuggets whose text matches the query (semantic search with cited provenance) |
| `kb_organize` | Discover areas, tag difficulty, and assign nuggets |
| `kb_areas` | List discovered knowledge areas in course order (beginner → advanced) |

### Security Guardrails

Mutating tools (`xpst_run`, `xpst_post`, `xpst_backfill`, `xpst_delete`, `xpst_schedule_add`, `kb_add`, `kb_organize`) post to or mutate **real accounts**. Two environment-variable tiers control them:

- **`XPST_MCP_READONLY=1`** — Blocks all mutating tools entirely (read-only mode)
- **`XPST_MCP_REQUIRE_CONFIRM=1`** — Requires `confirm: true` in the arguments (consent tier)

### Recommended Agent Cold-Start Flow

```
xpst_providers → xpst_auth_status → xpst_health → xpst_run(dry_run: true) → live run after user confirmation → xpst_search
```

Metadata tools (`xpst_providers`, `xpst_config_show`, `xpst_auth_status`) are lightweight and never start the posting engine.

See [docs/TUTORIAL_MCP.md](docs/TUTORIAL_MCP.md) for a full MCP walkthrough with every tool's schema and examples, and [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) for the complete schema reference.

---

## Platform Setup Guides

xPST supports seven platforms. Each setup guide lives in `docs/`:

| Platform | Role | Auth method | Guide |
|----------|------|-------------|-------|
| YouTube | Source + Destination | OAuth 2.0 (official Data API v3) | [docs/setup-youtube.md](docs/setup-youtube.md) |
| Instagram | Source + Destination | Meta Graph API (official, default) | [docs/setup-instagram.md](docs/setup-instagram.md) |
| X / Twitter | Source + Destination | Cookies (twikit), optional API v2 | [docs/setup-x-twitter.md](docs/setup-x-twitter.md) |
| TikTok | Source + Destination | yt-dlp (source) / Content Posting API (destination) | [docs/setup-tiktok.md](docs/setup-tiktok.md) |
| Threads | Destination | Meta Threads API (official) | [docs/setup-threads.md](docs/setup-threads.md) |
| LinkedIn | Destination | LinkedIn API (OAuth 2.0) | [docs/setup-linkedin.md](docs/setup-linkedin.md) |
| Messenger | Destination (opt-in) | Facebook Page Access Token + appsecret | [docs/setup-messenger.md](docs/setup-messenger.md) |

### YouTube (OAuth 2.0 — official API)

xPST uses the official YouTube Data API v3 with your own OAuth project:

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create or select a project
3. Enable **YouTube Data API v3**
4. Create OAuth 2.0 credentials (Desktop application type)
5. Download `client_secrets.json`
6. Save to `~/.xpst/credentials/youtube_client_secrets.json`
7. Run `xpst auth youtube` to complete authentication

The OAuth token is stored in your OS keychain (encrypted file fallback).

### Instagram (Meta Graph API — official, recommended)

Instagram defaults to the official **Meta Graph API** (`auth_mode: graph_api`) — the same sanctioned path that scheduling tools like Buffer and Meta Business Suite use, so there is no ban risk.

1. Convert your account to a **Creator or Business** account (free, reversible) and link it to a Facebook Page
2. Create a Meta Developer app (Business type) at [developers.facebook.com/apps](https://developers.facebook.com/apps)
3. Add the **Instagram Graph API** product
4. Get your IG user ID and generate a **long-lived access token** (60 days, refreshable)
5. Run `xpst connect instagram` and provide the token and IG user ID

```yaml
accounts:
  instagram:
    enabled: true
    auth_mode: graph_api
    graph_access_token: "YOUR_LONG_LIVED_TOKEN"
    graph_ig_user_id: "YOUR_IG_USER_ID"
```

> **Fallback:** An unofficial `instagrapi` session mode (`auth_mode: session`) still exists, but it carries a real risk of account suspension and is **not recommended**. Use the Graph API unless you have no other option. See [docs/setup-instagram.md](docs/setup-instagram.md) for the full walkthrough.

### X / Twitter (cookie-based)

X uses [twikit](https://github.com/d60/twikit) for cookie-based uploads:

**Option 1: Browser cookie export**
1. Log into x.com in your browser
2. Export cookies using a cookie editor extension
3. Save to `~/.xpst/credentials/x_cookies.json`

**Option 2: twikit login**
```bash
python3 -c "import twikit, asyncio; asyncio.run(twikit.Client('en-US').login('USER', 'PASS').save_cookies('cookies.json'))"
mv cookies.json ~/.xpst/credentials/x_cookies.json
```

Then run `xpst auth x`. An official **API v2** mode (`auth_mode: api_v2`) is also available if you have developer credentials. See [docs/setup-x-twitter.md](docs/setup-x-twitter.md).

### TikTok (source + destination)

TikTok works both ways in xPST.

**As a source** (downloading your content to cross-post elsewhere) — no authentication required:

```bash
xpst connect tiktok   # asks for the username to watch + optional browser cookies
```

Enabling browser cookies (`cookies_from_browser: true`) unlocks HD, watermark-free downloads via `yt-dlp`.

**As a destination** (posting *to* TikTok) — supported via the official **Content Posting API v2 (Direct Post)** with full video encoding. This requires a TikTok developer app:

```yaml
accounts:
  tiktok:
    enabled: true
    client_key: "your_tiktok_client_key"
    client_secret: "your_tiktok_client_secret"
```

See [docs/setup-tiktok.md](docs/setup-tiktok.md) and TikTok's [Direct Post docs](https://developers.tiktok.com/doc/content-posting-api-direct-post) for app registration and obtaining a user access token.

### Threads (Meta Threads API — official)

Threads uses Meta's official **Threads API** (container-publish model) with a long-lived access token (60 days, refreshable) — no ban risk:

1. Create a Meta app and add the **Threads API** product at [developers.facebook.com/apps](https://developers.facebook.com/apps)
2. Add your Threads account as a tester and accept the invite
3. Get your numeric Threads user ID via the Threads API Explorer
4. Generate a long-lived token with the `threads_basic` and `threads_content_publish` scopes

```yaml
accounts:
  threads:
    enabled: true
    graph_access_token: "YOUR_LONG_LIVED_THREADS_TOKEN"
    threads_user_id: "9000123456789012"
```

Limits: 250 posts/24h, ≤300s video, ≤1 GB, ≤500-char captions. xPST refreshes the token automatically while it is still valid. See [docs/setup-threads.md](docs/setup-threads.md).

### LinkedIn (LinkedIn API — OAuth 2.0)

LinkedIn uses the official **LinkedIn API** (OAuth 2.0) to publish video posts via the registered-media upload flow (`registerUpload` → upload → create post):

1. Create a LinkedIn developer app at [linkedin.com/developers](https://www.linkedin.com/developers)
2. Request the posting/share permissions and complete the OAuth 2.0 flow to obtain an access token (60 days, refreshable)
3. Configure xPST with your token and LinkedIn user ID

```yaml
accounts:
  linkedin:
    enabled: true
    access_token: "YOUR_LINKEDIN_ACCESS_TOKEN"
    linkedin_user_id: "YOUR_LINKEDIN_USER_ID"
```

Limits: ~150 posts/day, recommended MP4 (H.264) up to ~200 MB. See [docs/setup-linkedin.md](docs/setup-linkedin.md).

### Messenger (opt-in — ManyChat-lite auto-reply)

Messenger is **disabled by default**. It turns a Facebook Page into a keyword
auto-responder: xPST receives Page webhooks (verified with
`X-Hub-Signature-256`), matches incoming messages against your `reply_rules`,
and answers through the Graph API with `appsecret_proof` on every outbound call.

1. Create a Meta app + a Facebook Page you manage
2. Generate a **Page Access Token** with `pages_messaging` + `pages_manage_metadata`
3. Run `xpst auth messenger` (wizard) or set the config:

```yaml
accounts:
  messenger:
    enabled: true
    page_id: "1234567890"
    page_access_token: "PAGETOKEN..."
    app_id: "META_APP_ID"
    app_secret: "APPSECRET..."
    verify_token: "ANYTHING-SECRET"      # developer-chosen; xPST verifies it on GET
    auto_reply: true                     # master switch for ManyChat-lite mode
    reply_rules:                         # keyword -> reply; "*" is the catch-all
      price: "Our prices are on the website."
      "*": "Thanks for the message — a human will follow up soon."
```

4. Point your Page webhook at `https://your-host/webhook/messenger` — the
   dashboard verifies inbound events with `X-Hub-Signature-256` and answers via
   the Graph API with `appsecret_proof` on every outbound call.

MCP tools: `messenger_send`, `messenger_set_rules`. See
[docs/setup-messenger.md](docs/setup-messenger.md) for the full walkthrough.

### Local Files

Use local folders as a source for manual posting and carousels:

```bash
xpst post -v ./my-video.mp4 -c "My caption" -p youtube,instagram,x,threads,linkedin
xpst run --source local
```

---

## Configuration Reference

xPST loads configuration from `~/.xpst/config.yaml` with environment variable overrides (`XPST_*` prefix). Priority: environment variables > config file > defaults.

```yaml
accounts:
  youtube:
    enabled: true
    client_secrets: "~/.xpst/credentials/youtube_client_secrets.json"
    token_file: "~/.xpst/credentials/youtube_token.json"
  instagram:
    enabled: true
    auth_mode: graph_api          # graph_api (recommended) | session (fallback)
    graph_access_token: ""
    graph_ig_user_id: ""
  x:
    enabled: true
    auth_mode: cookies            # cookies (default) | api_v2
    cookies_file: "~/.xpst/credentials/x_cookies.json"
  tiktok:
    username: ""                  # creator to watch (source mode)
    cookies_from_browser: false
    enabled: false                # set true + client_key/secret for destination mode
    client_key: ""
    client_secret: ""
  threads:
    enabled: false
    graph_access_token: ""
    threads_user_id: ""
  linkedin:
    enabled: false
    access_token: ""
    linkedin_user_id: ""
  messenger:                     # opt-in ManyChat-lite auto-reply (off by default)
    enabled: false
    page_id: ""
    page_access_token: ""
    app_id: ""
    app_secret: ""
    verify_token: ""
    auto_reply: false
    reply_rules: {}              # keyword -> reply; "*" catch-all

rate_limits:                      # max uploads per day, per platform
  youtube: 5
  instagram: 5
  x: 5
  tiktok: 5
  threads: 5
  linkedin: 5

video:
  download_dir: "~/.xpst/downloads"
  cleanup_after_post: false
  encoding:                       # per-platform encoding profiles (passthrough-aware)
    youtube: { resolution: 1920, bitrate: "8M", fps: 60 }

reliability:
  max_retries: 3
  circuit_breaker_threshold: 5
  circuit_breaker_reset: 3600

monitoring:
  log_level: INFO
  log_file: "~/.xpst/logs/xpst.log"
  healthcheck_port: 8080

notifications:
  enabled: false
  discord: { webhook_url: "" }
  telegram: { bot_token: "", chat_id: "" }
```

Validate and auto-fix your configuration anytime:

```bash
xpst config validate
xpst config fix
```

Every config key can be overridden by a flat environment variable with the `XPST_` prefix. For example: `XPST_THREADS_GRAPH_ACCESS_TOKEN`, `XPST_INSTAGRAM_AUTH_MODE`, `XPST_YOUTUBE_ENABLED`. Rate limits are set via the config file only (no env override).

---

## Architecture Overview

xPST is organized as a small set of cooperating layers, each with a single responsibility:

- **Providers layer** — Every platform implements a common `PlatformUploader` interface declaring its role (source/destination), capabilities (upload, delete, health, analytics), and auth mode. The provider registry is what `xpst providers` and `xpst_providers` enumerate.
- **Engine** — Orchestrates the cross-post: detect new content from sources, encode once per destination profile (with passthrough probing), fan out uploads, and record results. Circuit breakers and the dead-letter queue live here.
- **State store** — Atomic, write-then-rename JSON state at `~/.xpst/state.json` with pidfile locking, plus the SQLite analytics database at `~/.xpst/analytics.db`.
- **Surfaces** — The CLI (Click), the desktop app (PySide6/QML), and the MCP server are thin drivers over the same engine and state; nothing platform-specific lives in a surface.
- **Knowledge base** — Transcription (faster-whisper), nugget extraction, and LanceDB embeddings, decoupled so it can be installed or omitted via the `knowledge` extra.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and [docs/adr.md](docs/adr.md) for architecture decision records.

---

## Development Guide

```bash
# Install with dev tooling
uv pip install -e ".[full,dev]"

# Run the test suite (1431 collected; 1419 passing / 12 skipped on Python 3.11)
pytest

# Lint and format
ruff check .
ruff format .

# Type checking
mypy src/xpst

# Architectural import boundaries
lint-imports
```

Contributions are welcome. The codebase enforces import boundaries (surfaces must not bypass the engine), keeps platform logic behind the provider interface, and ships every behavior change with tests. See [docs/TUTORIAL_CLI.md](docs/TUTORIAL_CLI.md), [docs/TUTORIAL_APP.md](docs/TUTORIAL_APP.md), and [docs/TUTORIAL_MCP.md](docs/TUTORIAL_MCP.md) for surface-specific walkthroughs.

---

## Security Practices

- **Credentials never leave your machine.** Tokens and cookies are stored in the OS keychain (macOS Keychain, Linux Secret Service, Windows Credential Manager) with an encrypted file fallback (Fernet + scrypt).
- **Official APIs by default.** Instagram (Graph API), Threads, LinkedIn, YouTube, and TikTok destination posting all use sanctioned APIs. Unofficial modes exist only as explicit, documented fallbacks.
- **Secrets are masked** in `xpst config show`, redacted in `xpst diagnostics` bundles, and never written to logs.
- **MCP guardrails** (`XPST_MCP_READONLY`, `XPST_MCP_REQUIRE_CONFIRM`) gate every mutating tool so agents cannot post without explicit authorization.
- **Self-audit** your installation with `xpst security-audit`, which checks credential file permissions and configuration hygiene.

See [docs/PRIVACY.md](docs/PRIVACY.md) for the full privacy model.

---

## License

xPST is dual-licensed under **MIT OR Apache-2.0**. You may choose either license. See [LICENSE](LICENSE) for details.

---

## Acknowledgments

xPST stands on the shoulders of excellent open-source projects, including FFmpeg, yt-dlp, faster-whisper, LanceDB, PySide6/Qt, Click, httpx, twikit, and the Model Context Protocol. Thank you to all their maintainers.
