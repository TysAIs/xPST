1|<p align="center">
2|  <img src="assets/logos/banner-logo.png" alt="xPST — Cross-Posting Suite" width="700">
3|</p>
4|
5|<p align="center">
6|  <strong>Post once, publish everywhere. Enterprise-grade, local-first, open-source cross-posting for short-form video.</strong>
7|</p>
8|
9|<p align="center">
10|  <a href="https://www.python.org"><img alt="Python" src="https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13-blue"></a>
11|  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-green"></a>
12|  <a href="#"><img alt="Tests" src="https://img.shields.io/badge/tests-1427%20passing-brightgreen"></a>
13|  <a href="#"><img alt="Platform" src="https://img.shields.io/badge/platform-Linux%20|%20macOS%20|%20Windows-lightgrey"></a>
14|  <a href="#"><img alt="MCP Server" src="https://img.shields.io/badge/MCP-server-orange"></a>
15|  <a href="#"><img alt="Desktop" src="https://img.shields.io/badge/desktop-PySide6%2FQML-blueviolet"></a>
16|</p>
17|
18|---
19|
20|## Table of Contents
21|
22|- [What is xPST](#what-is-xpst)
23|- [Features](#features)
24|- [Quick Start](#quick-start)
25|- [Installation](#installation)
26|- [CLI Reference](#cli-reference)
27|- [Desktop App Guide](#desktop-app-guide)
28|- [MCP Integration Guide](#mcp-integration-guide)
29|- [Platform Setup Guides](#platform-setup-guides)
30|- [Configuration Reference](#configuration-reference)
31|- [Architecture Overview](#architecture-overview)
32|- [Development Guide](#development-guide)
33|- [Security Practices](#security-practices)
34|- [License](#license)
35|- [Acknowledgments](#acknowledgments)
36|
37|---
38|
39|## What is xPST
40|
41|**xPST** (Cross-Posting Suite) is a local-first, open-source automation tool that takes a creator's short-form video from one source platform and republishes it — at full native fidelity — to every other platform they own. It tracks per-post performance across all of them in one place, and feeds the creator's published content into a personal knowledge base that any connected AI agent can semantically query.
42|
43|It runs three ways:
44|- **Desktop GUI** — PySide6/QML native app with 8 pages
45|- **CLI** — 27+ commands covering the entire workflow
46|- **MCP server** — 16 tools so AI agents can drive the entire product
47|
48|No subscriptions, no cloud servers, no vendor lock-in. Your content and credentials never leave your machine.
49|
50|---
51|
52|## Features
53|
54|### Core Cross-Posting
55|- **Full-fidelity fan-out** — One source video downloads once and uploads to every connected destination, with orientation-aware encoding that never degrades quality
56|- **Bidirectional cross-posting** — Monitor ALL connected sources for new content and distribute to every connected destination (not just one direction)
57|- **Smart passthrough** — A probe checks whether the source already satisfies the platform profile and skips the re-encode entirely, saving a generation loss
58|- **Circuit breakers** — One platform failing never blocks the others; repeat offenders are disabled and recover automatically
59|- **Crash recovery** — Partially-completed uploads are detected and queued for retry on next launch
60|
61|### Unified Analytics
62|- **Per-post engagement metrics** — Views, likes, comments, shares, and platform-specific signals in one normalized schema
63|- **Persistent history** — Every collection run appends per-post snapshots to a local SQLite store (`~/.xpst/analytics.db`), so trends come from real history
64|- **Honest capability matrix** — Clear documentation of what each platform actually exposes (and what it does not)
65|- **Export** — Analytics data exportable to JSON or CSV
66|
67|### Personal Content Knowledge Base
68|- **Transcription** — Every ingested video is transcribed via faster-whisper
69|- **Cited knowledge nuggets** — Content distilled into cited knowledge fragments with provenance (source URL, timestamps)
70|- **Vector search** — Semantic search over your content with LanceDB embeddings (with substring-match fallback)
71|- **Knowledge areas** — Organize nuggets into areas, order by difficulty into a course outline
72|- **Agent-queryable** — Query your back catalog from the CLI or any AI agent over MCP
73|
74|### Three Drivable Surfaces
75|- **Desktop GUI** — PySide6/QML app with Dashboard, Compose, Content, Analytics, Connect, Schedule, Settings, and About pages + DetailPanel
76|- **CLI** — 27+ Click-based commands with `--json` output, `--dry-run` mode, and meaningful exit codes
77|- **MCP server** — 16 tools (12 `xpst_*` + 4 `kb_*`) for AI agent integration
78|
79|### Enterprise Hardening
80|- **Encrypted credentials** — OS keychain storage (macOS Keychain, Linux Secret Service, Windows Credential Manager) with encrypted `.enc` file fallback (Fernet + scrypt)
81|- **Atomic state writes** — Write-then-rename and pidfile locking prevent corruption
82|- **Anti-bot pacing** — Randomized delays, time-of-day awareness, rate limits, User-Agent rotation
83|- **Quota management** — Configurable daily upload limits per platform
84|- **Dead-letter queue** — Failed uploads tracked and retryable
85|- **Diagnostics bundles** — Redacted export for support
86|- **State backup/restore** — Export, import, and snapshot the posting state
87|- **Config validation** — Detect and auto-fix common configuration issues
88|- **Plugin system** — Extend with custom uploaders and sources
89|- **i18n** — Translations supported via `~/.xpst/translations/`
90|
91|---
92|
93|## Quick Start
94|
95|Three commands to get going:
96|
97|```bash
98|# 1. Install
99|git clone https://github.com/TysAIs/xPST.git && cd xPST
100|uv venv && uv pip install -e ".[full]"
101|
102|# 2. Setup (interactive wizard connects your platforms)
103|xpst setup
104|
105|# 3. Run (check for new videos and cross-post them)
106|xpst run
107|```
108|
109|That's it. Your videos are now cross-posted to every connected platform.
110|
111|---
112|
113|## Installation
114|
115|### Prerequisites
116|
117|- **Python 3.10+** (3.11–3.13 recommended)
118|- **FFmpeg** on PATH (or set `XPST_FFMPEG_PATH`)
119|- **uv** (recommended) or plain pip
120|
121|### Option 1: Install from source with uv (recommended)
122|
123|```bash
124|git clone https://github.com/TysAIs/xPST.git
125|cd xPST
126|uv venv && uv pip install -e ".[full]"
127|xpst setup
128|xpst run
129|```
130|
131|### Option 2: Install from source with pip
132|
133|```bash
134|git clone https://github.com/TysAIs/xPST.git
135|cd xPST
136|python3 -m venv .venv && source .venv/bin/activate
137|pip install -e ".[full]"
138|xpst setup
139|xpst run
140|```
141|
142|### Option 3: Minimal install (CLI only, no desktop/KB)
143|
144|```bash
145|pip install -e .
146|```
147|
148|### Optional extras
149|
150|| Extra | What it adds |
151||-------|-------------|
152|| `mcp` | MCP server (`xpst mcp`, `xpst-mcp`) |
153|| `knowledge` | KB transcription/embeddings/LanceDB |
154|| `pyside6` | Native desktop GUI (PySide6/QML) |
155|| `dashboard` | Web dashboard (FastAPI + WebSocket) |
156|| `desktop` | pywebview fallback for desktop |
157|| `windows` | Windows-specific pywin32/winshell |
158|| `dev` | pytest, ruff, mypy, import-linter |
159|| `full` | Everything (`mcp,desktop,pyside6,dashboard,windows,knowledge`) |
160|
161|### Once published to PyPI
162|
163|```bash
164|pip install "xpst[full]"   # not yet available; watch the Releases page
165|```
166|
167|### Docker (planned)
168|
169|A Dockerfile is on the roadmap for containerized CLI and MCP server usage. For now, install from source.
170|
171|---
172|
173|## CLI Reference
174|
175|xPST provides 27+ top-level commands. Run `xpst --help` for the full list. Most commands accept `--json` for machine-readable output, and the CLI auto-enables JSON mode when stdout is piped (non-TTY).
176|
177|### Setup & Accounts
178|
179|| Command | Description |
180||---------|-------------|
181|| `xpst setup` | Interactive first-time setup wizard (connects platforms, writes config) |
182|| `xpst connect [PLATFORM]` | Streamlined account connection wizard; use `--test` to test existing |
183|| `xpst auth [PLATFORM]` | Authenticate with a specific platform (youtube/x/instagram/tiktok) |
184|| `xpst auth status` | Show authentication and quota status for all platforms |
185|| `xpst config show` | Display current configuration as YAML (sensitive values masked) |
186|| `xpst config set KEY VALUE` | Set a config value using dotted keys (e.g. `rate_limits.youtube 10`) |
187|| `xpst config validate` | Validate configuration for errors (exit 0 if valid, 4 if invalid) |
188|| `xpst config fix` | Detect and auto-fix common configuration issues |
189|| `xpst config export FILE` | Export configuration to a file |
190|| `xpst config import FILE` | Import configuration (merge or replace, with diff preview) |
191|| `xpst readiness` | Show first-run readiness and next actions; use `--fix` to create missing dirs |
192|| `xpst providers` | Show supported source and destination providers with capabilities |
193|
194|### Core Posting
195|
196|| Command | Description |
197||---------|-------------|
198|| `xpst run` | One-shot: check for new videos from a source and cross-post them |
199|| `xpst run --source all` | Bidirectional: check ALL sources and distribute to ALL destinations |
200|| `xpst run --dry-run` | Show what would happen without uploading |
201|| `xpst watch` | Continuous monitoring loop (runs until Ctrl+C) |
202|| `xpst watch --interval 300` | Check every 300 seconds (default: from config) |
203|| `xpst post -v VIDEO -c CAPTION` | Manually post a video file; use multiple `-v` for carousel |
204|| `xpst post -v v.mp4 -c 'text' -p youtube,x` | Post to specific platforms only |
205|| `xpst backfill` | Retry failed or incomplete posts from history |
206|| `xpst backfill --dry-run` | Show what would be backfilled without uploading |
207|| `xpst delete VIDEO_ID` | Delete a posted video from platforms; use `--platform` to target one |
208|| `xpst schedule add FILE --caption TEXT --at TIME` | Schedule a post for later publishing |
209|| `xpst schedule list` | List all scheduled posts |
210|| `xpst schedule remove ID` | Remove a scheduled post by ID |
211|| `xpst schedule run` | Process all due scheduled posts (typically called by cron) |
212|| `xpst schedule install` | Install an OS-level scheduler (macOS LaunchAgent / Linux cron / Windows Task) |
213|
214|### Analytics & Observability
215|
216|| Command | Description |
217||---------|-------------|
218|| `xpst analytics` | Show cross-platform analytics summary (views, likes, comments, shares) |
219|| `xpst analytics --refresh` | Force refresh (ignore cache) |
220|| `xpst analytics export -o FILE` | Export analytics to JSON or CSV (`--format csv`) |
221|| `xpst status` | Show cross-posting statistics and health status |
222|| `xpst health` | Test connectivity to all platforms (no uploads) |
223|| `xpst logs` | View recent logs (last 50 lines) |
224|| `xpst diagnostics` | Export a redacted local diagnostics bundle (zip) |
225|| `xpst failures list` | List failed uploads from the dead-letter queue |
226|| `xpst failures retry VIDEO_ID --platform P` | Retry one failed upload by re-posting its source file |
227|
228|### Knowledge Base
229|
230|| Command | Description |
231||---------|-------------|
232|| `xpst kb add SOURCE` | Ingest a local file or URL: transcribe, extract nuggets, embed, store |
233|| `xpst kb query TEXT` | Semantic search over your content (substring fallback, cited) |
234|| `xpst kb organize` | Discover areas, tag difficulty, and assign nuggets |
235|| `xpst kb areas` | List discovered knowledge areas in course order (beginner → advanced) |
236|| `xpst kb course` | Emit the organized, cited outline |
237|| `xpst kb doctor` | Read-only health check of the knowledge workspace |
238|| `xpst kb reembed` | Re-embed all nuggets with the configured embedding model |
239|| `xpst kb migrate-store` | Migrate the store format |
240|
241|### Surfaces
242|
243|| Command | Description |
244||---------|-------------|
245|| `xpst app` | Launch native desktop app (PySide6/QML); appears in your dock |
246|| `xpst dashboard` | Launch local web API dashboard at `http://localhost:8080` |
247|| `xpst mcp` | Start MCP (Model Context Protocol) server over stdio |
248|
249|### State Management
250|
251|| Command | Description |
252||---------|-------------|
253|| `xpst state export OUTPUT` | Export state.json to OUTPUT (validated copy) |
254|| `xpst state import SOURCE` | Restore state.json (current state backed up first) |
255|| `xpst state backup` | Snapshot state.json into `~/.xpst/backups/` with rotation |
256|
257|### Maintenance
258|
259|| Command | Description |
260||---------|-------------|
261|| `xpst update` | Update xPST dependencies to latest versions |
262|| `xpst update --check` | Check for updates without installing |
263|| `xpst update --components` | Show app, helper, and provider metadata update status |
264|| `xpst version` | Show xPST version and all dependency versions |
265|| `xpst plugins list` | List installed plugins |
266|| `xpst plugins docs` | Generate markdown documentation for installed plugins |
267|| `xpst build` | Build a standalone executable using PyInstaller |
268|| `xpst build --target macos` | Cross-compile for a different OS via Docker |
269|
270|### Global Options
271|
272|| Option | Description |
273||--------|-------------|
274|| `--config / -c` | Path to config file |
275|| `--verbose / -v` | Enable verbose (DEBUG) logging |
276|| `--quiet / -q` | Suppress decorative output |
277|| `--json` | Output in JSON format (auto-enabled when piped) |
278|| `--version` | Show version |
279|
280|### Exit Codes
281|
282|| Code | Meaning |
283||------|---------|
284|| `0` | Success |
285|| `1` | General error |
286|| `2` | Authentication failure |
287|| `3` | Rate limit exceeded |
288|| `4` | Configuration error |
289|| `10` | Platform unavailable |
290|
291|### Dry-Run Mode
292|
293|All posting commands (`run`, `post`, `backfill`) support `--dry-run` to preview what would happen without uploading. The CLI shows what would be posted and to which platforms, then exits without making any network calls.
294|
295|### JSON Output Mode
296|
297|All commands accept `--json` for machine-readable output. Additionally, the CLI **auto-enables JSON mode when stdout is not a TTY** (piped to another process), making it ideal for scripting and agent integration:
298|
299|```bash
300|xpst status --json | jq '.stats.posted'
301|xpst run --dry-run --json | jq '.videos[].video_id'
302|```
303|
304|---
305|
306|## Desktop App Guide
307|
308|The native desktop app is built with PySide6/QML and provides a polished, Apple-like UI with light/dark mode, the Inter font, and full accessibility support.
309|
310|```bash
311|xpst app          # launch the native desktop app
312|xpst app --no-splash  # skip the splash screen
313|```
314|
315|### 8 Pages
316|
317|| Page | What it does |
318||------|-------------|
319|| **Dashboard** | Overview of posted content, per-platform health, and quota status at a glance |
320|| **Compose** | Compose a new post: select a video file, write a caption, choose target platforms, and submit |
321|| **Content** | Browse your content library of posted videos with thumbnails, captions, and per-platform status |
322|| **Analytics** | View cross-platform engagement metrics (views, likes, comments, shares) with trend history |
323|| **Connect** | Connect and manage your social media platform accounts (YouTube, Instagram, X, TikTok) |
324|| **Schedule** | Manage scheduled posts: create, view, and remove upcoming and recurring posts |
325|| **Settings** | Customize xPST settings: encoding profiles, rate limits, notifications, and preferences |
326|| **About** | Version info, dependency versions, links to docs and source, acknowledgments |
327|
328|### DetailPanel
329|
330|The DetailPanel is a slide-out panel that shows the full details of a selected post: all per-platform upload results, URLs, error messages, analytics metrics, and timestamps.
331|
332|### First-Run Welcome
333|
334|On first launch, a welcome dialog guides you to the Connect page to set up your platform accounts. The app detects whether `~/.xpst/config.yaml` exists and routes you accordingly.
335|
336|> **Screenshots:** Screenshots with demo data are coming with the first release. Run `xpst app` to see the UI live, or see [docs/TUTORIAL_APP.md](docs/TUTORIAL_APP.md) for a full walkthrough.
337|
338|---
339|
340|## MCP Integration Guide
341|
342|xPST is designed to be driven end-to-end by AI agents over the [Model Context Protocol](https://modelcontextprotocol.io).
343|
344|### Setup
345|
346|```bash
347|pip install "xpst[mcp]"
348|```
349|
350|Add to your MCP client config (Claude Desktop, Claude Code, etc.):
351|
352|```json
353|{
354|  "mcpServers": {
355|    "xpst": {
356|      "command": "xpst-mcp",
357|      "transport": "stdio"
358|    }
359|  }
360|}
361|```
362|
363|### 16 Tools
364|
365|**9 posting/ops tools:**
366|
367|| Tool | Description |
368||------|-------------|
369|| `xpst_providers` | List supported content sources and posting destinations with capabilities |
370|| `xpst_config_show` | Display current configuration (sensitive values masked) |
371|| `xpst_auth_status` | Show authentication status for all platforms |
372|| `xpst_status` | Show cross-posting statistics and health status |
373|| `xpst_health` | Test connectivity to all platforms and sources (no uploads) |
374|| `xpst_run` | Check for new videos and cross-post them (supports `dry_run`, `source`, `max_posts`) |
375|| `xpst_post` | Manually post a local video file or carousel to platforms |
376|| `xpst_backfill` | Retry failed or incomplete posts from history |
377|| `xpst_delete` | Delete a post record from state |
378|
379|**3 scheduling/analytics tools:**
380|
381|| Tool | Description |
382||------|-------------|
383|| `xpst_analytics` | Per-post and per-platform engagement metrics with persistent history (`live=false` for offline, `live=true` to refresh from APIs) |
384|| `xpst_schedule_list` | List scheduled posts (pending, completed, failed) with times and targets |
385|| `xpst_schedule_add` | Schedule a post: local video + caption + ISO-8601 time + optional platform list and repeat rule |
386|
387|**4 knowledge-base tools:**
388|
389|| Tool | Description |
390||------|-------------|
391|| `kb_add` | Ingest a local file or URL into the knowledge base (transcribe, extract nuggets, embed, store) |
392|| `kb_query` | Return stored knowledge nuggets whose text matches the query (semantic search with cited provenance) |
393|| `kb_organize` | Discover areas, tag difficulty, and assign nuggets |
394|| `kb_areas` | List discovered knowledge areas in course order (beginner → advanced) |
395|
396|### Security Guardrails
397|
398|Mutating tools (`xpst_run`, `xpst_post`, `xpst_backfill`, `xpst_delete`, `xpst_schedule_add`, `kb_add`, `kb_organize`) post to or mutate **real accounts**. Two environment-variable tiers control them:
399|
400|- **`XPST_MCP_READONLY=1`** — Blocks all mutating tools entirely (read-only mode)
401|- **`XPST_MCP_REQUIRE_CONFIRM=1`** — Requires `confirm: true` in the arguments (consent tier)
402|
403|### Recommended Agent Cold-Start Flow
404|
405|```
406|xpst_providers → xpst_auth_status → xpst_health → xpst_run(dry_run: true) → live run after user confirmation → kb_query
407|```
408|
409|Metadata tools (`xpst_providers`, `xpst_config_show`, `xpst_auth_status`) are lightweight and never start the posting engine.
410|
411|See [docs/TUTORIAL_MCP.md](docs/TUTORIAL_MCP.md) for a full MCP walkthrough with every tool's schema and examples, and [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) for the complete schema reference.
412|
413|---
414|
415|## Platform Setup Guides
416|
417|### YouTube (OAuth 2.0 — official API)
418|
419|xPST uses the official YouTube Data API v3 with your own OAuth project:
420|
421|1. Go to [Google Cloud Console](https://console.cloud.google.com)
422|2. Create or select a project
423|3. Enable **YouTube Data API v3**
424|4. Create OAuth 2.0 credentials (Desktop application type)
425|5. Download `client_secrets.json`
426|6. Save to `~/.xpst/credentials/youtube_client_secrets.json`
427|7. Run `xpst auth youtube` to complete authentication
428|
429|The OAuth token is stored in your OS keychain (encrypted file fallback).
430|
431|### Instagram (session-based)
432|
433|Instagram uses [instagrapi](https://github.com/subzeroid/instagrapi) for session-based uploads:
434|
435|1. Log into instagram.com in your browser
436|2. Open DevTools → Application → Cookies
437|3. Find the cookie named `sessionid`
438|4. Copy its value
439|5. Create a JSON file at `~/.xpst/credentials/instagram_session.json`:
440|   ```json
441|   {
442|     "authorization_data": {
443|       "sessionid": "YOUR_SESSION_ID"
444|     }
445|   }
446|   ```
447|6. Run `xpst auth instagram`
448|
449|> **Note:** Instagram shares and saves require a Business or Creator account (Instagram insights API). On a personal account you get views (play count), likes, and comments.
450|
451|### X / Twitter (cookie-based)
452|
453|X uses [twikit](https://github.com/d60/twikit) for cookie-based uploads:
454|
455|**Option 1: Browser cookie export**
456|1. Log into x.com in your browser
457|2. Export cookies using a cookie editor extension
458|3. Save to `~/.xpst/credentials/x_cookies.json`
459|
460|**Option 2: twikit login**
461|```bash
462|python3 -c "import twikit, asyncio; asyncio.run(twikit.Client('en-US').login('USER', 'PASS').save_cookies('cookies.json'))"
463|mv cookies.json ~/.xpst/credentials/x_cookies.json
464|```
465|
466|Then run `xpst auth x`.
467|
468|See [docs/QUICKSTART.md](docs/QUICKSTART.md) for X-specific guidance.
469|
470|### TikTok (source monitoring only)
471|
472|> **TikTok is a source only.** xPST does not post to TikTok: there is no official self-serve upload API for this use case and no stable unofficial one. TikTok is fully supported for downloading your own content as a source.
473|
474|TikTok doesn't require authentication for basic downloads. For HD quality without watermarks:
475|
476|**Option 1: Browser cookies (recommended)**
477|1. Log into tiktok.com in your browser
478|2. Set `cookies_from_browser: true` in config.yaml
479|3. xPST will automatically use your browser cookies
480|
481|**Option 2: Export cookies manually**
482|1. Use a cookie editor extension to export cookies
483|2. Set `cookies_file` in config.yaml
484|
485|### Local Files
486|
487|Use local folders as a source for manual posting and carousels:
488|
489|```bash
490|xpst post -v ./my-video.mp4 -c "My caption" -p youtube,instagram,x
491|xpst run --source local
492|```
493|
494|---
495|
496|## Configuration Reference
497|
498|xPST loads configuration from `~/.xpst/config.yaml` with environment variable overrides (`XPST_*` prefix). Priority: environment variables > config file > defaults.
499|
500|```yaml
501|