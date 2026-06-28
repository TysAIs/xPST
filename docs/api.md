# xPST API Reference

## CrossPostEngine

`xpst.engine.CrossPostEngine` is the single canonical engine. It is shared by
the CLI, scheduler, desktop backend, and the MCP server. It wires its services
(state, upload, source, circuit breakers, crash recovery, notifications) in
``__init__`` — there is no separate async initialize step.

### `CrossPostEngine(config: XPSTConfig)`

Main engine class. Orchestrates fetching, downloading, encoding, uploading,
state tracking, and notifications. Each platform is handled independently so a
single platform failure never blocks others.

#### Attributes
- `config: XPSTConfig` - Loaded configuration
- `state: StateManager` - Persistent state manager
- `source_service: SourceService` - Source fetching/filtering service
- `upload_service: UploadService` - Upload pipeline service
- `circuit_breakers: CircuitBreakerManager` - Per-platform circuit breakers

#### Methods

##### `async check_and_post(catch_up: bool = False) -> list[CrossPostResult]`
Check TikTok for new videos and cross-post them to all enabled platforms.
`catch_up=True` fetches up to 20 videos instead of 5.

##### `async check_and_post_bidirectional(max_per_source: int = 5) -> list[CrossPostResult]`
Poll all enabled sources; cross-post any new post to the other platforms.

##### `async post_manual(video_path: Path, caption: str, platforms: list[str] | None = None) -> CrossPostResult`
Manually post a single local video file to the given platforms (all enabled if None).

##### `async post_manual_carousel(media_paths: list[Path], caption: str, platforms: list[str] | None = None) -> CrossPostResult`
Manually post a carousel/multi-media set.

##### `async backfill(platforms: list[str] | None = None, limit: int = 10) -> list[CrossPostResult]`
Retry videos missing from any platform, reusing previously downloaded files.

##### `async delete_post(video_id: str, platform: str) -> bool`
Delete a previously posted video from a platform via its API.

##### `async check_health() -> dict[str, Any]`
Health check across sources, platforms, circuit breakers, state, and quotas.

##### `acquire_pidfile() / release_pidfile() -> None`
Acquire/release the pidfile lock that prevents concurrent instances.

---

## Use-Case Factory

### `UseCaseFactory(deps: UseCaseDependencies)`

Factory for creating use-case instances with shared dependencies.

#### Methods
- `create_fetch_videos() -> FetchNewVideosUseCase`
- `create_cross_post() -> CrossPostVideoUseCase`
- `create_manual_post() -> ManualPostUseCase`
- `create_backfill() -> BackfillUseCase`
- `create_health_check() -> HealthCheckUseCase`
- `create_delete_post() -> DeletePostUseCase`

---

## Use-Cases

### `FetchNewVideosUseCase(deps)`

Fetch new videos from sources and filter unposted ones.

```python
result = await fetch_uc.execute(
    source_name="tiktok",
    max_count=5,
    catch_up=False
)
# result: FetchVideosResult(videos=[...], fetch_count=3, catch_up=False)
```

### `CrossPostVideoUseCase(deps)`

Cross-post a video to multiple platforms.

```python
result = await cross_post_uc.execute(
    video_id="vid123",
    caption="My video",
    platforms=["youtube", "instagram"]
)
# result: CrossPostResult(video_id, caption, results={...}, all_success=True, partial_success=False)
```

### `ManualPostUseCase(deps)`

Post a local video file.

```python
result = await manual_uc.execute(
    video_path="/path/to/video.mp4",
    caption="My video",
    platforms=["youtube"]
)
```

### `BackfillUseCase(deps)`

Fetch and post historical content.

```python
result = await backfill_uc.execute(
    source_name="tiktok",
    max_count=10,
    platforms=["youtube", "x"]
)
# result: BackfillResult(attempted=5, successful=3, results=[...])
```

### `HealthCheckUseCase(deps)`

Comprehensive health check.

```python
result = await health_uc.execute()
# result: HealthCheckResult(sources={...}, platforms={...}, circuit_breakers={...}, state={...}, quotas={...})
```

### `DeletePostUseCase(deps)`

Delete post from state (and optionally platform).

```python
result = await delete_uc.execute(
    video_id="vid123",
    platform="youtube",  # or None for all
    delete_from_platform=False
)
```

---

## StateManager

High-level state management with business logic.

### Video Tracking

```python
state.is_posted(video_id: str, platform: str) -> bool
state.is_fully_cross_posted(video_id: str, platforms: list[str]) -> bool
state.add_posted_video(video_id, source_url, source_platform, posted_to, caption, content_hash)
state.record_failure(video_id, platform, error)
state.remove_post(video_id, platform)
```

### Content Hash Deduplication

