# xPST Agent Guide

Everything an AI agent, automation script, or developer needs to use xPST programmatically.

---

## CLI with `--json` Flag

Every xPST CLI command supports `--json` for machine-readable JSON output. JSON mode is also **auto-enabled** when stdout is not a TTY (e.g., piped to `jq` or another program).

### Core Commands

#### `xpst run --json`

Check for new videos and cross-post them.

```bash
xpst run --json
xpst run --json --bidirectional
xpst run --json --dry-run
```

**Output:**

```json
{
  "status": "ok",
  "results": [
    {
      "video_id": "tiktok_abc123",
      "caption": "Original caption...",
      "all_success": true,
      "partial_success": false,
      "platforms": {
        "youtube": {
          "success": true,
          "post_url": "https://youtube.com/shorts/xyz",
          "post_id": "xyz",
          "error": null,
          "platform": "youtube"
        },
        "instagram": {
          "success": true,
          "post_url": "https://instagram.com/reel/abc",
          "post_id": "abc",
          "error": null,
          "platform": "instagram"
        }
      }
    }
  ]
}
```

When no new videos are found:

```json
{
  "status": "no_new_videos",
  "results": []
}
```

---

#### `xpst post --json`

Manually post a video to platforms.

```bash
xpst post --video ./my_video.mp4 --caption "Hello!" --json
xpst post --video ./clip.mp4 --caption "Hello!" --platforms youtube,instagram --json
xpst post --video ./img1.jpg --video ./img2.jpg --caption "Album" --json
xpst post --video ./clip.mp4 --caption "Test" --dry-run --json
```

**Output:**

```json
{
  "video_id": "local_abc123",
  "caption": "Hello!",
  "all_success": true,
  "partial_success": false,
  "platforms": {
    "youtube": {
      "success": true,
      "post_url": "https://youtube.com/shorts/xyz",
      "post_id": "xyz",
      "error": null,
      "platform": "youtube"
    }
  }
}
```

Dry run output:

```json
{
  "dry_run": true,
  "video": "./my_video.mp4",
  "caption": "Hello!",
  "carousel": false,
  "items": 1,
  "targets": ["youtube", "instagram", "x"]
}
```

---

#### `xpst health --json`

Test connectivity to all platforms (no uploads).

```bash
xpst health --json
```

**Output:**

```json
{
  "sources": {
    "tiktok": {
      "status": "ok",
      "yt_dlp_version": "2025.1.1",
      "username": "creator",
      "cookies_available": true
    }
  },
  "platforms": {
    "youtube": {
      "authenticated": true,
      "session_valid": true,
      "details": {
        "channel_name": "My Channel"
      }
    },
    "instagram": {
      "authenticated": true,
      "session_valid": true,
      "details": {
        "username": "myuser"
      }
    },
    "x": {
      "authenticated": true,
      "session_valid": true,
      "details": {
        "username": "myuser"
      }
    }
  },
  "circuit_breakers": {
    "youtube": { "state": "closed", "failure_count": 0 },
    "instagram": { "state": "closed", "failure_count": 0 },
    "x": { "state": "closed", "failure_count": 0 }
  },
  "quotas": {
    "youtube": { "remaining": 3, "daily_limit": 5 },
    "instagram": { "remaining": 4, "daily_limit": 5 },
    "x": { "remaining": 5, "daily_limit": 5 }
  }
}
```

---

#### `xpst status --json`

Show health status, statistics, and quota usage.

```bash
xpst status --json
```

**Output:**

```json
{
  "total_videos_tracked": 42,
  "total_processed": 38,
  "last_check": "2025-06-01T12:00:00",
  "by_platform": {
    "youtube": 15,
    "instagram": 12,
    "x": 11
  },
  "platform_health": {
    "youtube": {
      "status": "ok",
      "failures": 0,
      "last_success": "2025-06-01T12:00:00"
    }
  }
}
```

