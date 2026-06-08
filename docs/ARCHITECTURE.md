# xPST Architecture

This document describes the high-level architecture of xPST, designed for enterprise-grade reliability while remaining simple for individual creators.

## Overview

xPST is a modular, plugin-based system for cross-posting short-form video content. It monitors TikTok for new videos and distributes them to YouTube Shorts, X/Twitter, and Instagram Reels.

```
┌─────────────────────────────────────────────────────────────┐
│                        xPST Engine                      │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   TikTok     │  │   YouTube    │  │   X/Twitter  │      │
│  │   Source     │  │   Platform   │  │   Platform   │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                  │               │
│         ▼                 ▼                  ▼               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Video Processing Pipeline                │   │
│  │  Download → Encode → Upload → Track State            │   │
│  └──────────────────────────────────────────────────────┘   │
│                           │                                  │
│                           ▼                                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    Core Services                      │   │
│  │  State Manager │ Circuit Breakers │ Retry Logic      │   │
│  │  Logger        │ Config Manager   │ Scheduler        │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## Design Principles

### 1. Separation of Concerns
Each component has a single responsibility:
- **Sources**: Download videos (TikTok)
- **Platforms**: Upload videos (YouTube, X, Instagram)
- **Engine**: Orchestrate workflow
- **State**: Persist data
- **Utils**: Shared utilities

### 2. Plugin Architecture
New platforms and sources can be added by implementing abstract base classes:

```python
class PlatformUploader(ABC):
    @abstractmethod
    async def upload(self, video_path: Path, caption: str) -> UploadResult:
        pass
    
    @abstractmethod
    async def check_health(self) -> PlatformHealth:
        pass
```

### 3. Reliability First
- **Circuit Breakers**: Auto-disable failing platforms
- **Retry with Backoff**: Handle transient failures
- **Atomic State**: Never lose track of posted videos
- **Graceful Degradation**: One platform failure doesn't block others

### 4. Configuration as Code
All settings in YAML config with sensible defaults:
- Platform credentials
- Encoding profiles
- Reliability parameters
- Scheduling intervals

## Component Deep Dive

### Sources Layer

Responsible for downloading videos from source platforms.

**Current**: TikTok (via yt-dlp)

**Interface**:
```python
class VideoSource(ABC):
    async def list_videos(max_count: int) -> list[VideoMetadata]
    async def download(video_id: str, output_dir: Path) -> DownloadResult
    async def check_health() -> dict
```

**Key Decisions**:
- Use yt-dlp for TikTok (most reliable, actively maintained)
- Support browser cookies for HD quality
- Flat playlist extraction for fast metadata
- Format selection with fallbacks

### Platforms Layer

Responsible for uploading videos to target platforms.

**Current**: YouTube, X/Twitter, Instagram

**Interface**:
```python
class PlatformUploader(ABC):
    async def upload(video_path: Path, caption: str) -> UploadResult
    async def check_health() -> PlatformHealth
```

**Platform-Specific Encoding**:
Each platform has research-verified optimal encoding settings:

| Platform | Resolution | Bitrate | Profile | Key Settings |
|----------|------------|---------|---------|--------------|
| YouTube | Original | Original | N/A | Passthrough |
| Instagram | 720p | CRF 23 | Main@L3.0 | Fixed GOP 72, bt709, yuv420p |
| X/Twitter | 1080p | 10 Mbps | High@L4.0 | yuv420p (REQUIRED), bt709, keyint=90 |

### Engine Layer

Orchestrates the entire workflow:

1. **Fetch**: Get recent videos from TikTok
2. **Filter**: Identify new videos not yet posted
3. **Download**: Download video files
4. **Encode**: Create platform-specific versions
5. **Upload**: Post to each platform
6. **Track**: Update state and health

**Key Features**:
- Parallel uploads (future)
- Per-platform circuit breakers
- State persistence after each video
- Catch-up logic for sleep/wake

### State Management

Persistent state with corruption recovery:

**Structure**:
```json
{
  "version": 2,
  "posted_videos": {
    "video_id": {
      "tiktok_url": "...",
      "caption": "...",
      "posted_to": {
        "youtube": {"id": "...", "url": "..."},
        "x": {"id": "...", "url": "..."}
      }
    }
  },
  "health": {
    "platforms": {...},
    "total_processed": 0
  }
}
```

**Features**:
- Atomic writes (write temp, then rename)
- Backup rotation (keep last 5)
- Corruption recovery (fall back to backup)
- Versioned schema (migration support)

### Circuit Breaker Pattern

Prevents cascading failures:

**States**:
- **CLOSED**: Normal operation, requests pass through
- **OPEN**: Too many failures, requests blocked
- **HALF_OPEN**: Testing recovery

**Configuration**:
```yaml
reliability:
  circuit_breaker_threshold: 5  # Failures before opening
  circuit_breaker_reset: 3600   # Seconds before retry
```

### Retry Logic

Exponential backoff with jitter:

```python
backoff = min(base^n, max_backoff)
backoff += random.uniform(-jitter, jitter)
```

**Configurations**:
- `QUICK_RETRY`: 2 retries, 1s base, 5s max
- `STANDARD_RETRY`: 3 retries, 2s base, 30s max
- `AGGRESSIVE_RETRY`: 5 retries, 2s base, 60s max

### Video Processing

FFmpeg-based encoding with platform-optimized profiles:

**Instagram Profile**:
```bash
ffmpeg -i input.mp4 \
  -vf "scale=-2:720,setsar=1,format=yuv420p" \
  -c:v libx264 -preset slow -profile:v main -level:v 3.0 \
  -x264-params "scenecut=0:open_gop=0:min-keyint=72:keyint=72:ref=4" \
  -crf 23 -maxrate 3500k -bufsize 3500k \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -r 30 -c:a aac -b:a 256k -ar 44100 \
  -movflags +faststart output.mp4
