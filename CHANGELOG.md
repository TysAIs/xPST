# Changelog

All notable changes to xPST will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Setup wizard** (`xpst setup`): Interactive first-time configuration
  - System requirements check (ffmpeg, Python version)
  - Automatic directory structure creation (~/.xpst/)
  - Step-by-step platform authentication guidance
  - Config file generation
  - Connectivity testing
- **Auto-update system**:
  - `xpst update`: Updates yt-dlp, instagrapi, twikit to latest versions
  - `xpst update --check`: Check for available updates without installing
  - `xpst version`: Show current version and all dependency versions
- **Crash recovery**:
  - Detects incomplete uploads on startup
  - Interactive retry/skip for each incomplete item
  - Upload progress checkpoints saved during processing
  - Automatic checkpoint cleanup on successful uploads
- **Docker support**:
  - Updated Dockerfile with Python 3.12 base
  - FFmpeg installed in container
  - Volume mount for ~/.xpst/ persistent data
  - docker-compose.yml for easy deployment
  - docker-entrypoint.sh for flexible command routing
- **GitHub Actions CI** (`.github/workflows/test.yml`):
  - Test suite runs on Python 3.10, 3.11, 3.12
  - Lint check with ruff
  - Type check with mypy
  - Docker build verification
  - Coverage reporting with Codecov
- **CONTRIBUTING.md**: Updated contributor guide with new project structure
- **CHANGELOG.md**: This changelog tracking version changes

## [0.1.0] - 2024-XX-XX

### Added
- Initial release
- TikTok video downloading via yt-dlp
- YouTube Shorts uploading with OAuth2
- X/Twitter uploading via twikit
- Instagram Reels uploading via instagrapi
- Platform-specific video encoding (research-verified optimal settings)
- Circuit breaker pattern for fault tolerance
- Exponential backoff retry logic
- Atomic state persistence with backup rotation
- Watch mode with sleep/wake catch-up
- Manual posting via CLI
- Health check endpoint
- Docker support
- Comprehensive documentation

### Security
- Local-only credential storage
- No credentials committed to repository
- OAuth2 token refresh handling

---

## Version History

- **0.1.0** - Initial release with core functionality
- **Unreleased** - Development version

---

## Upgrade Notes

### 0.0.x to 0.1.0

No breaking changes. First public release.