---

#### `xpst version --json`

Show xPST and all dependency versions.

```bash
xpst version --json
```

**Output:**

```json
{
  "xpst": "0.1.0",
  "python": "3.11.15",
  "yt-dlp": "2025.1.1",
  "click": "8.1.8",
  "rich": "13.9.4"
}
```

---

#### `xpst logs --json`

View recent log entries.

```bash
xpst logs --json
```

**Output:**

```json
{
  "logs": [
    "2025-06-01 12:00:00 [INFO] Checking for new videos...",
    "2025-06-01 12:00:05 [INFO] Found 2 new videos"
  ]
}
```

---

#### `xpst backfill --json`

Retry failed or incomplete posts.

```bash
xpst backfill --json
xpst backfill --platforms youtube --limit 5 --json
xpst backfill --dry-run --json
```

**Output:**

```json
{
  "status": "ok",
  "results": [
    {
      "video_id": "abc123",
      "caption": "Previously failed video...",
      "all_success": true,
      "platforms": { "youtube": { "success": true, "post_url": "..." } }
    }
  ]
}
```

---

#### `xpst delete --json`

Delete a posted video from platforms.

```bash
xpst delete <video_id> --json
xpst delete <video_id> --platform instagram --json
xpst delete <video_id> --yes --json
```

**Output:**

```json
{
  "video_id": "abc123",
  "results": [
    { "platform": "youtube", "deleted": true },
    { "platform": "instagram", "deleted": true },
    { "platform": "x", "deleted": false }
  ]
}
```

---

#### `xpst analytics --json`

Show cross-platform analytics.

```bash
xpst analytics --json
xpst analytics --platforms youtube,instagram --json
xpst analytics --refresh --json
```

**Output:**

```json
{
  "platforms": {
    "youtube": { "posts": 10, "views": 80000, "likes": 5000, "comments": 200 },
    "instagram": { "posts": 10, "views": 45000, "likes": 2500, "comments": 120 }
  }
}
```

---

#### `xpst auth status --json`

Show authentication and quota status.

```bash
xpst auth status --json
```

**Output:**

```json
{
  "credential_storage": "OS Keychain",
  "stored_credentials": ["youtube_token", "x_cookies", "instagram_session"],
  "platforms": {
    "youtube": { "authenticated": true, "quota_remaining": 3 },
    "x": { "authenticated": true, "quota_remaining": 5 },
    "instagram": { "authenticated": true, "quota_remaining": 4 }
  }
}
```

---

### Configuration Commands

#### `xpst config show --json`

```bash
xpst config show --json
xpst config show --raw --json
```

#### `xpst config set`

```bash
xpst config set accounts.youtube.enabled true --json
xpst config set rate_limits.youtube 10 --json
```

**Output:**

```json
{
  "key": "rate_limits.youtube",
  "value": 10,
  "saved": true
}
```

#### `xpst config validate --json`

```bash
xpst config validate --json
```

**Output:**

```json
{
  "valid": true,
  "checks": [
    { "name": "Config file loaded", "ok": true, "detail": "OK" },
    { "name": "YouTube credentials", "ok": true, "detail": "~/.xpst/credentials/youtube_client_secrets.json" }
  ]
}
```

#### `xpst config export --json`

```bash
xpst config export backup.yaml --json
```

#### `xpst config import --json`

```bash
xpst config import backup.yaml --yes --json
```

---

### Schedule Commands

#### `xpst schedule add --json`

```bash
xpst schedule add video.mp4 --caption "Post later" --at "2026-06-10 14:00" --json
```

**Output:**

```json
{
  "id": "sch_abc123",
  "video_path": "/path/to/video.mp4",
  "caption": "Post later",
  "scheduled_time": "2026-06-10T14:00:00",
  "platforms": null,
  "status": "pending"
}
```

#### `xpst schedule list --json`

```bash
xpst schedule list --json
```

