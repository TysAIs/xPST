# xPST Installation Guide

Complete installation instructions for all platforms and use cases.

---

## Prerequisites

### Required

- **Python 3.10+** (3.10, 3.11, 3.12, or 3.13)
- **FFmpeg** — video encoding and processing
- **pip** or **uv** — Python package manager

### Optional

- **Git** — for development installation
- **Docker** — for containerized deployment
- **PySide6** — for native desktop app (QML UI)

---

## Platform-Specific Prerequisites

### macOS

```bash
# Install Python (if not already installed)
brew install python@3.12

# Install FFmpeg
brew install ffmpeg

# Verify
python3 --version   # Should be 3.10+
ffmpeg -version
```

### Ubuntu / Debian

```bash
# Install Python and FFmpeg
sudo apt update
sudo apt install python3 python3-pip python3-venv ffmpeg

# Verify
python3 --version
ffmpeg -version
```

### Fedora / RHEL

```bash
sudo dnf install python3 python3-pip ffmpeg

# Verify
python3 --version
ffmpeg -version
```

### Windows

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/)
   - Check "Add Python to PATH" during installation
2. Install FFmpeg:
   - Download from [ffmpeg.org](https://ffmpeg.org/download.html)
   - Extract and add `bin/` to your system PATH
   - Or use: `winget install ffmpeg`
3. Verify:
   ```powershell
   python --version
   ffmpeg -version
   ```

---

## Installation Methods

### Method 1: From PyPI (Recommended)

```bash
pip install xpst

# With MCP server support (for AI agents)
pip install "xpst[mcp]"

# With desktop app support
pip install "xpst[desktop]"

# With Windows-specific extras
pip install "xpst[windows]"

# Everything
pip install "xpst[mcp,desktop]"
```

### Method 2: From Source (Development)

```bash
# Clone the repository
git clone https://github.com/TysAIs/xPST.git
cd xPST

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # macOS/Linux
# .venv\Scripts\activate    # Windows

# Install in editable mode with dev dependencies
pip install -e ".[dev,mcp]"

# Run tests to verify
pytest
```

### Method 3: Using uv (Faster)

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/TysAIs/xPST.git
cd xPST
uv venv
source .venv/bin/activate
uv pip install -e ".[dev,mcp]"
```

---

## Docker Setup

### Build the Image

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
docker build -t xpst .
```

### Run

```bash
# Run a one-shot cross-post check
docker run --rm \
  -v ~/.xpst:/root/.xpst \
  xpst run

# Run with config file
docker run --rm \
  -v ~/.xpst:/root/.xpst \
  -v ~/Videos:/videos \
  xpst post --video /videos/clip.mp4 --caption "Hello!"

# Interactive shell
docker run --rm -it \
  -v ~/.xpst:/root/.xpst \
  xpst bash
```

### Docker Compose

```yaml
# docker-compose.yml
services:
  xpst:
    build: .
    volumes:
      - ~/.xpst:/root/.xpst
      - ~/Videos:/videos:ro
    command: watch --interval 900
    restart: unless-stopped
```

```bash
docker compose up -d
```

---

## First-Time Setup

### 1. Run the Setup Wizard

```bash
xpst setup
```

The wizard walks you through:

1. **Choose platforms** — enable YouTube, Instagram, X/Twitter, TikTok
2. **Authenticate each platform** — guided instructions for each
3. **Set rate limits** — defaults to 5 uploads/day per platform
4. **Configure notifications** — optional Discord/Telegram webhooks
5. **Create config file** — saves to `~/.xpst/config.yaml`

### 2. Connect Platforms

```bash
# Connect all platforms (interactive wizard)
xpst connect

# Connect a specific platform
xpst connect youtube
xpst connect instagram
xpst connect x

# Test existing connections
xpst connect --test
```

### 3. Verify Setup

```bash
# Check all platform connections
xpst health

# Validate configuration
xpst config validate

# View current config
xpst config show
```

### 4. First Run

```bash
# Dry run — see what would happen without posting
xpst run --dry-run

# Actual first run
xpst run

# Or start watching
xpst watch
```

---

## MCP Server Setup (for AI Agents)

### Install with MCP support

```bash
pip install "xpst[mcp]"
```

### Configure your AI assistant

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "xpst": {
      "command": "xpst-mcp"
    }
  }
}
```

**Cursor / Windsurf** (MCP settings):

```json
{
  "xpst": {
    "command": "xpst-mcp",
    "transport": "stdio"
  }
}
```

### Verify

```bash
# Test MCP server starts
xpst-mcp  # Should start and wait for stdio input

