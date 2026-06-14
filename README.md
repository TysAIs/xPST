<p align="center">
  <img src="docs/assets/xpst-horizontal.png" alt="xPST — Cross-Platform Studio" width="400">
</p>

# xPST

**Post once, publish everywhere. Local-first cross-posting, unified analytics, and a personal content knowledge base for short-form video creators.**

---

![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-green)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)
![MCP Server](https://img.shields.io/badge/MCP-server-orange)

Tests: 1198 passing, 3 skipped (local Linux run, `pytest`).

---

## What is xPST

xPST (Cross-Post Tool) is a local-first, open-source automation tool that takes a creator's short-form video from one source platform and republishes it, at full native fidelity, to every other platform they own (YouTube, Instagram, and X today). It tracks per-post performance across all of them in one place, and feeds the creator's own published content into a personal knowledge base that any connected AI agent can semantically query ("what did I say about X, and what performed?"). It runs three ways: a desktop GUI, a CLI, and an MCP server so AI agents can drive the entire product. It manages the unofficial third-party libraries it depends on with an update path designed never to silently break the app.

No subscriptions, no cloud servers, no vendor lock-in. Your content and credentials never leave your machine.

---

## The Four Pillars

### 1. Full-fidelity fan-out

One source video downloads once and uploads to every connected destination. The encoding pipeline is built to never degrade your video:

- **Orientation-aware scaling** targets a 1920px long edge, so a 1080x1920 vertical and a 1920x1080 landscape video both keep their full resolution.
- **Frame rate is a cap, never a force** (`-fpsmax 60`): 60fps sources stay 60fps, lower-fps sources are untouched.
- **Modern per-platform profiles**: YouTube 8 Mbps High; Instagram Reels CRF 20 High@4.0 (10 Mbps maxrate); X 10 Mbps High@4.0.
- **Smart passthrough**: a probe checks whether the source already satisfies the platform profile and skips the re-encode entirely, saving a generation loss.
- **Full-quality downloads**: yt-dlp fetches split video+audio streams (`bv*+ba`) and merges them, instead of settling for pre-muxed lower-quality files.
- One platform failing never blocks the others; a circuit breaker disables repeat offenders and recovers automatically.

### 2. Unified per-post analytics with persistent history

Views, likes, comments, shares, and platform-specific signals for every cross-posted video, in one normalized schema. Every collection run appends per-post snapshots to a local SQLite store (`~/.xpst/analytics.db`), keyed on `(platform, post_id, captured_at)`, so trends and "what performed well" come from real history. See the honest [capability matrix](#analytics-capability-matrix-honest) below for exactly what each platform exposes, and what it does not.

### 3. A personal content knowledge base

Every video you ingest is transcribed (faster-whisper), distilled into cited knowledge "nuggets," embedded (fastembed or a local embedding endpoint), and stored locally (JSON store, with a LanceDB vector store available). You can then organize nuggets into knowledge areas, order them by difficulty into a course outline, and query your own back catalog from the CLI or from any AI agent over MCP.

```bash
xpst kb add <url-or-file>     # transcribe, extract cited nuggets, embed, store
xpst kb query "topic"          # search stored nuggets
xpst kb organize               # cluster nuggets into areas, tag difficulty
xpst kb areas                  # list areas in course order
xpst kb course                 # emit the organized, cited outline
xpst kb doctor                 # read-only health check of the workspace
```

Queries are semantic: the query is embedded and vector-searched against the store, with an automatic substring-match fallback when embeddings are unavailable. Every hit carries provenance (source URL, timestamps) and a similarity score, and the response reports which mode answered.

**Honest status:** ingestion is explicit today (`xpst kb add` or the `kb_add` MCP tool); published posts are not yet auto-ingested. Performance-weighted retrieval (rank nuggets by how the source posts performed) is planned on top of the analytics snapshot store but not built yet.

### 4. Three drivable surfaces

- **Desktop GUI** (PySide6/QML): dashboard, content library, analytics, connect, schedule, settings.
- **CLI** (Click): 25 top-level commands covering the entire workflow.
- **MCP server**: 17 tools (12 `xpst_*` + 5 `kb_*`) so AI agents can discover providers, check health, schedule, post, inspect analytics, and mine your knowledge base. See [For AI Agents](#for-ai-agents).

---

## Supported Providers

Run `xpst providers --json` or call the MCP `xpst_providers` tool to inspect the installed provider catalog.

### Destinations (where xPST posts)

- **YouTube Shorts** — official YouTube Data API v3 with OAuth 2.0
- **Instagram Reels** — instagrapi session-based uploads and carousel posts
- **X** — twikit cookie-based uploads and carousel-as-thread posts

> **TikTok is a source only.** xPST does not post to TikTok: there is no official self-serve upload API for this use case and no stable unofficial one, so claiming it would be dishonest. TikTok remains fully supported for downloading your own content as a source.

### Sources (where xPST pulls from)

- **TikTok** — yt-dlp downloads with optional browser cookies
- **YouTube** — yt-dlp channel/video downloads with optional browser cookies
- **Instagram** — instagrapi session-based listing and downloads
- **X** — yt-dlp downloads with optional twikit metadata
- **Local Files** — local folders, videos, images, and carousel groups

---

## Analytics Capability Matrix (honest)

Not every metric is collectible on every platform. This is what the current code actually retrieves:

| Metric | YouTube | Instagram | X | TikTok (source) |
|---|---|---|---|---|
| Views | yes (Data API v3) | insights impressions with a **Business/Creator account**, else public `play_count` | yes | yes (scrape) |
| Likes | yes | yes | yes | yes (scrape) |
| Comments | yes | yes | replies | yes (scrape) |
| Shares | not collected (needs YouTube Analytics API; on roadmap) | **Business/Creator account required** | retweet count (reported as shares) | not exposed |
| Saves | n/a | **Business/Creator account required** | n/a | not exposed |
| Reposts | n/a | not exposed by instagrapi | quote count not collected yet | repost count (scrape) |
| Story-reposts | n/a (no stories) | **impossible without the Meta Graph Business API** | n/a (no stories) | n/a |

Plain-language caveats:

- **Story-reposts are collectible on ZERO platforms via this stack.** No tool built on these libraries can get them; treat any claim otherwise with suspicion.
- **Instagram shares and saves require a Business or Creator account** (Instagram insights API). On a personal account you get views (play count), likes, and comments.
- **TikTok metrics come from an unauthenticated yt-dlp scrape.** They may break without notice if TikTok changes its pages, and there is no SLA.

---

## Video Constraints Per Platform

xPST encodes per-platform but does **not** currently trim or split videos that exceed a platform's duration limit:

| Platform | Aspect | Max duration | What happens if exceeded |
|---|---|---|---|
| YouTube Shorts | 9:16 vertical | 60s | Longer videos upload as regular long-form YouTube videos, not Shorts |
| Instagram Reels | 9:16 vertical | 90s | Upload may be rejected by Instagram |
| X | any | 140s (2:20) | Upload fails |

Encoding targets (defaults, all configurable): 1920px long edge, H.264 High profile, 60fps cap, BT.709, yuv420p. Sources that already comply are passed through without re-encoding.

---

## Quick Start

### Install

**Recommended for most users: download a binary from GitHub Releases.**

1. Open the [xPST Releases page](https://github.com/TysAIs/xPST/releases).
2. Download the asset for your OS:
   - Windows: `xPST.exe`
   - macOS: `.dmg`, `.zip`, or `.app` bundle when available
   - Linux: Linux desktop binary or archive when available
3. Run the app, then complete setup from the desktop onboarding flow or:

```bash
xpst setup
xpst health
```

**Python wheel install:** use this when you want the CLI/MCP server from a release artifact instead of a desktop binary.

```bash
python -m pip install "./xpst-<version>-py3-none-any.whl[mcp,knowledge]"
xpst setup
xpst run
```

**Source install:** use this for development, local patches, or testing unreleased code. With [uv](https://docs.astral.sh/uv/) (fastest):

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
uv venv && uv pip install -e ".[mcp,knowledge]"
uv run xpst setup
uv run xpst run
```

Or with plain pip:

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[mcp,knowledge]"
xpst setup
xpst run
```

Optional extras: `mcp` (MCP server), `knowledge` (KB transcription/embeddings/LanceDB), `pyside6` (desktop GUI), `dashboard` (web dashboard), `full` (everything).

### Configure and run

```bash
xpst setup        # interactive wizard: connects platforms, writes ~/.xpst/config.yaml
xpst health       # test connectivity to all platforms (no uploads)
xpst run          # one-shot: check for new videos and post
xpst watch        # continuous: check every 15 minutes
```

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for per-platform authentication walkthroughs and [docs/INSTALL.md](docs/INSTALL.md) for all install paths.

---

## CLI

xPST provides 25 top-level commands (`xpst --help` for the full list):

```bash
# Setup and accounts
xpst setup | connect | auth | config | readiness | providers

# Core posting
xpst run [--bidirectional]      # fan out new videos from sources
xpst watch [--interval 600]     # continuous monitoring loop
xpst post --video ./v.mp4 --caption "..."   # manual post or carousel
xpst schedule add|list|remove   # scheduled posts
xpst backfill                   # retry failed or incomplete posts
xpst delete <video_id>          # delete a posted video from platforms

# Knowledge base
xpst kb add|query|organize|areas|course|doctor

# Observability and surfaces
xpst status | health | logs | analytics | diagnostics
xpst app                        # native desktop GUI
xpst dashboard                  # web dashboard
xpst mcp                        # MCP server over stdio

# Maintenance
xpst update | version | plugins | build
```

Most commands accept a `--json` flag for machine-readable output; coverage is being expanded. See [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) for output formats.

### Bidirectional cross-posting

Most cross-posting tools work in one direction. xPST can monitor ALL connected sources for new content and distribute to every connected destination:

```bash
xpst run                  # default source -> YouTube, Instagram, X
xpst run --bidirectional  # all sources -> all destinations
```

In bidirectional mode:

- Post a Reel on Instagram, and it goes to YouTube Shorts and X
- Upload a Short on YouTube, and it goes to Instagram Reels and X
- Post a video on X, and it goes to YouTube Shorts and Instagram Reels
- Post on TikTok, and it goes to YouTube Shorts, Instagram Reels, and X (TikTok is source-only)

The engine deduplicates across platforms so content is not double-posted.

---

## For AI Agents

xPST is designed to be driven end-to-end by AI agents over the Model Context Protocol.

### MCP server setup

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

### 17 tools

**12 posting/ops tools:** `xpst_providers`, `xpst_config_show`, `xpst_auth_status`, `xpst_status`, `xpst_health`, `xpst_analytics`, `xpst_schedule_list`, `xpst_schedule_add`, `xpst_run`, `xpst_post`, `xpst_backfill`, `xpst_delete`

**5 knowledge-base tools:** `kb_add`, `kb_query`, `kb_organize`, `kb_areas`, `kb_course`

Metadata tools (`xpst_providers`, `xpst_config_show`, `xpst_auth_status`) are lightweight and never start the posting engine. **`xpst_run` and `xpst_post` post to your real accounts** — agents should always use `dry_run: true` first and confirm with the user before a live post.

Recommended cold-start flow for an agent: `xpst_providers` (discover the catalog) → `xpst_auth_status` / `xpst_health` (confirm readiness) → `xpst_run` with `dry_run: true` → live run after user confirmation → `kb_query` to mine the creator's content.

See [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) for every tool's schema, arguments, and example calls, and [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) for the CLI/JSON and Python API surfaces.

---

## Desktop App and Dashboard

```bash
xpst app          # native desktop app (PySide6/QML), appears in your dock
xpst dashboard    # local web dashboard at http://localhost:8080
```

Both read the same local state as the CLI and require no external services. They provide upload history, per-platform health, analytics, quota tracking, and the dead-letter queue of failed posts.

**Screenshots**

> Screenshots with demo data are coming with the first release — run
> `xpst desktop` or `xpst dashboard` to see the UI live.

---

## Architecture

```
 SOURCES (TikTok, YouTube, Instagram, X, Local)
    |  yt-dlp / instagrapi download, dedup, filter
    v
 CROSS-POST ENGINE
    |  orientation-aware encode (FFmpeg) or passthrough
    |  circuit breakers, anti-bot pacing, quotas, crash recovery
    v
 DESTINATIONS (YouTube Shorts, Instagram Reels, X)
    |
    +--> ANALYTICS (xpst analytics: per-post snapshots -> ~/.xpst/analytics.db)
    +--> NOTIFICATIONS (Discord, Telegram)

 KNOWLEDGE BASE (xpst kb add: transcribe -> nuggets -> embed -> store)
```

**Pipeline per video:** fetch metadata → download source → encode per platform (or pass through) → upload → track state → notify.

Full diagrams and module layout: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Configuration

xPST loads configuration from `~/.xpst/config.yaml` with environment variable overrides (`XPST_*` prefix). Priority: environment variables > config file > defaults.

```yaml
# ~/.xpst/config.yaml (excerpt; xpst setup generates this)
accounts:
  tiktok:
    username: "your_tiktok_username"     # source only
    cookies_from_browser: true           # HD downloads
  youtube:
    enabled: true
    client_secrets: "~/.xpst/credentials/youtube_client_secrets.json"
  x:
    enabled: true
    cookies_file: "~/.xpst/credentials/x_cookies.json"
  instagram:
    enabled: true
    session_file: "~/.xpst/credentials/instagram_session.json"

video:
  encoding:
    youtube:   { resolution: 1920, bitrate: "8M",  maxrate: "10M", profile: "high", fps: 60 }
    instagram: { resolution: 1920, crf: 20,        maxrate: "10M", profile: "high", fps: 60 }
    x:         { resolution: 1920, bitrate: "10M", maxrate: "12M", profile: "high", fps: 60 }
    # resolution = long-edge target (orientation-aware); fps = cap, not force

rate_limits: { youtube: 5, instagram: 5, x: 5 }   # uploads per day
schedule:    { check_interval: 900 }               # seconds between checks
```

Every value can be overridden: `export XPST_RATE_LIMITS_YOUTUBE=10`. See `configs/example.yaml` for the full reference.

---

## Platform Risk and Terms of Service

Read this before connecting real accounts.

- **Instagram and X integrations use unofficial, reverse-engineered clients** ([instagrapi](https://github.com/subzeroid/instagrapi) and [twikit](https://github.com/d60/twikit)). These libraries automate the private APIs that the official apps use. **Using them may violate the platforms' Terms of Service and can result in rate limiting, challenges, shadow restrictions, or account suspension.** Only YouTube uses an official, sanctioned API (Data API v3 with your own OAuth project).
- **Downloads use [yt-dlp](https://github.com/yt-dlp/yt-dlp)**, including unauthenticated scraping for TikTok metadata. Scrape-based features can break without notice when platforms change.
- xPST ships conservative anti-bot defaults (randomized delays, time-of-day awareness, 5 uploads/day per platform, User-Agent rotation), but **no automation tool can guarantee your accounts will not be flagged. By using xPST you accept this risk.** Use accounts you can afford to lose while evaluating, and increase limits gradually.
- xPST keeps everything local: credentials go to your OS keychain (encrypted file fallback), state stays in `~/.xpst/`, and nothing is sent to any xPST server because there isn't one.

See the [Security Policy](SECURITY.md) and [Privacy](docs/PRIVACY.md) for the full posture, and [docs/X_AUTH_GUIDE.md](docs/X_AUTH_GUIDE.md) for X-specific guidance.

---

## Security

- **OS keychain storage** for credentials (macOS Keychain, Linux Secret Service, Windows Credential Manager), with encrypted `.enc` file fallback.
- **No passwords in config** — the config file references credential paths, never secrets.
- **Atomic state writes** (write-then-rename) and pidfile locking prevent corruption.
- **No cloud component** — there is no xPST service to leak to.

---

## Contributing

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
uv venv && uv pip install -e ".[dev]"
uv run pytest                  # keep it green (1198 tests)
uv run ruff check src tests
uv run mypy src/xpst
uv run lint-imports            # architectural import walls
```

Guidelines:

- **Tests required** for new features; the suite must stay green.
- **Type hints** (Python 3.10+) and Google-style docstrings on public APIs.
- **No new dependencies** without discussion in an issue first.
- **Conventional commits** (`feat:`, `fix:`, `docs:`, `test:`).
- See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## License

Licensed under either of:

- **MIT License**
- **Apache License 2.0**

at your option (`MIT OR Apache-2.0`). The combined license text is in [LICENSE](LICENSE); see [LICENSING_REPORT.md](LICENSING_REPORT.md) and [NOTICES.md](NOTICES.md) for the dependency licensing posture (including Qt/PySide6 LGPL notes for desktop bundles).

---

## Acknowledgments

xPST stands on the shoulders of these excellent open-source projects:

- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — video downloading
- **[instagrapi](https://github.com/subzeroid/instagrapi)** — Instagram client
- **[twikit](https://github.com/d60/twikit)** — X/Twitter client
- **[google-api-python-client](https://github.com/googleapis/google-api-python-client)** — YouTube Data API v3
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** — transcription
- **[fastembed](https://github.com/qdrant/fastembed)** / **[LanceDB](https://github.com/lancedb/lancedb)** — embeddings and vector store
- **[FFmpeg](https://ffmpeg.org)** — video encoding
- **[Click](https://github.com/pallets/click)** / **[Rich](https://github.com/Textualize/rich)** — CLI
- **[PySide6](https://wiki.qt.io/Qt_for_Python)** — desktop GUI
