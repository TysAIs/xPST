# xPST Documentation

**Enterprise-grade cross-posting for short-form video**

Automatically distribute TikTok videos to YouTube Shorts, X/Twitter, and Instagram Reels.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Authentication](#authentication)
5. [Usage](#usage)
6. [CLI Reference](#cli-reference)
7. [MCP Server](#mcp-server)
8. [Dashboard API](#dashboard-api)
9. [Architecture](#architecture)
10. [Security](#security)
11. [Troubleshooting](#troubleshooting)
12. [Contributing](#contributing)

---

## Quick Start

```bash
# Install
pip install xpst

# Run interactive setup wizard
xpst setup

# Check for new videos and cross-post
xpst run

# Or watch continuously
xpst watch
```

---

## Installation

### From PyPI (recommended)

```bash
pip install xpst
```

### From source

```bash
git clone https://github.com/yourusername/xpst
cd xpst
pip install -e .
```

### System requirements

- Python 3.11+
- FFmpeg (for video encoding)
- yt-dlp (installed automatically)

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Windows
winget install Gyan.FFmpeg
```

---

## Configuration

Configuration is stored in `~/.xpst/config.yaml`. The setup wizard (`xpst setup`) creates this file interactively.

### Configuration Structure

```yaml
# Accounts for each platform
accounts:
  tiktok:
    username: "your_username"           # TikTok username to fetch from
    cookies_from_browser: false          # Use browser cookies for auth
    cookies_file: "~/.xpst/credentials/tiktok_cookies.json"

  youtube:
    enabled: true
    client_secrets: "~/.xpst/credentials/youtube_client_secrets.json"
    token_file: "~/.xpst/credentials/youtube_token.json"

  x:
    enabled: true
    cookies_file: "~/.xpst/credentials/x_cookies.json"

  instagram:
    enabled: true
    session_file: "~/.xpst/credentials/instagram_session.json"
    username: "your_username"

# Video processing settings
video:
  download_dir: "~/.xpst/downloads"
  cleanup_after_post: false
  encoding:
    youtube:
      passthrough: false
      resolution: 1080
      bitrate: "8M"
      profile: "high"
      gop: 15
      fps: 30
    instagram:
      resolution: 720
      crf: 23
      maxrate: "3500k"
      profile: "main"
      level: "3.0"
      gop: 72
      fps: 30
    x:
      resolution: 1080
      bitrate: "10M"
      profile: "high"
      level: "4.0"
      gop: 90
      fps: 30

# Reliability settings
reliability:
  max_retries: 3
  retry_backoff: 2
  circuit_breaker_threshold: 5
  circuit_breaker_reset: 3600

# Anti-bot settings
anti_bot:
  enabled: true
  min_delay: 2.0
  max_delay: 10.0
  jitter: 0.3

# Scheduling
schedule:
  check_interval: 900          # 15 minutes
  enabled: true
  catch_up_max_hours: 24

# Monitoring
monitoring:
  log_level: "INFO"
  log_file: "~/.xpst/logs/xpst.log"
  log_rotation: "10 MB"
  healthcheck_port: 8080
  enable_metrics: true

# Notifications (optional)
notifications:
  enabled: false
  discord_webhook_url: ""
  telegram_bot_token: ""
  telegram_chat_id: ""
  notify_on_error: true
  notify_on_post: false

# Dashboard auth
dashboard_username: "admin"
dashboard_password_hash: ""  # bcrypt hash, set via xpst config set
```

### Environment Variables

All settings can be overridden with `XPST_` prefix:

```bash
XPST_ACCOUNTS_TIKTOK_USERNAME=myuser
XPST_VIDEO_DOWNLOAD_DIR=/data/videos
XPST_MONITORING_LOG_LEVEL=DEBUG
```

---

## Authentication

### TikTok

Uses yt-dlp with browser cookies:

```bash
# Option 1: Browser cookies (automatic)
xpst auth tiktok

# Option 2: Manual cookies file
# Export cookies from browser and save to ~/.xpst/credentials/tiktok_cookies.json
```

### YouTube

OAuth2 with Google Cloud Console:

```bash
xpst auth youtube
# Opens browser for Google OAuth consent
# Creates client_secrets.json and token.json
```

### X/Twitter

Cookie-based via twikit:

```bash
xpst auth x
# Enter username/password, saves cookies to x_cookies.json
```

### Instagram

Session-based via instagrapi:

```bash
xpst auth instagram
# Enter username/password, saves session file
```

---

## Usage

### One-time run

```bash
# Standard TikTok → YouTube/Instagram/X
xpst run

# Bidirectional (all sources to all platforms)
xpst run --bidirectional

# Dry run (show what would happen)
xpst run --dry-run --json

# Custom config
xpst --config /path/to/config.yaml run
```

### Continuous watching

```bash
# Watch every 15 minutes (default)
xpst watch

# Custom interval
xpst watch --interval 300

# Bidirectional mode
xpst watch --bidirectional
```

### Manual posting

```bash
# Single video
xpst post -v video.mp4 -c "My caption"

# Carousel (multiple images/videos)
xpst post -v img1.jpg -v img2.jpg -v img3.jpg -c "Carousel post"

# Specific platforms
xpst post -v video.mp4 -c "Test" -p youtube,instagram
```

### Health checks

```bash
# Full health check (platforms + sources + quotas)
xpst health --json

# Status summary
xpst status --json
```

### Backfill historical content

```bash
# Retry failed posts
xpst backfill --limit 10

# Dry run
xpst backfill --dry-run --json
```

### Configuration management

```bash
# Show config
xpst config show --json

# Validate config
xpst config validate

# Set value
xpst config set accounts.youtube.enabled false

# Export/import
xpst config export > backup.yaml
xpst config import < backup.yaml
```

---

## CLI Reference

### Global Options

| Option | Description |
|--------|-------------|
| `-c, --config PATH` | Config file path |
| `-v, --verbose` | Enable debug logging |
| `-q, --quiet` | Suppress decorative output |
| `--json` | Machine-readable JSON output |

### Commands

| Command | Description |
|---------|-------------|
| `run` | Check for new videos and post |
| `watch` | Continuous monitoring |
| `post` | Manual post |
| `health` | Platform connectivity test |
| `status` | Statistics and health |
| `backfill` | Retry failed posts |
| `auth` | Platform authentication |
| `connect` | Streamlined account connect |
| `config` | Configuration management |
| `schedule` | Scheduled posts |
| `logs` | View logs |
| `dashboard` | Launch web API dashboard |
| `mcp` | Start MCP server |
| `setup` | Interactive setup wizard |
| `update` | Update dependencies |
| `version` | Show version info |
| `app` | Native desktop app |

### Exit Codes

| Code | Name | Description |
|------|------|-------------|
| 0 | EXIT_SUCCESS | Success |
| 1 | EXIT_GENERAL | General error |
| 2 | EXIT_AUTH_FAILURE | Authentication failed |
| 3 | EXIT_RATE_LIMIT | Rate limited by platform |
| 4 | EXIT_CONFIG_ERROR | Configuration error |
| 10 | EXIT_PLATFORM_UNAVAILABLE | Platform API unavailable |

### JSON Output

All commands support `--json` for machine-readable output. JSON is automatic when stdout is piped:

```bash
xpst run --json
# {"status": "ok", "results": [...]}

xpst health --json
# {"sources": {...}, "platforms": {...}, "circuit_breakers": {...}}
```

---

## MCP Server

The MCP (Model Context Protocol) server allows AI assistants to control xPST.

### Starting the server

```bash
xpst mcp
```

### Available Tools

| Tool | Description |
|------|-------------|
| `xpst_run` | Check and cross-post new videos |
| `xpst_post` | Manual post local video |
| `xpst_health` | Platform health check |
| `xpst_status` | Statistics |
| `xpst_backfill` | Retry failed posts |
| `xpst_config_show` | Show configuration |
| `xpst_auth_status` | Auth status |
| `xpst_delete` | Delete post from state |

### Example: Using with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "xpst": {
      "command": "xpst",
      "args": ["mcp"]
    }
  }
}
```

Then in Claude:

> "Check for new TikTok videos and cross-post them to YouTube Shorts"

---

## Dashboard API

Launch the web API dashboard:

```bash
# Default port 8080
xpst dashboard

# Custom port and host
xpst dashboard --port 9000 --host 127.0.0.1
```

### Endpoints

| Endpoint | Description | Auth |
|----------|-------------|------|
| `GET /health` | Aggregated health status | No |
| `GET /metrics` | Prometheus metrics | No |
| `GET /state` | Cross-posting statistics | Yes |
| `GET /analytics` | Detailed analytics | Yes |
| `GET /history` | Post history | Yes |

### Authentication

Basic auth with `dashboard_username` and bcrypt `dashboard_password_hash` from config.

```bash
# Set password
xpst config set monitoring.dashboard_password mypassword
```

---

## Architecture

### Core Components

```
xpst/
├── cli.py              # Click-based CLI with JSON/TTY detection
├── config.py           # Configuration management (YAML + env + defaults)
├── engine_v2.py        # New engine with use-case layer + DI
├── state.py            # StateManager (legacy API)
├── state_manager.py    # Business logic for state
├── state_store.py      # Atomic I/O, locking, corruption recovery
├── platforms/          # Platform uploaders
│   ├── base.py         # Abstract base + registry
│   ├── youtube.py      # YouTube Shorts (OAuth2)
│   ├── instagram.py    # Instagram Reels (instagrapi)
│   └── x.py            # X/Twitter (twikit)
├── sources/            # Video sources
│   ├── base.py         # Abstract base + registry
│   ├── tiktok.py       # TikTok (yt-dlp)
│   ├── youtube.py      # YouTube (yt-dlp)
│   ├── x.py            # X (yt-dlp)
│   ├── instagram.py    # Instagram (instagrapi)
│   └── local.py        # Local files
├── usecases/           # Business logic layer
│   ├── base.py         # Protocols and result DTOs
│   ├── fetch_videos.py
│   ├── cross_post.py
│   ├── manual_post.py
│   ├── backfill.py
│   ├── health_check.py
│   ├── delete_post.py
│   └── factory.py      # DI factory
├── utils/              # Shared utilities
│   ├── logger.py       # Structured logging (rich + JSON)
│   ├── credentials.py  # OS keychain + encrypted fallback
│   ├── circuit_breaker.py
│   ├── quota.py        # API quota tracking
│   ├── metrics.py      # Prometheus metrics
│   ├── sessions.py     # SessionManager (auth consolidation)
│   ├── video.py        # FFmpeg encoding
│   └── platform.py     # Cross-platform paths
├── mcp/                # MCP server
│   └── server.py       # stdio MCP server
├── dashboard/          # Web API dashboard
│   ├── server.py       # FastAPI server
│   └── analytics.py    # Analytics collector
└── config_migration.py # Config version migrations
```

### Use-Case Layer

Business logic is separated into use-cases with dependency injection:

```python
# Use-cases receive dependencies via constructor
class CrossPostVideoUseCase:
    def __init__(self, deps: UseCaseDependencies):
        self.deps = deps
    
    async def execute(self, video_id, caption, platforms=None):
        # Pure business logic, no direct I/O
        pass
```

This enables:
- Unit testing with mocks
- Parallel execution
- Clean separation of concerns

---

## Security

### Credential Storage

1. **OS Keychain** (default)
   - macOS: Keychain
   - Windows: Credential Locker
   - Linux: Secret Service (libsecret)

2. **Encrypted Fallback** (when keyring unavailable)
   - Fernet encryption with argon2id key derivation
   - `.enc` files in `~/.xpst/credentials/`
   - Per-file encryption keys

### Dashboard Authentication

- bcrypt password hashing (cost factor 12)
- Basic auth over HTTPS recommended
- Separate username/password from platform credentials

### Network Security

- TLS for all external API calls
- No telemetry or data collection
- Local-only dashboard by default

### Data Privacy

- No video content stored permanently
- Downloaded videos cleaned up after post (configurable)
- State file contains only metadata (URLs, IDs, timestamps)

---

## Troubleshooting

### Common Issues

#### "No sessionid found in session file"
```bash
# Re-authenticate
xpst auth instagram
```

#### "YouTube credentials expired"
```bash
# Re-authenticate (auto-refreshes token)
xpst auth youtube
```

#### "FFmpeg not found"
```bash
# Install FFmpeg
brew install ffmpeg  # macOS
sudo apt install ffmpeg  # Linux
```

#### "Rate limited"
```bash
# Wait for circuit breaker to reset (default: 1 hour)
# Or manually reset:
xpst run --dry-run  # Won't trigger API calls
```

#### "State corruption"
```bash
# State automatically recovers from backup
# Manual recovery:
cp ~/.xpst/backups/state.json.backup_YYYYMMDD_HHMMSS ~/.xpst/state.json
```

### Debug Logging

```bash
xpst -v run  # DEBUG level
xpst --quiet run  # WARNING only
```

### Health Check

```bash
xpst health --json
# Check: sources, platforms, circuit_breakers, quotas
```

---

## Contributing

### Development Setup

```bash
git clone https://github.com/yourusername/xpst
cd xpst
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

### Running Tests

```bash
# All tests (793 tests)
pytest

# Specific test file
pytest tests/test_state.py -v

# With coverage
pytest --cov=xpst --cov-report=html
```

### Code Style

```bash
# Format
ruff format .
ruff check . --fix

# Type check
mypy src/xpst
```

### Release Process

```bash
# Build and test
./scripts/release.sh

# Sign macOS binary
./scripts/sign_macos.sh
```

---

## License

MIT License - see LICENSE file for details.

---

## Support

- Issues: https://github.com/yourusername/xpst/issues
- Discussions: https://github.com/yourusername/xpst/discussions