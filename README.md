<p align="center">
  <img src="assets/logos/banner-logo.png" alt="xPST — Cross-Posting Suite" width="700">
</p>

<p align="center">
  <strong>Post once, publish to connected destinations. Enterprise-grade, local-first, open-source cross-posting for short-form video.</strong>
</p>

<p align="center">
  <a href="https://www.python.org"><img alt="Python" src="https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13-blue"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-green"></a>
  <a href="https://github.com/TysAIs/xPST/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/TysAIs/xPST/actions/workflows/ci.yml/badge.svg"></a>
  <a href="#"><img alt="Platforms" src="https://img.shields.io/badge/platforms-7-blue"></a>
  <a href="#"><img alt="Platform" src="https://img.shields.io/badge/os-Linux%20|%20macOS%20|%20Windows-lightgrey"></a>
  <a href="#"><img alt="MCP Server" src="https://img.shields.io/badge/MCP-40%20tools-orange"></a>
  <a href="#"><img alt="Desktop" src="https://img.shields.io/badge/desktop-PySide6%2FQML-blueviolet"></a>
</p>

---

## Table of Contents

- [What is xPST](#what-is-xpst)
- [Features](#features)
- [Download](#download)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [CLI Reference](#cli-reference)
- [Desktop App Guide](#desktop-app-guide)
- [Dashboard Guide](#dashboard-guide)
- [MCP Integration Guide](#mcp-integration-guide)
- [Platform Setup Guides](#platform-setup-guides)
- [Video Quality Pipeline](#video-quality-pipeline)
- [Configuration Reference](#configuration-reference)
- [Architecture Overview](#architecture-overview)
- [Development Guide](#development-guide)
- [Security Practices](#security-practices)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## What is xPST

**xPST** (Cross-Posting Suite) is a local-first, open-source automation tool that takes a creator's short-form video from one source platform and republishes it to connected destinations. It tracks per-post performance across configured platforms in one place, and feeds the creator's published content into a personal knowledge base that any connected AI agent can semantically query.

xPST includes integrations for **six platforms** — YouTube, Instagram, X/Twitter, TikTok, Threads, and (opt-in) Facebook Messenger — but their current availability is not uniform. YouTube, X, and Instagram are live-verified; TikTok is currently source-only; Threads and Messenger are disabled/unauthenticated. See the [capability truth table](docs/INSTALL.md#capability-truth-table) before treating an integration as ready.

It runs three ways:
- **Desktop GUI** — PySide6/QML native app with 8 pages
- **CLI** — 46 top-level commands (69 including subcommands) covering the entire workflow
- **MCP server** — 40 tools so AI agents can drive the entire product

No subscriptions, no cloud servers, no vendor lock-in. Your content and credentials never leave your machine.

**Privacy: zero personal data in the distributable tools.** xPST ships no
telemetry, analytics endpoint, or hosted account. Everything — videos,
captions, upload state, cookies, OAuth tokens — lives on your machine under
`~/.xpst/` (the credential store uses encrypted files by default and can use
an OS keychain when explicitly enabled). The only network traffic is the
platform API calls you configure. See
[docs/PRIVACY.md](docs/PRIVACY.md) for the full model.

---

## Features

### Core Cross-Posting
- **Live-verified integrations** — YouTube, Instagram, and X/Twitter are authenticated and live-checked; publishing still depends on your accounts and API/session state
- **TikTok source support** — TikTok can currently be used as a source; destination publishing is pending external developer review
- **Explicitly disabled integrations** — Threads and Messenger remain opt-in and currently unauthenticated/disabled
- **Connected-provider fan-out** — One source video can be sent to destinations that are actually configured and available; see the [capability truth table](docs/INSTALL.md#capability-truth-table)
- **Smart passthrough** — A probe checks whether the source already satisfies the platform profile and skips the re-encode entirely, saving a generation loss
- **Circuit breakers** — One platform failing never blocks the others; repeat offenders are disabled and recover automatically
- **Crash recovery** — Partially-completed uploads are detected and queued for retry on next launch

### Unified Analytics
- **Analytics adapters** — per-platform collectors for YouTube, Instagram, X, TikTok, and Threads; live availability depends on the capability status and your credentials
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
- **CLI** — 46 Click-based commands (69 including subcommands) with `--json` output, `--dry-run` mode, and meaningful exit codes
- **MCP server** — 40 tools (34 `xpst_*` + 2 `messenger_*` + 4 `kb_*`) for AI agent integration

#### Surface counts

These numbers are generated from the shipped code, not maintained by hand. `python scripts/generate_counts.py --check` fails when a claim here drifts, and runs in CI.

<!-- BEGIN GENERATED SURFACE COUNTS -->
| Surface | Count | Measured from |
|---------|-------|---------------|
| MCP tools | **40** | `tools/list` over a real stdio handshake with `xpst mcp start` |
| CLI top-level commands | **46** | `xpst.cli.main.commands` |
| CLI commands including subcommands | **69** | recursive walk of the Click command tree |
| HTTP routes (dashboard app) | **34** | FastAPI route table (30 xPST routes + 4 framework docs routes) |
| Supported providers | **7** | `xpst.provider_truth.SUPPORTED_PROVIDERS` |

Regenerate and verify with `python scripts/generate_counts.py --write` / `--check`; the check runs in CI, so these numbers cannot drift silently.
<!-- END GENERATED SURFACE COUNTS -->

### Enterprise Hardening
- **Encrypted credential-store values** — Fernet/scrypt `.enc` fallback by default, with optional OS keychain storage; platform-specific token/session files remain owner-only and the whole `~/.xpst/` directory is sensitive
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

## Download

Download desktop artifacts from the [GitHub Releases page](https://github.com/TysAIs/xPST/releases). The per-platform asset names, SHA256 verification steps, Gatekeeper guidance, uninstall steps, and current capability status are in [docs/INSTALL.md](docs/INSTALL.md).

**Current limitation:** the newest published installers were built from an older tag than `main`, and macOS signing/notarization is not proven. Verify the release's `SHA256SUMS` before opening an artifact. There is no proven automatic-update channel today, so install newer builds manually from Releases.

---

## Quick Start

Three commands to get going:

```bash
# 1. Install (uv recommended)
git clone https://github.com/TysAIs/xPST.git && cd xPST
uv venv && uv pip install -e ".[full]"

# 2. Setup (interactive wizard connects your platforms)
xpst onboard

# 3. Run (check for new videos and cross-post them)
xpst run
```

That's it. After you connect a live-verified source and destination, use `xpst run` to process content. The actual destinations available to your installation depend on account credentials and the capability status in [docs/INSTALL.md](docs/INSTALL.md).

Other entry points:

```bash
xpst dashboard    # local web dashboard at http://localhost:8080
xpst app          # native desktop app (PySide6/QML)
xpst mcp          # MCP server for AI agents (stdio)
xpst auth status  # check which platforms are connected
```

### The onboarding flow

`xpst onboard` is the first-run path: it checks which platforms are already connected, then walks you through the configured setup steps one at a time. Some integrations are currently disabled or source-only; consult the [capability truth table](docs/INSTALL.md#capability-truth-table) before enabling a destination.

```bash
xpst onboard             # guided first-run setup
xpst onboard --dry-run   # preview the plan, no side effects
xpst connect youtube     # re-link one platform later (e.g. after token expiry)
xpst doctor              # something not posting? auth health + quota + fix-it checklist
```

When a platform upload fails, the CLI now prints a copy-pasteable fix
(expired token → re-auth command, missing scope → which scope, quota
exceeded → `xpst quota`) instead of just the raw error.

Client secrets: xPST never ships platform client secrets. YouTube, X,
Instagram and Threads use your own developer app/keys with a guided
copy-paste setup wizard (BYO app) — sharing a secret in an open-source
repo would let anyone burn your API quota. TikTok can be used as a source;
destination publishing is pending external developer review and approved app
credentials.

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
| `dashboard` | Web dashboard (FastAPI + WebSocket) |
| `desktop` | Native desktop GUI (PySide6/QML) |
| `windows` | Windows-specific pywin32/winshell |
| `dev` | pytest, ruff, mypy, import-linter |
| `full` | Everything (`mcp,desktop,dashboard,windows,knowledge`) |

### PyPI status

`pip install xpst` is **not available yet**: the PyPI JSON endpoint currently
returns HTTP 404. Use the GitHub release desktop assets described in
[docs/INSTALL.md](docs/INSTALL.md), or install from a source checkout with the
source-install steps above. Do not treat the wheel attached to a GitHub Release
as proof that the package is published on PyPI.

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

xPST provides 46 top-level commands (69 including subcommands). Run `xpst --help` for the full list. Most commands accept `--json` for machine-readable output, and the CLI auto-enables JSON mode when stdout is piped (non-TTY).

### Setup & Accounts

| Command | Description |
|---------|-------------|
| `xpst setup` | Interactive first-time setup wizard (connects platforms, writes config) |
| `xpst onboard` | Guided first-run onboarding for configured platforms (`--dry-run` previews) |
| `xpst doctor` | Diagnose auth health, quotas and environment; prints a prioritized fix-it checklist |
| `xpst connect [PLATFORM]` | Streamlined account connection wizard; use `--test` to test existing |
| `xpst auth [PLATFORM]` | Authenticate with a specific platform (youtube/x/instagram/tiktok/threads) |
| `xpst auth status` | Show authentication and quota status for all platforms, with a truthful per-platform badge derived from a live check |
| `xpst refresh-tokens` | Refresh expiring/expired access tokens in the background (bounded retry, no prompts, no token material printed) |
| `xpst config show` | Display current configuration as YAML (sensitive values masked) |
| `xpst config set KEY VALUE` | Set a config value using dotted keys (e.g. `rate_limits.youtube 10`) |
| `xpst config validate` | Validate configuration for errors (exit 0 if valid, 4 if invalid) |
| `xpst config fix` | Detect and auto-fix common configuration issues |
| `xpst config export FILE` | Export configuration to a file |
| `xpst config import FILE` | Import configuration (merge or replace, with diff preview) |
| `xpst readiness` | Show first-run readiness and next actions; use `--fix` to create missing dirs |
| `xpst providers` | Show supported source and destination providers with capabilities |
| `xpst media status` | Show which ffmpeg/ffprobe xPST will use and where it came from (env / system / fetched) |
| `xpst media fetch` | Download a checksum-verified static ffmpeg/ffprobe into `~/.xpst/bin` (only needed when the machine has none) |

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
| **Connect** | Connect and manage configured social accounts; current live status is in the [capability table](docs/INSTALL.md#capability-truth-table) |
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
framework needed). It is loopback-only by default (`127.0.0.1`). Read-only
endpoints are protected with HTTP Basic auth when dashboard credentials are
configured; every mutating endpoint (`POST /api/post`, `/api/connect/{platform}`,
`/api/onboarding*`, `/bio/edit`) always requires the xPST API token, because
loopback is not an authorisation boundary. Print it with
`xpst auth api-token`.

```bash
xpst dashboard                # http://127.0.0.1:8080
xpst dashboard --port 9000    # custom port
```

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | none | Aggregated per-platform health (`healthy` / `degraded`) |
| `GET /metrics` | none | Prometheus text-format metrics |
| `GET /state` | Basic | Posting summary: totals, per-platform counts, health, best platform |
| `POST /api/post` | API token | Plan (`dry_run`) or publish through the real engine |
| `POST /api/connect/{platform}` | API token | Inspect / enable / verify one destination |
| `GET /webhook/messenger` | none | Meta webhook handshake (only when Messenger is enabled) |
| `POST /webhook/messenger` | none | Messenger events, verified with `X-Hub-Signature-256` |

Set the dashboard password (stored as a bcrypt hash) to also protect reads:

```bash
xpst config set monitoring.dashboard_password mypassword
```

For the full graphical experience use the native desktop app (`xpst app`,
requires the `desktop` extra) or the NiceGUI dashboard (requires the
`dashboard` extra). Endpoints, auth, and response shapes are documented in
[docs/DASHBOARD.md](docs/DASHBOARD.md).

---

## MCP Integration Guide

xPST is designed to be driven end-to-end by AI agents over the [Model Context Protocol](https://modelcontextprotocol.io).

### Setup

Install xPST from a source checkout using the installation steps above, then
use the MCP extra from that checkout. PyPI publication is not available yet;
see [docs/INSTALL.md](docs/INSTALL.md#capability-truth-table) for the current
status.

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

<!-- BEGIN GENERATED README TOOL INDEX -->

### 38 Tools

Generated from the live registry — full schemas, consent gates, and per-tool notes live in [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md).

| Tool | Purpose | Mutates real accounts |
|------|---------|-----------------------|
| `xpst_run` | Check for new videos and cross-post them to configured platforms | **Yes** |
| `xpst_post` | Manually post a local video file or carousel to platforms | **Yes** |
| `xpst_analytics` | Per-post and per-platform engagement metrics (views, likes, comments, shares) with persisted sn… | No |
| `xpst_cross_post_analytics` | Cross-post correlation analytics (B1): one video posted to multiple platforms shown as a single… | No |
| `xpst_followers` | Follower counts per platform with growth history. Returns total followers across all platforms,… | No |
| `xpst_best_time` | Best time to post per platform, based on engagement history. Analyzes when your posts get the h… | No |
| `xpst_security_audit` | Run an automated security check on the xPST installation. Verifies credential file permissions,… | No |
| `xpst_suggest_caption` | Generate AI caption suggestions for a video file. Uses the video's transcript to generate 3 cap… | No |
| `xpst_generate_ideas` | Generate post ideas for a content topic (AI content studio). Uses the KB LLM when configured (X… | No |
| `xpst_transcript` | Get the transcript for a video by its content_hash or video_id. Returns the full transcript tex… | No |
| `xpst_search` | Search the knowledge base for nuggets, clips, and topics. Returns matching knowledge nuggets wi… | No |
| `xpst_activity` | List recorded platform failures with targeted retry or review actions (read-only) | No |
| `xpst_schedule_list` | List scheduled posts (pending, completed, failed) with times and targets | No |
| `xpst_schedule_add` | Schedule a post for later: local video file + caption + ISO-8601 time, optional platform list a… | **Yes** |
| `xpst_health` | Test connectivity to all platforms and sources (no uploads) | No |
| `xpst_status` | Show cross-posting statistics and health status | No |
| `xpst_backfill` | Retry failed or incomplete posts from history | **Yes** |
| `xpst_config_show` | Display current configuration (with sensitive values masked) | No |
| `xpst_auth_status` | Show live authentication status for every provider — the same verdict as `xpst auth status` (ro… | No |
| `xpst_bio_get` | Get the link-in-bio page URL and its current configuration. Returns the public /bio URL, the pa… | No |
| `xpst_capabilities` | Return the canonical role-aware provider and capability contract without network calls | No |
| `xpst_preflight` | Run the canonical side-effect-free post preflight for local media and targets (media, caption,… | No |
| `xpst_readiness` | Return local setup readiness and actionable blockers without starting the posting engine | No |
| `xpst_auth_start` | Return a human-only authentication action plan; never opens a browser or accepts secrets | No |
| `xpst_providers` | List supported content sources and posting destinations with capabilities | No |
| `xpst_disconnect` | Disconnect a platform: remove its stored account credentials (tokens, cookies, session files) a… | **Yes** |
| `xpst_delete` | Delete a post record from state | **Yes** |
| `messenger_send` | Send a text message to a Messenger recipient (page-scoped PSID) via the Meta Graph API. Require… | **Yes** |
| `messenger_set_rules` | Configure the Messenger ManyChat-lite auto-reply rules. Provide a keyword->reply map (the '*' k… | **Yes** |
| `xpst_messenger_check_comments` | Fetch recent comments on an Instagram or Facebook post and auto-reply per the configured reply_… | **Yes** |
| `kb_add` | Ingest a local file or URL into the knowledge base | **Yes** |
| `kb_query` | Return stored knowledge nuggets whose text matches the query | No |
| `kb_organize` | Discover areas, tag difficulty, and assign nuggets | **Yes** |
| `kb_areas` | List discovered knowledge areas in course order (beginner -> advanced) | No |
| `xpst_setup_start` | Start or return the shared resumable setup transaction | No |
| `xpst_setup_status` | Read the shared setup transaction and pending human actions | No |
| `xpst_setup_resume` | Resume setup with safe step state or caller-verified readiness | No |
| `xpst_setup_reset` | Reset the shared setup transaction and its recovery copies | No |

<!-- END GENERATED README TOOL INDEX -->

### Security Guardrails

Mutating tools — every row marked **Yes** in the table above — post to or mutate **real accounts**. With no environment variables set they are **refused** (fail-closed). Three tiers control them:

- **`XPST_MCP_ALLOW_MUTATIONS=1`** — Explicit opt-in: mutating tools run without a per-call confirmation
- **`XPST_MCP_REQUIRE_CONFIRM=1`** — Consent tier: mutating tools require `confirm: true` in the arguments
- **`XPST_MCP_READONLY=1`** — Blocks all mutating tools entirely, even when `ALLOW_MUTATIONS` is set (read-only mode)

Local setup-state tools (`xpst_setup_start`, `xpst_setup_resume`, `xpst_setup_reset`) are not in the mutating set: they change local setup/config state rather than platform accounts, and are not gated.

### Recommended Agent Cold-Start Flow

```
xpst_providers → xpst_auth_status → xpst_health → xpst_run(dry_run: true) → live run after user confirmation → xpst_search
```

Metadata tools (`xpst_providers`, `xpst_config_show`, `xpst_auth_status`) are lightweight and never start the posting engine.

See [docs/TUTORIAL_MCP.md](docs/TUTORIAL_MCP.md) for a full MCP walkthrough with every tool's schema and examples, and [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) for the complete schema reference.

---

## Platform Setup Guides

xPST includes six platform integrations, but the live status is not uniform. The current capability snapshot is in the [truth table](docs/INSTALL.md#capability-truth-table). Each setup guide documents the configuration path and platform-specific prerequisites:

| Platform | Current role/status | Auth method | Guide |
|----------|---------------------|-------------|-------|
| YouTube | Live-verified (account-dependent) | OAuth 2.0 (official Data API v3) | [docs/setup-youtube.md](docs/setup-youtube.md) |
| Instagram | Live-verified (account-dependent) | Meta Graph API (official, default) | [docs/setup-instagram.md](docs/setup-instagram.md) |
| X / Twitter | Live-verified (account-dependent) | Cookies (twikit) or API v2 | [docs/setup-x-twitter.md](docs/setup-x-twitter.md) |
| TikTok | Source-only; destination pending external review | yt-dlp (source) / Content Posting API (not currently available) | [docs/setup-tiktok.md](docs/setup-tiktok.md) |
| Threads | Disabled / unauthenticated; opt-in destination | Meta Threads API (official) | [docs/setup-threads.md](docs/setup-threads.md) |
| Messenger | Disabled / unauthenticated; opt-in messaging/auto-reply | Facebook Page Access Token + app secret | [docs/setup-messenger.md](docs/setup-messenger.md) |

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

`xpst connect instagram` now supports the official OAuth flow (ban-safe).

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

`xpst connect x` now supports the official OAuth flow (ban-safe).

### TikTok (source-only today)

TikTok is currently supported as a **source** for downloading content to
cross-post elsewhere. Source fetching uses `yt-dlp`, with browser cookies
available for HD downloads when configured:

```bash
xpst connect tiktok   # asks for the username to watch + optional browser cookies
```

Enabling browser cookies (`cookies_from_browser: true`) can unlock HD,
watermark-free downloads via `yt-dlp`.

TikTok **destination publishing is not available yet**. It awaits external
TikTok developer review and approved app credentials. Do not enable or document
it as a ready publishing destination until that review is complete.

See [docs/setup-tiktok.md](docs/setup-tiktok.md) for the source configuration
and the pending destination requirements.

### Threads (Meta Threads API — opt-in, currently disabled)

Threads is implemented as an official Meta API destination, but it is currently
**disabled and unauthenticated**. The configuration below describes the
opt-in requirements only; it is not a claim that Threads is ready in the
current live environment.

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

The configured Threads path has platform limits (including post frequency,
video duration/size, and caption length) and may refresh a still-valid token.
See [docs/setup-threads.md](docs/setup-threads.md) for the opt-in requirements.

### Messenger (opt-in — currently disabled)

Messenger is an **opt-in messaging/auto-reply** integration, not a video-posting
target. It is currently disabled and unauthenticated. The configuration below
is a future opt-in path, not a live-readiness claim. When enabled, xPST can
receive Page webhooks and match incoming messages against your `reply_rules`.

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
xpst post -v ./my-video.mp4 -c "My caption" -p youtube,instagram,x,threads
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

## Video Quality Pipeline

xPST ships every cross-posted video at the highest fidelity the target platform
accepts. Before any upload, the media pipeline decides — per platform — what
happens to your file, always preferring the path that spends the fewest quality
generations:

1. **Passthrough** — source streams already match the platform profile
   (H.264/yuv420p, within the long-edge and fps caps, AAC audio) in a native
   container: the source bytes are uploaded untouched.
2. **Remux** — streams are perfect but the container is foreign (MKV, WebM,
   AVI): a zero-loss stream copy (`-c copy`) into MP4 with `+faststart`.
   Never a re-encode when a remux will do.
3. **Transcode** — otherwise the platform's encoder profile runs, with
   orientation-aware scaling (the LONG edge, portrait never crushed),
   frame rate as a cap (`-fpsmax`, never a force), HDR→SDR tone-mapping
   (or a loud refusal instead of washed-out colors), closed GOPs, and
   EBU R128 loudness normalization.

Per-platform profiles (defaults, overridable in `video.encoding_*` config):

| | YouTube | TikTok | Instagram Reels | X |
|---|---|---|---|---|
| Rate control | 8 Mbps **two-pass** | CRF 20 (10M cap) | CRF 20 (10M cap) | 10 Mbps **two-pass** |
| Audio | AAC 256k @ 48 kHz | AAC 128k @ 44.1 kHz | AAC 256k @ 44.1 kHz | AAC 256k @ 44.1 kHz |
| Loudness target | −14 LUFS | −14 LUFS | −14 LUFS | −16 LUFS |
| Max duration | — (Shorts 60 s) | 10 min | 15 min | 140 s |

**Loudness normalization** is two-pass EBU R128 (`loudnorm` measurement pass,
then linear-mode correction) so every platform receives audio already at its
preferred level — no per-platform gain surprises, no true-peak limiter pumping.

**Pre-flight verification**: every upload is checked against the platform's
ingest spec right before it ships — container, codec, pix_fmt, geometry, fps,
bitrate, audio, duration, file size, faststart, and measured loudness. Hard
errors (e.g. a container the platform rejects, no real video track) block the
upload with an actionable message; warnings are logged and attached to the
upload result's `quality` metadata.

Check any file yourself, with a dry-run transformation plan:

```bash
xpst verify-media ./my-video.mp4                    # verify against all platforms
xpst verify-media ./my-video.mp4 -p x --plan        # X spec + what the pipeline would do
```

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

# Run the test suite (current pass/skip counts: see the CI badge at the top)
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

- **Credentials stay local.** The `CredentialStore` uses Fernet-encrypted files
  (scrypt-derived keys) by default and can use the OS keychain when explicitly
  enabled with `XPST_USE_KEYRING=1`. Some platform flows also maintain
  owner-only token/session files; treat `~/.xpst/` as sensitive.
- **Official APIs where live and configured.** YouTube, Instagram's Graph API,
  and the live-verified paths use sanctioned APIs or user-owned sessions as
  documented. TikTok destination publishing is not currently available;
  Threads and Messenger are disabled/unauthenticated.
- **Secrets are masked** in `xpst config show`, redacted in `xpst diagnostics` bundles, and never written to logs.
- **MCP guardrails** (`XPST_MCP_ALLOW_MUTATIONS`, `XPST_MCP_REQUIRE_CONFIRM`, `XPST_MCP_READONLY`) gate the mutating tools so agents cannot post without explicit authorization — with none set, they are refused.
- **Self-audit** your installation with `xpst security-audit`, which checks credential file permissions and configuration hygiene.

See [docs/PRIVACY.md](docs/PRIVACY.md) for the full privacy model.

---

## License

xPST is dual-licensed under **MIT OR Apache-2.0**. You may choose either license. See [LICENSE](LICENSE) for details.

---

## Acknowledgments

xPST stands on the shoulders of excellent open-source projects, including FFmpeg, yt-dlp, faster-whisper, LanceDB, PySide6/Qt, Click, httpx, twikit, and the Model Context Protocol. Thank you to all their maintainers.
