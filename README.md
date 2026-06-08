# XPST 🚀

[![License: MIT OR Apache-2.0](https://img.shields.io/badge/License-MIT%2FApache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Enterprise-grade, open-source cross-posting for short-form video**

Automatically distribute portrait videos from TikTok to YouTube Shorts, X/Twitter, and Instagram Reels — or any combination. Run locally, own your data, zero subscriptions.

## ⚠️ Disclaimer

**This software is provided for educational and legitimate content distribution purposes only.**

XPST uses unofficial APIs and third-party libraries to interact with social media platforms. Users are solely responsible for ensuring their use of this software complies with:

- The Terms of Service of each platform (Instagram, X/Twitter, YouTube, TikTok)
- All applicable local, state, national, and international laws and regulations
- Platform-specific rate limits and usage policies

**Use at your own risk.** The developers of XPST:
- Are not responsible for any account suspensions, bans, or other penalties
- Do not encourage or condone violating any platform's Terms of Service
- Recommend using official platform APIs and tools whenever possible
- Provide this software "as is" without warranty of any kind

By using XPST, you acknowledge that:
1. You understand the risks of using unofficial APIs
2. You will comply with all applicable Terms of Service
3. You accept full responsibility for your use of this software
4. You will not use this software for spam, harassment, or other abusive purposes

## ✨ Features

- **TikTok-first workflow** — Download new videos, cross-post everywhere
- **Portrait-optimized** — Purpose-built for 9:16 short-form content
- **Platform-native quality** — Research-verified encoding per platform:
  - YouTube: Original quality (no re-encoding)
  - Instagram: 720p @ CRF 23, Main@L3.0, fixed GOP 72, bt.709
  - X/Twitter: 1080p @ 10 Mbps, High@L4.0, yuv420p, bt.709
- **Enterprise-grade reliability** — Circuit breakers, exponential backoff, dead letter queue
- **Self-hosted & private** — All credentials stay on your machine
- **Zero subscriptions** — Free forever, dual MIT/Apache-2.0 licensed

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- FFmpeg (`brew install ffmpeg` / `apt install ffmpeg`)
- yt-dlp (`pip install yt-dlp`)

### Installation

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/XPST.git
cd XPST

# Install with pip (recommended)
pip install -e .

# Or with uv (faster)
uv pip install -e .

# Or with Poetry
poetry install
```

### Setup Accounts

```bash
# Interactive setup wizard
xpst setup

# Or set up individual platforms
xpst auth tiktok      # Uses browser cookies (optional, for HD quality)
xpst auth youtube     # OAuth2 flow for YouTube Shorts
xpst auth x           # X/Twitter login via twikit
xpst auth instagram   # Instagram session via instagrapi
```

### Run

```bash
# One-time check and post
xpst run

# Watch mode (every 15 minutes)
xpst watch

# Manual post
xpst post --video ./my_video.mp4 --caption "Check this out!"

# Health check
xpst status

# View logs
xpst logs
```

## 📁 Configuration

XPST uses `~/.xpst/config.yaml`:

```yaml
# Account settings
accounts:
  tiktok:
    username: "your_username"
    cookies_from_browser: true  # Use browser cookies for HD quality
    
  youtube:
    enabled: true
    client_secrets: "~/.xpst/credentials/youtube_client_secrets.json"
    
  x:
    enabled: true
    cookies_file: "~/.xpst/credentials/x_cookies.json"
    
  instagram:
    enabled: true
    session_file: "~/.xpst/credentials/instagram_session.json"

# Video processing
video:
  download_dir: "~/.xpst/downloads"
  cleanup_after_post: false  # Keep downloaded videos
  
  # Platform-specific encoding
  encoding:
    youtube:
      passthrough: true  # No re-encoding for YouTube
      
    instagram:
      resolution: 720
      crf: 23
      maxrate: "3500k"
      profile: main
      level: "3.0"
      gop: 72
      fps: 30
      color: bt709
      
    x:
      resolution: 1080
      bitrate: "10M"
      maxrate: "12M"
      profile: high
      level: "4.0"
      gop: 90
      fps: 30
      color: bt709

# Reliability
reliability:
  max_retries: 3
  retry_backoff: 2  # Exponential base (2^n seconds)
  circuit_breaker_threshold: 5  # Failures before disabling platform
  circuit_breaker_reset: 3600  # Seconds before retry

# Monitoring
monitoring:
  log_level: INFO
  log_file: "~/.xpst/logs/xpst.log"
  log_rotation: "10 MB"
  healthcheck_port: 8080

# Scheduling
schedule:
  check_interval: 900  # 15 minutes
  catchup_window: 172800  # 48 hours
  catchup_times_per_day: 3
```

## 🏗️ Architecture

```
XPST/
├── src/xpst/
│   ├── __init__.py          # Package version, exports
│   ├── cli.py               # Click CLI commands
│   ├── engine.py            # Core cross-posting engine
│   ├── config.py            # Configuration management
│   ├── state.py             # State persistence (atomic writes)
│   ├── scheduler.py         # Watch mode, catch-up logic
│   │
│   ├── platforms/           # Platform plugins
│   │   ├── __init__.py      # Plugin registry
│   │   ├── base.py          # Abstract base class
│   │   ├── youtube.py       # YouTube Shorts uploader
│   │   ├── x.py             # X/Twitter uploader
│   │   └── instagram.py     # Instagram Reels uploader
│   │
│   ├── sources/             # Video sources
│   │   ├── __init__.py      # Source registry
│   │   ├── base.py          # Abstract base class
│   │   └── tiktok.py        # TikTok downloader (yt-dlp)
│   │
│   └── utils/               # Shared utilities
│       ├── __init__.py
│       ├── logger.py        # Structured logging
│       ├── circuit_breaker.py # Circuit breaker pattern
│       ├── retry.py         # Retry with backoff
│       └── video.py         # FFmpeg video processing
│
├── tests/                   # Test suite
├── docs/                    # Documentation
├── configs/                 # Example configurations
├── pyproject.toml           # Modern Python packaging
└── README.md                # This file
```

## 🔧 Development

### Setup Development Environment

```bash
# Clone and install with dev dependencies
git clone https://github.com/YOUR_USERNAME/XPST.git
cd XPST
pip install -e ".[dev]"

# Or with uv
uv pip install -e ".[dev]"
```

### Run Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=xpst --cov-report=html

# Specific test file
pytest tests/test_video.py -v
```

### Code Quality

```bash
# Lint
ruff check src/ tests/

# Format
ruff format src/ tests/

# Type checking
mypy src/xpst/
```

## 📊 Monitoring

XPST exposes health endpoints for monitoring:

```bash
# CLI health check
xpst status

# HTTP health endpoint (if enabled)
curl http://localhost:8080/health

# JSON status
curl http://localhost:8080/status
```

## 🐳 Docker

```bash
# Build
docker build -t xpst .

# Run
docker run -v ~/.xpst:/root/.xpst xpst watch

# Docker Compose
docker-compose up -d
```

## 🤝 Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Areas for Contribution

- [ ] Facebook Reels support
- [ ] LinkedIn video support
- [ ] Bluesky video support
- [ ] Analytics dashboard (web UI)
- [ ] Telegram bot interface
- [ ] Discord bot interface

## 📄 License

XPST is dual-licensed under your choice of:

- **MIT License** — See [LICENSE](LICENSE) for the full text
- **Apache License 2.0** — See [LICENSE](LICENSE) for the full text

You may choose either license when using, modifying, or distributing this software.

### Why Dual License?

- **MIT**: Maximum simplicity and compatibility (including GPL v2)
- **Apache 2.0**: Patent protection and enterprise-friendly terms

This dual licensing model is used by major projects like Rust, TensorFlow, and Kubernetes.

## 🙏 Acknowledgments

Built on these amazing open-source projects:
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — Video downloading
- [twikit](https://github.com/david-lev/twikit) — X/Twitter automation
- [instagrapi](https://github.com/subzeroid/instagrapi) — Instagram automation
- [google-api-python-client](https://github.com/googleapis/google-api-python-client) — YouTube API
- [FFmpeg](https://ffmpeg.org/) — Video processing
- [Click](https://click.palletsprojects.com/) — CLI framework

See [NOTICES.md](NOTICES.md) for complete dependency licenses.

## 📞 Support

- [Issues](https://github.com/YOUR_USERNAME/XPST/issues)
- [Discussions](https://github.com/YOUR_USERNAME/XPST/discussions)
- [Wiki](https://github.com/YOUR_USERNAME/XPST/wiki)

---

**Made with ❤️ for content creators who own their work**
