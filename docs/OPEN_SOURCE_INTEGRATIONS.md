# xPST — Open Source Integration Audit

This document catalogs all open-source dependencies in xPST, evaluates their roles, recommends additional integrations, and verifies license compatibility.

---

## Current Dependencies

### Core CLI & Framework

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **click** | ≥8.1.0 | BSD-3 | CLI framework — command parsing, options, help generation |
| **pyyaml** | ≥6.0 | MIT | YAML configuration file parsing |
| **rich** | ≥13.0.0 | MIT | Terminal formatting — tables, progress bars, colored output |

### Video Downloading

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **yt-dlp** | ≥2025.1.1 | Unlicense | TikTok video downloading with browser cookie support |

### Platform APIs

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **google-api-python-client** | ≥2.0.0 | Apache-2.0 | YouTube Data API v3 client for uploads and analytics |
| **google-auth-oauthlib** | ≥1.0.0 | Apache-2.0 | OAuth 2.0 flow for YouTube authentication |
| **google-auth-httplib2** | ≥0.1.0 | Apache-2.0 | HTTP transport for Google auth |
| **twikit** | ≥2.0.0 | MIT | X/Twitter API client for video uploads via cookies |
| **instagrapi** | ≥2.0.0 | MIT | Instagram API client for Reels uploads via session cookies |

### Monitoring & Observability

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **structlog** | ≥23.0.0 | Apache-2.0/MIT | Structured JSON logging with context |
| **prometheus-client** | ≥0.19.0 | Apache-2.0 | Prometheus metrics exposition |

### Security

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **keyring** | ≥25.0.0 | MIT | OS keychain integration (macOS Keychain, Linux Secret Service, Windows Credential Manager) |

### Dashboard

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **nicegui** | ≥1.4.0,<4.0 | MIT | Web dashboard framework |
| **plotly** | ≥5.0.0,<7.0 | MIT | Interactive analytics charts |

### Auth & HTTP

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **authlib** | ≥1.3.0 | BSD-3 | OAuth 2.0 library |
| **httpx** | ≥0.24.0 | BSD-3 | Async HTTP client |

### Optional Dependencies

| Package | Version | License | Purpose |
|---------|---------|---------|---------|
| **mcp** | ≥1.0.0 | MIT | MCP server framework (AI agent integration) |
| **pywebview** | ≥4.0 | BSD-3 | Desktop app fallback (webview window) |
| **PySide6** | — | LGPL-3.0 | Native desktop app (QML UI) |

---

## License Compatibility Matrix

All current dependencies use permissive licenses compatible with xPST's dual MIT/Apache-2.0 license:

| License | Compatible | Notes |
|---------|-----------|-------|
| MIT | ✅ | Full compatibility |
| BSD-2, BSD-3 | ✅ | Full compatibility |
| Apache-2.0 | ✅ | Full compatibility |
| Unlicense | ✅ | Public domain equivalent |
| LGPL-3.0 | ✅ | OK for dynamically-linked use (PySide6) |
| GPL-3.0 | ❌ | Not used by any current dependency |

---

## Recommended Additions

### 1. `watchdog` — File System Monitoring

**License:** Apache-2.0 ✅
**What it provides:** Cross-platform file system event monitoring using native OS APIs (FSEvents on macOS, inotify on Linux, ReadDirectoryChangesW on Windows).

**How it improves xPST:**
- Replaces polling-based video detection with instant file change notifications
- Dramatically reduces CPU usage in `xpst watch` mode
- Enables instant cross-posting when a new video file appears in the download directory
- Supports recursive directory watching for organized download folders

**Integration point:** `src/xpst/sources/local.py` — replace `os.listdir` polling with `watchdog` observers.

```python
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class VideoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.src_path.endswith(('.mp4', '.mov', '.webm')):
            queue_crosspost(event.src_path)
```

---

### 2. `python-dotenv` — Environment Variable Management

**License:** BSD-3 ✅
**What it provides:** Loads `.env` files into `os.environ` automatically.

**How it improves xPST:**
- Enables `.env` files for credentials (alternative to config YAML)
- Compatible with Docker, CI/CD, and deployment workflows
- Simplifies secret management (`.gitignore` the `.env` file)
- Supports `XPST_*` prefix environment variables cleanly

**Integration point:** `src/xpst/config.py` — add `load_dotenv()` call at startup.

```python
from dotenv import load_dotenv

def load(config_path=None):
    load_dotenv()  # Load .env before reading env vars
    ...
```

