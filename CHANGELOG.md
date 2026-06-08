# Changelog

All notable changes to xPST will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- 793 tests (unit + integration + E2E)
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
