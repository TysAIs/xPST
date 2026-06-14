# Changelog

All notable changes to xPST will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0rc2] - 2026-06-14

### Fixed
- Hardened release gates for RC2 after the knowledge-base handoff fixes.
- Preserved FFmpeg preflight behavior while keeping existing run-command failure paths covered.
- Verified clean install, installed MCP stdio, desktop QML, and release metadata gates.

## [Unreleased]

### Knowledge Base (new subsystem)
- Personal content knowledge base under `src/xpst/knowledge/` (optional `xpst[knowledge]` extra): ingest a local file or URL, transcribe with faster-whisper, extract cited knowledge "nuggets" (strict-JSON extraction with source URL + timestamps), embed via fastembed or a local embedding endpoint, and store locally
- Stores: JSON nugget store plus a LanceDB vector store
- Organize pipeline: cluster nuggets into knowledge areas by embedding similarity, tag difficulty, and order areas into a course outline
- CLI: `xpst kb add|query|organize|areas|course|doctor` (doctor is a read-only workspace health check covering deps, store integrity, queue state, and embedding consistency)
- Durable ingestion queue with stale-claim requeue for background processing
- 4 MCP tools (`kb_add`, `kb_query`, `kb_organize`, `kb_areas`) bringing the MCP server to 13 tools total; heavy deps stay lazy-imported off the cold path (enforced by import-linter contracts)
- Semantic query surface shared by CLI and MCP: `kb query` embeds the query and vector-searches the store, with automatic substring fallback when embeddings are unavailable; every hit carries provenance (source URL, timestamps) and a similarity score

### Video Fidelity Overhaul
- Orientation-aware scaling targeting a 1920px long edge (fixes vertical 1080x1920 sources previously being downscaled to sub-SD width)
- Frame rate is now a cap (`-fpsmax 60`), never a force: 60fps sources keep 60fps
- Instagram profile modernized to Reels-grade 1080p+ (long edge 1920, CRF 20, High@4.0, 10M maxrate) from the obsolete 720p/CRF23/Main profile
- Smart passthrough: a compliance probe skips re-encoding entirely when the source already satisfies the platform profile
- yt-dlp downloads now select split video+audio streams (`bv*+ba`) with `--merge-output-format mp4` instead of pre-muxed lower-quality files

### Analytics Foundation
- Instagram collection now uses the real instagrapi API (`login_by_sessionid`/`load_settings`, `insights_media`, `media_info`); the previous code called methods that do not exist in instagrapi, so Instagram analytics was permanently empty and only mocks kept tests green
- Persistent per-post snapshot store: append-only SQLite at `~/.xpst/analytics.db` keyed on `(platform, post_id, captured_at)` — the foundation for real trend history and future knowledge-base performance weighting
- Instagram insights parsed defensively: shares/saves require a Business/Creator account, with public `media_info` counts as fallback

### Engine Correctness (double-post guard cluster)
- `_process_video` no longer hardcodes the TikTok source; the requested source is threaded through
- Clearing the dead-letter queue no longer deletes posted-video history (re-post risk eliminated)
- `source_platform` and content hash are now recorded uniformly across the unidirectional and bidirectional flows, so cross-flow deduplication and source-scoped backfill work
- Deduplication uses file content fingerprints instead of caption-only hashes
- Retry paths verify post existence before re-uploading after ambiguous failures
- `post_manual` performs an already-posted check with a stable video ID

### CI
- Consolidated to a single GitHub Actions workflow; duplicate weaker `test.yml` removed; `feat/knowledge-base` added to push triggers

### Documentation
- README rewritten: four-pillar framing, honest per-platform analytics capability matrix (story-reposts collectible on zero platforms; IG shares/saves need a Business account; TikTok metrics are unauthenticated scrape), per-platform video constraints, TikTok corrected to source-only everywhere, verified counts (25 CLI commands, 13 MCP tools), PyPI install gated behind a published note, platform-risk/ToS disclosure section
- docs/MCP_TOOLS.md regenerated from the live registry: all 13 tools documented with arguments, examples, and a real-account guardrails section
- docs/ENTERPRISE_READINESS.md marked as a superseded historical snapshot
- Added .github/PULL_REQUEST_TEMPLATE.md and CODE_OF_CONDUCT.md (Contributor Covenant 2.1)

## [0.1.0] - 2026-06-08

### Core Engine
- Bidirectional cross-posting: TikTok → YouTube Shorts, X/Twitter, Instagram Reels
- Reverse cross-posting: post from any platform, auto-distribute to others
- Multi-file carousel support (multiple --video flags)
- Platform-specific video encoding (YouTube 1080p 8Mbps, Instagram 720p CRF23, X 1080p 10Mbps)
- Content hash deduplication with 7 double-post prevention safeguards
- Circuit breaker pattern with auto-recovery (fixed silent failure bug)
- Exponential backoff retry (3 attempts)
- Atomic state persistence with thread-safe locking
- Sleep/wake catch-up for Mac laptop users

