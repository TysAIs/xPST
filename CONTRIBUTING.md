# Contributing to xPST

Thank you for your interest in contributing to xPST! This document provides guidelines and information for contributors.

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- FFmpeg
- Git

### Setup Development Environment

```bash
# Clone the repo
git clone https://github.com/xPSTOwner/xPST.git
cd ~/XPST

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install in development mode
pip install -e ".[dev]"

# Run the setup wizard
xpst setup

# Run tests
pytest

# Run linter
ruff check src/ tests/

# Run formatter
ruff format src/ tests/
```

## 📋 How to Contribute

### Reporting Bugs

1. Check existing [issues](https://github.com/xPSTOwner/xPST/issues) first
2. Create a new issue with:
   - Clear title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Python version, etc.)
   - Logs if applicable

### Suggesting Features

1. Check existing [discussions](https://github.com/xPSTOwner/xPST/discussions)
2. Open a new discussion with:
   - Use case description
   - Proposed solution
   - Alternatives considered

### Submitting Code

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Add tests for new functionality
5. Run tests: `pytest`
6. Run linter: `ruff check src/ tests/`
7. Run type checker: `mypy src/xpst/ --ignore-missing-imports`
8. Commit with clear message: `git commit -m "Add feature: description"`
9. Push to your fork: `git push origin feature/my-feature`
10. Open a Pull Request

## 🏗️ Architecture

### Project Structure

```
xPST/
├── src/xpst/          # Main package
│   ├── cli.py              # CLI commands (Click)
│   ├── engine.py           # Core cross-posting logic
│   ├── config.py           # Configuration management
│   ├── state.py            # State persistence
│   ├── scheduler.py        # Watch mode logic
│   ├── setup.py            # Interactive setup wizard
│   ├── updater.py          # Auto-update system
│   ├── crash_recovery.py   # Crash recovery & checkpoints
│   ├── platforms/          # Platform uploaders
│   │   ├── base.py         # Abstract base class
│   │   ├── youtube.py      # YouTube Shorts
│   │   ├── x.py            # X/Twitter
│   │   └── instagram.py    # Instagram Reels
│   ├── sources/            # Video sources
│   │   ├── base.py         # Abstract base class
│   │   └── tiktok.py       # TikTok downloader
│   └── utils/              # Shared utilities
│       ├── logger.py       # Structured logging
│       ├── circuit_breaker.py
│       ├── retry.py        # Retry logic
│       └── video.py        # FFmpeg processing
├── tests/                  # Test suite
├── docs/                   # Documentation
└── configs/                # Example configs
```

### Adding a New Platform

1. Create `src/xpst/platforms/newplatform.py`
2. Inherit from `PlatformUploader`
3. Implement required methods:
   - `upload(video_path, caption) -> UploadResult`
   - `check_health() -> PlatformHealth`
4. Add config to `config.py`
5. Add tests in `tests/test_platforms.py`
6. Update README

### Adding a New Source

1. Create `src/xpst/sources/newsource.py`
2. Inherit from `VideoSource`
3. Implement required methods:
   - `list_videos(max_count) -> list[VideoMetadata]`
   - `download(video_id, output_dir) -> DownloadResult`
   - `check_health() -> dict`
4. Add config to `config.py`
5. Add tests

## 🧪 Testing

### Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=xpst --cov-report=html

# Specific test file
pytest tests/test_config.py -v

# Specific test
pytest tests/test_config.py::TestXPSTConfig::test_default_config -v
```

### Writing Tests

- Use `pytest` fixtures for common setup
- Test both success and failure cases
- Mock external dependencies (APIs, file system)
- Use descriptive test names

Example:

```python
def test_load_config_from_file(tmp_path):
    """Test that config loads correctly from YAML file"""
    config_data = {"accounts": {"tiktok": {"username": "test"}}}
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    config = XPSTConfig.load(str(config_file))
    assert config.tiktok.username == "test"
```

## 📝 Code Style

### Python Version

- Target: Python 3.10+
- Use type hints (PEP 484)
- Use dataclasses for data structures

### Formatting

We use `ruff` for formatting and linting:

```bash
# Check formatting
ruff format --check src/ tests/

# Auto-format
ruff format src/ tests/

# Check linting
ruff check src/ tests/

# Fix auto-fixable issues
ruff check --fix src/ tests/
```

### Naming Conventions

- **Files**: `snake_case.py`
- **Classes**: `PascalCase`
- **Functions**: `snake_case()`
- **Constants**: `UPPER_SNAKE_CASE`
- **Private**: `_leading_underscore`

### Docstrings

Use Google-style docstrings:

```python
def upload_video(video_path: Path, caption: str) -> UploadResult:
    """
    Upload a video to the platform.

    Args:
        video_path: Path to the video file
        caption: Caption for the video

    Returns:
        UploadResult with success status and metadata

    Raises:
        FileNotFoundError: If video file doesn't exist
        ValueError: If video format is invalid
    """
```

## 🔒 Security

### Credential Handling

- **Never** commit credentials to the repository
- Use `~/.xpst/credentials/` for local storage
- Add credential files to `.gitignore`
- Document credential requirements clearly

### API Keys

- Use environment variables for API keys
- Never hardcode keys in source code
- Provide clear setup instructions

## 🐳 Docker Development

```bash
# Build the Docker image
docker build -t xpst .

# Run with docker compose
docker compose up -d

# Check logs
docker compose logs -f

# Run a one-time command
docker compose run --rm xpst run
```

## 📦 Release Process

### Version Bumping

1. Update `version` in `pyproject.toml`
2. Update `__version__` in `src/xpst/__init__.py`
3. Update `CHANGELOG.md`
4. Commit: `git commit -m "Bump version to X.Y.Z"`
5. Tag: `git tag vX.Y.Z`
6. Push: `git push && git push --tags`

### Publishing to PyPI

```bash
# Build
python -m build

# Upload
twine upload dist/*
```

## 🤝 Community

### Code of Conduct

- Be respectful and inclusive
- Focus on constructive feedback
- Help others learn and grow

### Communication

- [GitHub Issues](https://github.com/xPSTOwner/xPST/issues): Bug reports
- [GitHub Discussions](https://github.com/xPSTOwner/xPST/discussions): Questions, ideas
- [Pull Requests](https://github.com/xPSTOwner/xPST/pulls): Code contributions

## 🙏 Thank You!

Every contribution helps make xPST better for everyone. We appreciate your time and effort!