```

**X/Twitter Profile**:
```bash
ffmpeg -i input.mp4 \
  -vf "scale=-2:1080:flags=lanczos,setsar=1,format=yuv420p" \
  -c:v libx264 -preset slow -profile:v high -level:v 4.0 \
  -x264-params "scenecut=0:open_gop=0:min-keyint=90:keyint=90:ref=4" \
  -b:v 10M -maxrate 12M -bufsize 24M \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -r 30 -c:a aac -b:a 256k -ar 44100 \
  -movflags +faststart output.mp4
```

## Data Flow

```
┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐
│ TikTok  │───▶│ Download│───▶│ Encode  │───▶│ Upload  │
│ Source  │    │ Video   │    │ Per     │    │ To      │
│         │    │         │    │ Platform│    │ Platform│
└─────────┘    └─────────┘    └─────────┘    └─────────┘
                                    │               │
                                    ▼               ▼
                              ┌─────────┐    ┌─────────┐
                              │ State   │    │ Health  │
                              │ Manager │    │ Monitor │
                              └─────────┘    └─────────┘
```

## Security Model

### Credential Storage
- All credentials stored locally in `~/.xpst/credentials/`
- Never committed to repository (in `.gitignore`)
- OAuth tokens refreshed automatically

### API Security
- OAuth2 for YouTube (industry standard)
- Cookie-based for X/Twitter (via twikit)
- Session-based for Instagram (via instagrapi)

### Data Privacy
- No data sent to external servers (except target platforms)
- All processing happens locally
- State files contain only post metadata, no credentials

## Extensibility

### Adding a New Platform

1. Create `src/xpst/platforms/newplatform.py`
2. Implement `PlatformUploader` interface
3. Add config to `config.py`
4. Register in `PlatformRegistry`

### Adding a New Source

1. Create `src/xpst/sources/newsource.py`
2. Implement `VideoSource` interface
3. Add config to `config.py`
4. Register in `SourceRegistry`

### Custom Encoding

Override encoding settings in config:

```yaml
video:
  encoding:
    instagram:
      resolution: 1080
      crf: 18
      # ... other settings
```

## Performance Considerations

### Bottlenecks
1. **Video encoding**: CPU-intensive, ~30-60s per video
2. **Platform uploads**: Network-bound, varies by platform
3. **yt-dlp extraction**: API-bound, ~5-10s per video

### Optimization Opportunities
1. **Parallel encoding**: Encode for multiple platforms simultaneously
2. **Caching**: Cache encoded videos (already implemented)
3. **Incremental updates**: Only process new videos (already implemented)

### Resource Usage
- **CPU**: Spikes during encoding (1-2 cores)
- **Memory**: ~200-500 MB during encoding
- **Disk**: ~50-100 MB per video (original + encoded)
- **Network**: Varies by platform and video size

## Deployment Options

### Local (Recommended)
```bash
pip install xpst
xpst setup
xpst watch
```

### Docker
```bash
docker-compose up -d
```

### Systemd (Linux)
```ini
[Unit]
Description=xPST Cross-Poster
After=network.target

[Service]
Type=simple
User=xpst
ExecStart=/usr/local/bin/xpst watch
Restart=always

[Install]
WantedBy=multi-user.target
```

### LaunchAgent (macOS)
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xpst.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/xpst</string>
        <string>watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

## Future Roadmap

### Phase 2
- [ ] Facebook Reels support
- [ ] LinkedIn video support
- [ ] Bluesky video support
- [ ] Web dashboard (Flask/FastAPI)

### Phase 3
- [ ] AI-powered caption generation
- [ ] Automatic hashtag optimization
- [ ] Analytics dashboard
- [ ] Multi-account support

### Phase 4
- [ ] Telegram bot interface
- [ ] Discord bot interface
- [ ] Slack integration
- [ ] Webhook notifications

## References

### Research Sources
- Instagram encoding: [dev.to/alfg/ffmpeg-for-instagram](https://dev.to/alfg/ffmpeg-for-instagram-35bi)
- X/Twitter encoding: [gehrcke.de](https://gehrcke.de/2021/10/twitters-h-264-video-requirements/)
- X/Twitter encoding: [gist.github.com/transkatgirl](https://gist.github.com/transkatgirl/19363e3ef458ea206aec141ad9d8b382)
- TikTok quality: [yt-dlp issue #4138](https://github.com/yt-dlp/yt-dlp/issues/4138)

### Dependencies
- [yt-dlp](https://github.com/yt-dlp/yt-dlp): Video downloading
- [twikit](https://github.com/david-lev/twikit): X/Twitter automation
- [instagrapi](https://github.com/subzeroid/instagrapi): Instagram automation
- [google-api-python-client](https://github.com/googleapis/google-api-python-client): YouTube API
- [FFmpeg](https://ffmpeg.org/): Video processing
- [Click](https://click.palletsprojects.com/): CLI framework
- [Rich](https://rich.readthedocs.io/): Terminal formatting