---

### 3. `tqdm` — Progress Bars

**License:** MIT ✅
**What it provides:** Fast, extensible progress bars for loops and iterations.

**How it improves xPST:**
- Shows upload progress for large video files (bytes uploaded / total)
- Indicates download progress when fetching from TikTok
- Displays batch processing progress in `xpst run` and `xpst backfill`
- Works in both terminal and Jupyter notebooks

**Integration point:** `src/xpst/utils/progress.py`, `src/xpst/services/upload_service.py`.

```python
from tqdm import tqdm

with tqdm(total=file_size, unit='B', unit_scale=True) as pbar:
    for chunk in upload_stream:
        pbar.update(len(chunk))
```

---

### 4. `aiofiles` — Async File Operations

**License:** Apache-2.0 ✅
**What it provides:** Asynchronous file I/O using Python's `asyncio`.

**How it improves xPST:**
- Prevents blocking the event loop during file reads/writes in async context
- Improves throughput when processing multiple videos concurrently
- Non-blocking log file writes
- Better performance for the async engine pipeline

**Integration point:** `src/xpst/engine.py`, `src/xpst/state.py`.

```python
import aiofiles

async def save_state(self):
    async with aiofiles.open(self.state_path, 'w') as f:
        await f.write(json.dumps(self.state))
```

---

### 5. `Pillow` — Image Processing / Thumbnails

**License:** MIT-CMU ✅ (permissive, compatible)
**What it provides:** Image manipulation — resize, crop, format conversion, thumbnails.

**How it improves xPST:**
- Generates video thumbnails for the web dashboard
- Creates Instagram-optimized cover images (1:1 crop)
- Validates image dimensions before carousel uploads
- Converts between image formats (PNG → JPEG for platform requirements)

**Integration point:** `src/xpst/utils/video.py`, `src/xpst/dashboard/`.

```python
from PIL import Image

def generate_thumbnail(video_path, size=(320, 180)):
    frame = extract_first_frame(video_path)
    img = Image.fromarray(frame)
    img.thumbnail(size)
    return img
```

---

### 6. `moviepy` — Video Editing

**License:** MIT ✅
**What it provides:** Programmatic video editing — trim, concatenate, add text overlays, extract frames.

**How it improves xPST:**
- Adds watermark/text overlay for branding across platforms
- Trims videos to platform-specific length limits (60s for Shorts, 90s for Reels)
- Concatenates multiple clips for stitched content
- Extracts audio tracks for platform-specific processing
- Generates preview clips for the dashboard

**Integration point:** `src/xpst/utils/video.py`.

```python
from moviepy.editor import VideoFileClip

def trim_to_limit(video_path, max_seconds=60):
    clip = VideoFileClip(video_path)
    if clip.duration > max_seconds:
        clip = clip.subclip(0, max_seconds)
    return clip
```

---

## Dependency Graph

```
xPST
├── click (CLI)
├── pyyaml (config)
├── rich (terminal UI)
├── yt-dlp (TikTok downloading)
├── google-api-python-client + google-auth-oauthlib (YouTube)
├── twikit (X/Twitter)
├── instagrapi (Instagram)
├── structlog (logging)
├── prometheus-client (metrics)
├── keyring (security)
├── nicegui + plotly (dashboard)
├── authlib + httpx (HTTP/auth)
│
├── [optional] mcp (AI integration)
├── [optional] pywebview (desktop fallback)
│
└── [recommended]
    ├── watchdog (file monitoring)
    ├── python-dotenv (env vars)
    ├── tqdm (progress bars)
    ├── aiofiles (async I/O)
    ├── Pillow (image processing)
    └── moviepy (video editing)
```

---

## License Compatibility Summary

| Recommendation | License | Compatible? |
|---------------|---------|-------------|
| watchdog | Apache-2.0 | ✅ Yes |
| python-dotenv | BSD-3 | ✅ Yes |
| tqdm | MIT | ✅ Yes |
| aiofiles | Apache-2.0 | ✅ Yes |
| Pillow | MIT-CMU | ✅ Yes |
| moviepy | MIT | ✅ Yes |

**All recommended additions use MIT, Apache-2.0, or BSD licenses — fully compatible with xPST's dual MIT/Apache-2.0 licensing.**

---

## See Also

- [MCP Tools Reference](MCP_TOOLS.md) — AI integration tools
- [Agent Guide](AGENT_GUIDE.md) — Programmatic usage
- [Install Guide](INSTALL.md) — Setup instructions