```python
state.get_by_hash(content_hash: str) -> str | None
state.has_hash(content_hash: str) -> bool
state.compute_hash(file_path: Path) -> str
```

### Dead Letter Queue

```python
state.get_dead_letter_queue() -> list[dict]
state.clear_dead_letter_queue() -> int
```

### Platform Health

```python
state.update_platform_health(platform, status, last_success)
state.update_last_check_time()
state.update_last_wake_check()
state.record_circuit_breaker_failure(platform)
state.record_circuit_breaker_success(platform)
state.is_circuit_breaker_open(platform) -> bool
```

### Statistics

```python
state.get_statistics() -> dict
# {
#   "version": 2,
#   "total_videos_tracked": 100,
#   "total_processed": 95,
#   "cross_posted_count": 245,
#   "by_platform": {"youtube": 90, "x": 85, "instagram": 70, "tiktok": 0},
#   "last_check": "...",
#   "last_wake_check": "...",
#   "dead_letter_count": 3,
#   "platform_health": {...}
# }
```

### Persistence

```python
state.save()   # Explicit save
state.reload() # Reload from disk
```

### Legacy API (Compatibility)

```python
state.mark_video_posted(video_id, platform, post_id, post_url, content_hash)
state.mark_cross_posted(video_id, platform, post_id, post_url, caption)
state.mark_cross_post_failed(video_id, platform, error)
state.is_video_posted(video_id, platform) -> bool
state.is_cross_posted(video_id, platform) -> bool
state.get_cross_post_data(video_id, platform) -> dict
state.find_duplicate_by_hash(content_hash, exclude_platform) -> dict
```

---

## StateStore

Low-level state storage with atomic operations.

```python
store = StateStore(config_dir)

store.get() -> dict              # Thread-safe copy
store.get_raw() -> dict          # Raw reference (internal)
store.set(state: dict)           # Atomic write with backup
store.update(updater: callable)  # Atomic update
store.save()                     # Persist current state
store.load_fresh() -> dict       # Reload from disk
```

**Features:**
- Atomic writes (temp file + rename)
- Cross-process locking (fcntl on Unix)
- Backup rotation (keeps 5)
- Corruption recovery (falls back to backups)
- Schema version validation

---

## Configuration

### `XPSTConfig.load(config_path: str | None = None) -> XPSTConfig`

Load configuration with priority:
1. Environment variables (`XPST_*`)
2. Config file (`~/.xpst/config.yaml`)
3. Default values

### `config.save(config_path: str)`

Save current configuration to file.

### Environment Variables

```bash
XPST_ACCOUNTS_TIKTOK_USERNAME=myuser
XPST_ACCOUNTS_YOUTUBE_ENABLED=true
XPST_VIDEO_DOWNLOAD_DIR=/data/videos
XPST_MONITORING_LOG_LEVEL=DEBUG
XPST_SCHEDULE_CHECK_INTERVAL=300
XPST_ANTI_BOT_ENABLED=false
```

---

## SessionManager

Centralized authentication for all platforms/sources.

```python
session_mgr = SessionManager(config)

# Platform clients
await session_mgr.get_youtube_service(client_secrets, token_file)
await session_mgr.get_instagram_client(session_file, username, password)
await session_mgr.get_x_client(cookies_file, username, password)

# Source clients
await session_mgr.get_tiktok_client()
await session_mgr.get_instagram_source_client(session_file, username, password)
```

---

## Circuit Breaker

### `CircuitBreakerManager()`

Per-platform circuit breaker management.

```python
cb_mgr = CircuitBreakerManager()

cb = cb_mgr.get("youtube")  # Auto-creates if missing
allowed = cb.allow_request()  # True if closed/half-open with capacity
cb.record_success()
cb.record_failure()
cb.reset()

status = cb_mgr.get_status()
# {"youtube": {"state": "closed", "failure_count": 0, "success_count": 10}}
```

---

## Quota Manager

### `QuotaManager(config_dir: str)`

API quota tracking with daily limits.

```python
quota = QuotaManager(config_dir)

quota.consume("youtube", 1)  # Reserve 1 upload
remaining = quota.get_remaining("youtube")
# {"daily": 95, "limit": 100}

quota.release("youtube", 1)  # Release reservation
status = quota.get_all_status()
```

---

## Metrics

### `metrics` (singleton)

Prometheus metrics tracker (no-op if `prometheus_client` not installed).

```python
from xpst.utils.metrics import metrics, metrics_text

metrics.record_upload("youtube", "success", duration=2.5)
metrics.record_encoding("instagram", duration=10.0)
metrics.set_active_platforms(3)
metrics.set_circuit_breaker_state("x", is_open=True)

# Expose at /metrics
text = metrics_text()  # Prometheus exposition format
```