### CLI (22 commands)
- `xpst run` — one-time check and post (--bidirectional, --dry-run)
- `xpst watch` — continuous monitoring mode
- `xpst post` — manual video/carousel posting
- `xpst schedule add/list/remove/run/install` — scheduled posts with recurring (daily/weekly/monthly)
- `xpst config show/set/validate/export/import/fix` — full config management with diff display
- `xpst auth` — platform authentication (YouTube OAuth, X cookies, Instagram session)
- `xpst connect` — streamlined account setup wizard
- `xpst health` — platform connectivity test
- `xpst status` — health status overview
- `xpst analytics/export` — cross-platform analytics with CSV/JSON export
- `xpst backfill` — retry failed posts
- `xpst delete` — delete posted videos from platforms
- `xpst plugins list/docs` — plugin management
- `xpst build` — PyInstaller standalone executable builder
- `xpst update` — dependency update manager
- `xpst version` — version and dependency info
- `xpst dashboard` — web API server (FastAPI/uvicorn)
- `xpst app` — native desktop application (PySide6 QML)
- `xpst mcp` — MCP server for AI agent integration
- `xpst logs` — log viewer
- `xpst setup` — interactive first-time setup wizard
- All commands support `--json`, `--dry-run`, `--quiet` flags
- Meaningful exit codes (0/1/2/3/4/10)
- TTY detection (auto --json when piped)

### Desktop App (PySide6 QML)
- Native macOS/Windows/Linux application with system tray
- Dashboard page: metric cards, platform health, recent posts from real data
- Content library: search, filter, sort (newest/oldest/platform/status), grid/list toggle
- Content pagination with keyboard navigation (Ctrl+Left/Right)
- Video preview with embedded MediaPlayer, seek bar, error handling
- Batch post with per-platform caption editor
- Drag-and-drop video posting with caption prompt
- Analytics: Canvas bar charts, comparison mode (this week vs last), date range picker
- Connect page: platform health monitoring, X cookie paste dialog
- Settings: platform toggles, rate limits, encoding preview with side-by-side comparison
- Schedule page: calendar view with clickable days, month navigation
- About page: version, dependencies with license badges, git changelog
- Developer section: MCP server start/stop, tools list
- Keyboard shortcut customization (editable key bindings)
- Dark/light mode toggle with full theme switching
- Notification bell with history model
- Sidebar quick stats (post count + health indicator with pulse animation)
- Splash screen with progress stages (--no-splash flag)
- Window state persistence (QSettings, multi-monitor support)
- Crash recovery dialog with retry
- Upload progress overlay with per-platform progress bars
- Content deduplication badges with confidence scores
- 63 Accessible.name/role annotations for screen readers

### MCP Server (8 tools, 3 resources)
- `post_video` — post a video to platforms
- `crosspost_new` — check for new videos and cross-post
- `check_status` — get system health status
- `list_platforms` — list configured platforms
- `get_analytics` — retrieve engagement analytics
- `delete_post` — delete a post from platforms
- `health_check` — test platform connectivity
- `get_logs` — retrieve recent log entries
- Resources: `xpst://config`, `xpst://state`, `xpst://health`

### Security
- OS keychain integration (macOS Keychain, Windows Credential Locker, Linux Secret Service)
- Encrypted file fallback for credentials
- Dashboard password: SHA-256 hashed
- Plugin sandboxing: RestrictedPlugin blocks os/sys/subprocess imports
- No credentials in logs or config files
- ToS compliance warnings before every X/Instagram upload

### Plugin System
- Auto-discovery from ~/.xpst/plugins/
- Hot-reload: FileSystemWatcher monitors for changes
- Dependency management: auto-install requires from register()
- Sandbox mode: restricted __builtins__ for untrusted plugins
- Documentation generator: `xpst plugins docs`

### Reliability
- Rate limiting: configurable per-platform daily limits (default 5/day)
- Anti-bot protection: random delays (12-300s), time-of-day (8am-11pm), UA rotation
- Quota tracking with persistent state
- Dead letter queue for failed uploads
- Prometheus metrics endpoint

### Build & Release
- PyInstaller build scripts (macOS .spec, Windows .spec, build.sh)
- Ad-hoc code signing script (scripts/sign_macos.sh)
- Release automation script (scripts/release.sh)
- GitHub Actions CI with pytest-cov (--cov-fail-under=80)
- PyPI-ready wheel (pip install xpst)

### Documentation
- README.md with badges, Quick Start, For AI Agents section
- docs/AGENT_GUIDE.md — CLI --json + MCP + Python API guide
- docs/MCP_TOOLS.md — full MCP tools reference
- docs/INSTALL.md — comprehensive install guide (pip/git/Docker)
- docs/ARCHITECTURE.md — system architecture with ASCII diagrams
- docs/OPEN_SOURCE_INTEGRATIONS.md — dependency audit
- docs/X_AUTH_GUIDE.md — X/Twitter cookie authentication guide
- CONTRIBUTING.md — contributor guide
- CHANGELOG.md — this file

### Testing
- 866 tests (unit + integration + E2E)
- Integration tests: full pipeline, duplicate prevention, circuit breaker, rate limits, health check
- CLI E2E tests: version, config, schedule, plugins via CliRunner
- Cross-platform tests: Windows/macOS/Linux path handling
- Edge case tests: auth expiry, video formats, network failures, config corruption

### Dependencies (all free, open source)
- yt-dlp (Unlicense), instagrapi (MIT), twikit (MIT)
- Google API client (Apache-2.0), authlib (BSD-3), httpx (BSD-3)
- click (BSD-3), rich (MIT), pyyaml (MIT)
- FastAPI (MIT), uvicorn (BSD-3)
- keyring (MIT), structlog (Apache-2.0), prometheus-client (Apache-2.0)
- Optional: PySide6 (LGPL-3.0), MCP (MIT), pywebview (BSD-3)