# Or via CLI
xpst mcp
```

---

## Desktop App Setup

### Native App (PySide6 — Recommended)

```bash
pip install PySide6
xpst app
```

### Browser Fallback

```bash
pip install "xpst[desktop]"
xpst app

# Or launch in browser directly
xpst dashboard
xpst dashboard --port 9090
```

---

## OS Scheduler Setup

Automatically run `xpst schedule run` on a timer:

```bash
# Install OS-level scheduler (every 15 minutes)
xpst schedule install

# Custom interval (every 30 minutes)
xpst schedule install --interval 30

# Remove scheduler
xpst schedule install --remove
```

- **macOS:** Creates a LaunchAgent in `~/Library/LaunchAgents/`
- **Linux:** Adds a crontab entry
- **Windows:** Creates a Scheduled Task

---

## Troubleshooting

### "FFmpeg not found"

```bash
# Verify FFmpeg is installed and in PATH
ffmpeg -version

# macOS
brew install ffmpeg

# Linux
sudo apt install ffmpeg
```

### "Permission denied" on credentials

```bash
# Fix permissions on credentials directory
chmod 700 ~/.xpst/credentials
chmod 600 ~/.xpst/credentials/*
```

### "Module not found: xpst"

```bash
# Make sure xPST is installed
pip install -e .

# Or check if virtual environment is activated
source .venv/bin/activate
which xpst  # Should point to .venv/bin/xpst
```

### "Config file not found"

```bash
# Run the setup wizard to create config
xpst setup

# Or manually create
mkdir -p ~/.xpst
# Edit ~/.xpst/config.yaml (see README for full config)
```

### YouTube "OAuth" errors

```bash
# Delete cached token and re-authenticate
rm ~/.xpst/credentials/youtube_token.json
xpst auth youtube
```

### Instagram "Session expired"

```bash
# Re-export cookies from browser
# See docs/X_AUTH_GUIDE.md for cookie instructions
xpst auth instagram
```

### X/Twitter "Cookie expired"

```bash
# Re-export cookies from browser
# See docs/X_AUTH_GUIDE.md for cookie instructions
xpst auth x
```

### "Keyring" errors on Linux

```bash
# Install secret service support
sudo apt install gnome-keyring

# Or use file-based fallback (automatic if keyring unavailable)
```

### "yt-dlp" errors

```bash
# Update yt-dlp to latest version
pip install --upgrade yt-dlp

# Or update all xPST dependencies
xpst update
```

### Port already in use (dashboard)

```bash
# Use a different port
xpst dashboard --port 9090
```

### Tests failing

```bash
# Run with verbose output
pytest -x --tb=long -v

# Run specific test file
pytest tests/test_engine.py -v

# Check for missing dependencies
pip install -e ".[dev]"
```

---

## Updating

```bash
# Check for updates
xpst update --check

# Update all dependencies
xpst update

# Or manually
pip install --upgrade xpst
```

---

## Uninstalling

```bash
# Remove xPST package
pip uninstall xpst

# Remove configuration and credentials (optional)
rm -rf ~/.xpst

# Remove OS scheduler (if installed)
xpst schedule install --remove
```

---

## See Also

- [Agent Guide](AGENT_GUIDE.md) — CLI `--json` and Python API
- [MCP Tools Reference](MCP_TOOLS.md) — AI integration
- [X Auth Guide](X_AUTH_GUIDE.md) — Cookie-based authentication
- [Open Source Integrations](OPEN_SOURCE_INTEGRATIONS.md) — Dependencies