**Output:**

```json
[
  {
    "id": "sch_abc123",
    "video_path": "/path/to/video.mp4",
    "caption": "Post later",
    "scheduled_time": "2026-06-10T14:00:00",
    "platforms": ["youtube", "instagram"],
    "status": "pending"
  }
]
```

#### `xpst schedule remove --json`

```bash
xpst schedule remove sch_abc123 --json
```

#### `xpst schedule run --json`

```bash
xpst schedule run --json
xpst schedule run --dry-run --json
```

#### `xpst schedule install --json`

```bash
xpst schedule install --interval 15 --json
xpst schedule install --remove --json
```

---

## MCP Server

### Starting the Server

```bash
# Standalone
xpst-mcp

# Via CLI
xpst mcp
```

### Client Configuration

```json
{
  "mcpServers": {
    "xpst": {
      "command": "xpst-mcp",
      "transport": "stdio"
    }
  }
}
```

### Tool Schemas

See the full [MCP Tools Reference](MCP_TOOLS.md) for all 8 tools and 3 resources with complete schemas.

**Quick reference:**

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `post_video` | Upload video to platforms | `video_path`, `caption`, `platforms` |
| `crosspost_new` | Check & cross-post new videos | `bidirectional`, `limit` |
| `check_status` | System health + quotas + circuit breakers | *(none)* |
| `list_platforms` | Configured platforms + capabilities | *(none)* |
| `get_analytics` | Engagement metrics | `platforms`, `top_n` |
| `delete_post` | Remove a post | `post_id`, `platform` |
| `health_check` | Connectivity test (no uploads) | *(none)* |
| `get_logs` | Recent log entries | `lines`, `level` |

---

## Programmatic Python API

### Basic Usage

```python
import asyncio
from pathlib import Path
from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine

# Load config
config = XPSTConfig.load()  # Loads ~/.xpst/config.yaml

# Create engine
engine = CrossPostEngine(config)

# Check for new videos and cross-post
results = asyncio.run(engine.check_and_post())
for result in results:
    print(f"{result.video_id}: {'OK' if result.all_success else 'PARTIAL'}")
    for platform, upload_result in result.results.items():
        print(f"  {platform}: {upload_result.post_url or upload_result.error}")
```

### Manual Post

```python
import asyncio
from pathlib import Path
from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine

config = XPSTConfig.load()
engine = CrossPostEngine(config)

# Post a single video
result = asyncio.run(engine.post_manual(
    video_path=Path("~/Videos/my_video.mp4"),
    caption="Hello from xPST!",
    platforms=["youtube", "instagram"]
))

print(result.all_success)  # True if all platforms succeeded
print(result.results["youtube"].post_url)  # YouTube URL
```

### Carousel Post

```python
result = asyncio.run(engine.post_manual_carousel(
    media_paths=[Path("img1.jpg"), Path("img2.jpg"), Path("img3.jpg")],
    caption="My album post",
    platforms=["instagram"]
))
```

### Health Check

```python
import asyncio
from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine

config = XPSTConfig.load()
engine = CrossPostEngine(config)

health = asyncio.run(engine.check_health())

# Check platform status
for platform, info in health.get("platforms", {}).items():
    status = "✅" if info.get("authenticated") and info.get("session_valid") else "❌"
    print(f"{status} {platform}")
```

### Analytics

```python
import asyncio
from xpst.config import XPSTConfig
from xpst.analytics import AnalyticsCollector

config = XPSTConfig.load()
collector = AnalyticsCollector(config.config_dir)

post_ids = collector._discover_post_ids()
data = asyncio.run(collector.collect_all(post_ids))
totals = collector.get_total_metrics(data)

print(f"Total views: {totals.get('views', 0):,}")
print(f"Total likes: {totals.get('likes', 0):,}")
```

### State Management

