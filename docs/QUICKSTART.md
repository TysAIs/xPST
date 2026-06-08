# xPST Quick Start Guide

Get xPST up and running in 5 minutes.

## Prerequisites

- **Python 3.9+** ([Download](https://python.org))
- **FFmpeg** ([Download](https://ffmpeg.org/download.html))
  - macOS: `brew install ffmpeg`
  - Ubuntu: `sudo apt install ffmpeg`
  - Windows: `choco install ffmpeg` or download from ffmpeg.org

## Installation

### Option 1: pip (Recommended)

```bash
# Clone the repository
git clone https://github.com/xPSTOwner/XPST.git
cd XPST

# Create virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install xPST
pip install -e .
```

### Option 2: uv (Fastest)

```bash
git clone https://github.com/xPSTOwner/XPST.git
cd XPST

# Install with uv
uv pip install -e .
```

### Option 3: Docker

```bash
git clone https://github.com/xPSTOwner/XPST.git
cd XPST

# Build and run
docker-compose up -d
```

## Setup

### Step 1: Run Setup Wizard

```bash
xpst setup
```

This will:
1. Ask for your TikTok username
2. Ask which platforms to enable
3. Create configuration file at `~/.xpst/config.yaml`
4. Create credentials directory at `~/.xpst/credentials/`

### Step 2: Authenticate Platforms

#### YouTube Shorts

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or select existing)
3. Enable **YouTube Data API v3**
4. Go to **Credentials** → **Create Credentials** → **OAuth 2.0 Client ID**
5. Application type: **Desktop app**
6. Download the JSON file
7. Save as `~/.xpst/credentials/youtube_client_secrets.json`
8. Run: `xpst auth youtube`

#### X/Twitter

**Option A: Browser Cookies (Easiest)**
1. Log into [x.com](https://x.com) in your browser
2. Install a cookie editor extension (e.g., "Cookie-Editor")
3. Export cookies as JSON
4. Save as `~/.xpst/credentials/x_cookies.json`

**Option B: twikit Login**
```bash
python3 -c "
import twikit, asyncio
async def login():
    c = twikit.Client('en-US')
    await c.login(os.environ['XPST_USER'], os.environ['XPST_PASS'])
    c.save_cookies('cookies.json')
asyncio.run(login())
"
mv cookies.json ~/.xpst/credentials/x_cookies.json
```

#### Instagram Reels

1. Log into [instagram.com](https://instagram.com) in your browser
2. Open DevTools (F12) → Application → Cookies
3. Find the cookie named `sessionid`
4. Copy its value
5. Create `~/.xpst/credentials/instagram_session.json`:

```json
{
    "authorization_data": {
        "sessionid": "YOUR_SESSION_ID_HERE"
    }
}
```

### Step 3: Verify Setup

```bash
xpst status
```

This shows:
- Platform authentication status
- Health of each component
- Any configuration issues

## Usage

### One-Time Check

```bash
# Check for new videos and post them
xpst run
```

### Watch Mode (Recommended)

```bash
# Monitor every 15 minutes (default)
xpst watch

# Custom interval (e.g., every 5 minutes)
xpst watch --interval 300
```

### Manual Post

```bash
# Post a specific video
xpst post --video ./my_video.mp4 --caption "Check this out! #AI #tech"

# Post to specific platforms only
xpst post --video ./my_video.mp4 --caption "Test" --platforms youtube,x
```

### Backfill Failed Posts

```bash
# Retry posts that failed
xpst backfill

# Limit to 5 videos
xpst backfill --limit 5

# Specific platforms
xpst backfill --platforms youtube,instagram
```

### View Logs

```bash
xpst logs
```

## Configuration

Edit `~/.xpst/config.yaml`:

```yaml
accounts:
  tiktok:
    username: "your_username"
    cookies_from_browser: true  # Enable HD downloads
  
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
  download_dir: "~/.xpst/downloads"
  cleanup_after_post: false

schedule:
  check_interval: 900  # 15 minutes
```

See `configs/example.yaml` for full configuration options.

## Troubleshooting

### "yt-dlp not found"

```bash
pip install yt-dlp
# or
brew install yt-dlp
```

### "FFmpeg not found"

```bash
# macOS
brew install ffmpeg

# Ubuntu
sudo apt install ffmpeg

# Windows
choco install ffmpeg
```

### "YouTube credentials expired"

If your OAuth app is in **Testing** mode with a Gmail/Brand Account:
1. Go to Google Cloud Console → OAuth consent screen
2. Switch to **Production**
3. Run: `xpst auth youtube`

### "X session expired"

Re-export cookies from your browser and update `~/.xpst/credentials/x_cookies.json`.

### "Instagram session expired"

Get a fresh `sessionid` from browser cookies and update `~/.xpst/credentials/instagram_session.json`.

### Video quality is poor

**For Instagram/X**: xPST automatically encodes videos with optimal settings. If quality is still poor:

1. Check source video quality: `xpst status` shows download format
2. Enable browser cookies for HD downloads:
   ```yaml
   tiktok:
     cookies_from_browser: true
   ```
3. Check FFmpeg encoding settings in config

### Circuit breaker is open

If a platform fails 5+ times, it's temporarily disabled. To reset:

```bash
# Check status
xpst status

# Wait 1 hour (automatic reset) or restart
xpst run
```

## Running as a Service

### macOS (LaunchAgent)

```bash
# Create plist file
cat > ~/Library/LaunchAgents/com.xpst.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xpst</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/.venv/bin/xpst</string>
        <string>watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

# Load service
launchctl load ~/Library/LaunchAgents/com.xpst.plist
```

### Linux (systemd)

```bash
# Create service file
sudo cat > /etc/systemd/system/xpst.service << 'EOF'
[Unit]
Description=xPST Cross-Poster
After=network.target

[Service]
Type=simple
User=xPSTOwner
WorkingDirectory=/path/to/xPST
ExecStart=/path/to/.venv/bin/xpst watch
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl enable xpst
sudo systemctl start xpst
```

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/xPSTOwner/XPST/issues)
- **Discussions**: [GitHub Discussions](https://github.com/xPSTOwner/XPST/discussions)
- **Wiki**: [Documentation Wiki](https://github.com/xPSTOwner/XPST/wiki)

## Next Steps

- Read the [Architecture Guide](docs/ARCHITECTURE.md) for technical details
- Check [Contributing Guide](CONTRIBUTING.md) to contribute
- Review [Configuration Reference](configs/example.yaml) for advanced options