**Metrics:**
- `xpst_uploads_total{platform,status}` - Counter
- `xpst_upload_duration_seconds{platform}` - Histogram
- `xpst_encoding_duration_seconds{platform}` - Histogram
- `xpst_active_platforms` - Gauge
- `xpst_circuit_breaker_state{platform}` - Gauge (1=open, 0=closed)

---

## Dashboard API

### `start_dashboard(port: int = 8080, host: str = "127.0.0.1", config_dir: str = "~/.xpst")`

FastAPI server with endpoints:

```python
from xpst.dashboard import start_dashboard

start_dashboard()  # Runs on 127.0.0.1:8080 (loopback only)

# Expose on the network (e.g. inside a container):
start_dashboard(host="0.0.0.0")
```

The default bind address is `127.0.0.1`, so the dashboard is reachable only
from the local machine. Binding to a non-loopback address (such as `0.0.0.0`)
without `dashboard_username`/`dashboard_password_hash` configured logs a
warning, because the authenticated endpoints (`/state`, `/analytics`,
`/history`) would otherwise be exposed to the network without credentials.

### Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | No | Aggregated platform health |
| `/metrics` | GET | No | Prometheus metrics |
| `/state` | GET | Yes | Cross-posting statistics |
| `/analytics` | GET | Yes | Detailed analytics |
| `/history` | GET | Yes | Post history with filters |

---

## Logging

### `setup_logging(log_level: str, log_file: str | None, log_rotation: str, enable_json: bool)`

```python
from xpst.utils.logger import setup_logging

setup_logging(
    log_level="INFO",
    log_file="~/.xpst/logs/xpst.log",
    log_rotation="10 MB",
    enable_json=True  # JSON file output
)
```

### `get_logger(name: str) -> logging.Logger`

```python
logger = get_logger("xpst.platforms.youtube")
logger.info("Uploading video", extra={"video_id": "123", "platform": "youtube"})
```

---

## Credential Store

### `CredentialStore(config_dir: str)`

```python
store = CredentialStore(config_dir)

# Store
store.store("youtube_token", token_json)
store.store_json("x_cookies", cookies_dict)

# Retrieve
token = store.retrieve("youtube_token")
cookies = store.retrieve_json("x_cookies")

# List
keys = store.list_keys()

# Storage type
store._use_keyring  # True if OS keychain, False if encrypted fallback
```

---

## Video Processor

### `VideoProcessor(ffmpeg_path: str | None = None)`

```python
processor = VideoProcessor()

# Encode for platform
output = processor.encode_for_platform(
    input_path=Path("input.mp4"),
    platform="instagram",
    output_path=Path("output.mp4")
)

# Generate thumbnail
thumb = processor.generate_thumbnail(
    video_path=Path("video.mp4"),
    output_path=Path("thumb.jpg"),
    timestamp="00:00:01"
)
```

---

## Anti-Bot

### `AntiBot(config)`

```python
anti_bot = AntiBot(config)

# Get jittered interval
interval = anti_bot.get_jittered_interval(base_interval=900)

# Random delay
await anti_bot.random_delay()
```

---

## Platform Uploaders

### Base Class: `PlatformUploader(config)`

```python
class YouTubeUploader(PlatformUploader):
    async def upload(video_path, caption) -> UploadResult
    async def health_check() -> dict
```

### `UploadResult`

```python
@dataclass
class UploadResult:
    success: bool
    post_id: str | None = None
    post_url: str | None = None
    platform: str
    metadata: dict | None = None
    error: str | None = None
```

---

## Video Sources

### Base Class: `VideoSource(config)`

```python
class TikTokSource(VideoSource):
    async def list_videos(max_count) -> list[VideoMetadata]
    async def download(video_id, output_dir) -> DownloadResult
    async def check_health() -> dict
```

### `VideoMetadata`

```python
@dataclass
class VideoMetadata:
    video_id: str
    url: str
    caption: str
    description: str
    duration: int
    width: int
    height: int
    view_count: int
    like_count: int
    timestamp: str | None
    author: str
    thumbnail_url: str
    hashtags: list[str]
    content_type: ContentType
    source_platform: str
    extra: dict
```

### `DownloadResult`

```python
@dataclass
class DownloadResult:
    success: bool
    video_path: Path | None = None
    media_paths: list[Path] | None = None
    format_used: str | None = None
    error: str | None = None
```

---

## Exceptions

- `CircuitBreakerOpenError` - Circuit breaker is open
- `QuotaExceededError` - Daily API quota exceeded
- `AuthenticationError` - Auth failed/expired
- `UploadError` - Platform upload failed
- `DownloadError` - Source download failed
- `ConfigurationError` - Invalid configuration
- `EncodingError` - Video encoding failed