```python
from xpst.config import XPSTConfig
from xpst.state import StateManager

config = XPSTConfig.load()
state = StateManager(config.config_dir)

stats = state.get_statistics()
print(f"Videos tracked: {stats['total_videos_tracked']}")
print(f"Processed: {stats['total_processed']}")

# Dead letter queue (failed posts)
dlq = state.get_dead_letter_queue()
for item in dlq:
    print(f"  Failed: {item['video_id']} → {item['platform']}: {item.get('errors')}")
```

### Quota Management

```python
from xpst.config import XPSTConfig
from xpst.utils.quota import QuotaManager

config = XPSTConfig.load()
quota = QuotaManager(config.config_dir)

status = quota.get_status()
for platform, info in status.items():
    print(f"{platform}: {info['remaining']}/{info['daily_limit']} remaining today")

remaining = quota.get_remaining("youtube")
print(f"YouTube daily remaining: {remaining.get('daily')}")
```

### Credential Store

```python
from xpst.utils.credentials import CredentialStore

store = CredentialStore("~/.xpst")

# Store a credential
store.store("youtube_token", token_json_string)

# Retrieve a credential
token = store.retrieve("youtube_token")

# Store JSON (cookies, sessions)
store.store_json("x_cookies", cookies_dict)
cookies = store.retrieve_json("x_cookies")

# List stored credentials
keys = store.list_keys()
```

---

## Exit Codes

All CLI commands use consistent exit codes:

| Code | Constant | Meaning |
|------|----------|---------|
| `0` | `EXIT_SUCCESS` | Command completed successfully |
| `1` | `EXIT_GENERAL` | General error (file not found, invalid input) |
| `2` | `EXIT_AUTH_FAILURE` | Authentication failed (expired cookies, invalid credentials) |
| `3` | `EXIT_RATE_LIMIT` | Rate limit exceeded |
| `4` | `EXIT_CONFIG_ERROR` | Configuration error (missing file, invalid values) |
| `10` | `EXIT_PLATFORM_UNAVAILABLE` | Platform API unavailable (e.g., dashboard can't start) |

### Error Categories

Errors are automatically categorized for retry decisions:

**Retryable** (auto-retried with backoff):
- Network timeouts (`ConnectionError`, `TimeoutError`)
- HTTP 429, 500, 502, 503, 504
- DNS resolution failures
- Connection resets
- Rate limits

**Fatal** (no retry, fail immediately):
- HTTP 401 (Unauthorized — session expired)
- HTTP 403 (Forbidden — banned/invalid credentials)
- Invalid video format (unsupported codec/resolution)
- File not found
- Quota exceeded
- Invalid configuration

**Platform-specific fatal errors:**
- `YOUTUBE_QUOTA_EXCEEDED`
- `X_SESSION_EXPIRED`
- `IG_SESSION_EXPIRED`
- `IG_INVALID_FORMAT`

---

## Pipe-Friendly Patterns

```bash
# Extract YouTube post URLs from last run
xpst run --json | jq -r '.results[].platforms.youtube.post_url'

# Count failed platforms
xpst status --json | jq '[.platform_health | to_entries[] | select(.value.status != "ok")] | length'

# Check if any platform needs attention
xpst health --json | jq '.platforms | to_entries[] | select(.value.session_valid == false) | .key'

# Get top video by views
xpst analytics --json | jq '.top_posts[0]'

# Monitor logs for errors in real time
xpst logs --json | jq -r '.logs[] | select(contains("ERROR"))'

# List all scheduled posts
xpst schedule list --json | jq '.[] | select(.status == "pending")'
```

---

## See Also

- [MCP Tools Reference](MCP_TOOLS.md) — AI agent tool schemas
- [Install Guide](INSTALL.md) — Setup instructions
- [X Auth Guide](X_AUTH_GUIDE.md) — Cookie-based authentication
- [Open Source Integrations](OPEN_SOURCE_INTEGRATIONS.md) — Dependency audit
