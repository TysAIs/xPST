# xPST MCP Server — Tools & Resources Reference

> **Protocol:** [Model Context Protocol (MCP)](https://modelcontextprotocol.io)
> **Transport:** stdio
> **Server name:** `xPST`

The xPST MCP server exposes all cross-posting capabilities as MCP tools and resources, enabling AI assistants to manage video distribution across platforms.

---

## Quick Setup

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

Or via the CLI:

```bash
xpst mcp
```

---

## Tools (8)

### 1. `post_video`

Post a video file to one or more social media platforms.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `video_path` | `string` | **yes** | — | Path to the video file on disk |
| `caption` | `string` | **yes** | — | Caption/title for the post |
| `platforms` | `string[]` | no | `null` (all enabled) | Target platform names, e.g. `["youtube", "x"]` |

**Returns:**

```json
{
  "video_id": "abc123",
  "caption": "My awesome video...",
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
    "x": {
      "success": true,
      "post_url": "https://x.com/user/status/123",
      "post_id": "123",
      "error": null,
      "platform": "x"
    }
  }
}
```

**Example (from an AI assistant):**

> "Post the video at ~/Videos/test.mp4 with caption 'Summer vibes ☀️' to YouTube and Instagram."

The assistant calls:
```json
{
  "tool": "post_video",
  "arguments": {
    "video_path": "~/Videos/test.mp4",
    "caption": "Summer vibes ☀️",
    "platforms": ["youtube", "instagram"]
  }
}
```

---

### 2. `crosspost_new`

Check for new videos and cross-post them to all platforms.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `bidirectional` | `boolean` | no | `false` | Check ALL sources for bidirectional cross-posting |
| `limit` | `integer` | no | `10` | Maximum number of videos to process |

**Returns:**

```json
[
  {
    "video_id": "tiktok_abc123",
    "caption": "Original caption...",
    "all_success": true,
    "partial_success": false,
    "platforms": {
      "youtube": { "success": true, "post_url": "...", "post_id": "...", "error": null, "platform": "youtube" },
      "instagram": { "success": true, "post_url": "...", "post_id": "...", "error": null, "platform": "instagram" },
      "x": { "success": false, "post_url": null, "post_id": null, "error": "Rate limited", "platform": "x" }
    }
  }
]
```

**Example:**

> "Check for new videos and cross-post them."

```json
{
  "tool": "crosspost_new",
  "arguments": {
    "bidirectional": false,
    "limit": 5
  }
}
```

---

### 3. `check_status`

Check xPST health status including quotas and circuit breakers.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| *(none)* | — | — | — | — |

**Returns:**

```json
{
  "health": {
    "sources": { "tiktok": { "status": "ok", "username": "..." } },
    "platforms": {
      "youtube": { "authenticated": true, "session_valid": true },
      "instagram": { "authenticated": true, "session_valid": false },
      "x": { "authenticated": true, "session_valid": true }
    },
    "circuit_breakers": { "youtube": { "state": "closed", "failure_count": 0 } }
  },
  "statistics": {
    "total_videos_tracked": 42,
    "total_processed": 38,
    "last_check": "2025-06-01T12:00:00",
    "by_platform": { "youtube": 15, "instagram": 12, "x": 11 }
  },
  "quotas": {
    "youtube": { "used_today": 2, "daily_limit": 5, "remaining": 3 },
    "instagram": { "used_today": 1, "daily_limit": 5, "remaining": 4 }
  },
  "circuit_breakers": { "youtube": { "state": "closed", "failure_count": 0 } },
  "dead_letter_queue_count": 0
}
```

**Example:**

> "What's the current status of xPST?"

```json
{
  "tool": "check_status",
  "arguments": {}
}
```

---

### 4. `list_platforms`

List all configured platforms with auth status and capabilities.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| *(none)* | — | — | — | — |

**Returns:**

```json
{
  "youtube": {
    "enabled": true,
    "class": "YouTubeUploader",
    "supports_delete": true,
    "supports_carousel": false
  },
  "instagram": {
    "enabled": true,
    "class": "InstagramUploader",
    "supports_delete": true,
    "supports_carousel": true
  },
  "x": {
    "enabled": true,
    "class": "XUploader",
    "supports_delete": true,
    "supports_carousel": false
  }
}
```

**Example:**

> "Which platforms are configured and what can they do?"

```json
{
  "tool": "list_platforms",
  "arguments": {}
}
```

---

### 5. `get_analytics`

Get engagement analytics across platforms.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `platforms` | `string[]` | no | `null` (all) | Filter to specific platforms |
| `top_n` | `integer` | no | `10` | Number of top posts to return |

**Returns:**

```json
{
  "totals": { "posts": 30, "views": 150000, "likes": 8500, "comments": 320, "shares": 180 },
  "platform_totals": {
    "youtube": { "posts": 10, "views": 80000, "likes": 5000 },
    "instagram": { "posts": 10, "views": 45000, "likes": 2500 },
    "x": { "posts": 10, "views": 25000, "likes": 1000 }
  },
  "top_posts": [
    { "platform": "youtube", "post_id": "abc", "views": 50000, "likes": 3000, "comments": 150 }
  ],
  "post_count": 30
}
```

**Example:**

> "Show me analytics for YouTube and Instagram, top 5 posts."

```json
{
  "tool": "get_analytics",
  "arguments": {
    "platforms": ["youtube", "instagram"],
    "top_n": 5
  }
}
```

---

### 6. `delete_post`

Delete a previously posted video from a specific platform.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `post_id` | `string` | **yes** | — | The video/post identifier |
| `platform` | `string` | **yes** | — | Platform name (`"youtube"`, `"x"`, `"instagram"`) |

**Returns:**

```json
{
  "success": true,
  "post_id": "abc123",
  "platform": "youtube"
}
```

**Example:**

> "Delete the YouTube video with ID abc123."

```json
{
  "tool": "delete_post",
  "arguments": {
    "post_id": "abc123",
    "platform": "youtube"
  }
}
```

---

### 7. `health_check`

Perform a connectivity health check on all sources and platforms. Tests authentication without performing uploads.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| *(none)* | — | — | — | — |

**Returns:**

```json
{
  "sources": {
    "tiktok": { "status": "ok", "yt_dlp_version": "2025.1.1", "username": "creator", "cookies_available": true }
  },
  "platforms": {
    "youtube": { "authenticated": true, "session_valid": true, "details": { "channel_name": "My Channel" } },
    "instagram": { "authenticated": true, "session_valid": true, "details": { "username": "myuser" } },
    "x": { "authenticated": true, "session_valid": true, "details": { "username": "myuser" } }
  },
  "circuit_breakers": {
    "youtube": { "state": "closed", "failure_count": 0 },
    "instagram": { "state": "closed", "failure_count": 0 }
  },
  "quotas": {
    "youtube": { "remaining": 3, "daily_limit": 5 },
    "instagram": { "remaining": 4, "daily_limit": 5 }
  }
}
```

**Example:**

> "Can you check if all my social media connections are working?"

```json
{
  "tool": "health_check",
  "arguments": {}
}
```

---

### 8. `get_logs`

Retrieve recent log entries.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `lines` | `integer` | no | `50` | Number of log lines to return |
| `level` | `string` | no | `"INFO"` | Minimum log level filter: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Returns:**

```json
[
  "2025-06-01 12:00:00 [INFO] Checking for new videos...",
  "2025-06-01 12:00:05 [INFO] Found 2 new videos",
  "2025-06-01 12:00:10 [WARNING] Instagram rate limit approaching (4/5)",
  "2025-06-01 12:01:00 [INFO] Successfully posted to YouTube"
]
```

**Example:**

> "Show me the last 20 error-level log entries."

```json
{
  "tool": "get_logs",
  "arguments": {
    "lines": 20,
    "level": "ERROR"
  }
}
```

---

## Resources (3)

### 1. `xpst://config`

Returns the current xPST configuration (sanitized — no secrets).

**URI:** `xpst://config`

**Returns (JSON string):**

```json
{
  "config_dir": "~/.xpst",
  "youtube_enabled": true,
  "x_enabled": true,
  "instagram_enabled": true,
  "download_dir": "~/.xpst/downloads",
  "check_interval": 900,
  "notifications_enabled": false,
  "rate_limits": {
    "youtube": 5,
    "instagram": 5,
    "x": 5,
    "tiktok": 5
  }
}
```

> **Note:** Sensitive values (tokens, cookies, passwords) are never exposed through this resource.

---

### 2. `xpst://state`

Returns the current cross-posting state summary.

**URI:** `xpst://state`

**Returns (JSON string):**

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
    "youtube": { "status": "ok", "failures": 0, "last_success": "2025-06-01T12:00:00" },
    "instagram": { "status": "ok", "failures": 0, "last_success": "2025-06-01T11:45:00" }
  }
}
```

---

### 3. `xpst://health`

Returns current system health status.

**URI:** `xpst://health`

**Returns (JSON string):**

```json
{
  "statistics": {
    "total_videos_tracked": 42,
    "total_processed": 38,
    "last_check": "2025-06-01T12:00:00"
  },
  "quotas": {
    "youtube": { "used_today": 2, "daily_limit": 5, "remaining": 3 },
    "instagram": { "used_today": 1, "daily_limit": 5, "remaining": 4 },
    "x": { "used_today": 0, "daily_limit": 5, "remaining": 5 }
  }
}
```

---

## AI Assistant Integration Patterns

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "xpst": {
      "command": "xpst-mcp"
    }
  }
}
```

### Cursor / Windsurf

Add to your MCP settings:

```json
{
  "xpst": {
    "command": "xpst-mcp",
    "transport": "stdio"
  }
}
```

### Common AI Workflows

| Intent | Tool(s) to Call |
|--------|-----------------|
| "Post this video to all platforms" | `post_video` |
| "Check for new content and cross-post" | `crosspost_new` |
| "Is everything working?" | `health_check` or `check_status` |
| "What platforms can I post to?" | `list_platforms` |
| "Show me my video performance" | `get_analytics` |
| "Delete that Instagram post" | `delete_post` |
| "Check recent errors" | `get_logs` with `level=ERROR` |
| "What's my config?" | Resource: `xpst://config` |
| "How many videos have I posted?" | Resource: `xpst://state` |

---

## Error Handling

All tools return structured error information when something fails:

```json
{
  "success": false,
  "error": "Video file not found: ~/missing.mp4"
}
```

Platform upload results include per-platform errors:

```json
{
  "platforms": {
    "youtube": { "success": true, "post_url": "..." },
    "x": { "success": false, "error": "Session expired — run: xpst auth x" }
  }
}
```

---

## See Also

- [Agent Guide](AGENT_GUIDE.md) — CLI `--json` usage and Python API
- [Install Guide](INSTALL.md) — Setup instructions
- [X Auth Guide](X_AUTH_GUIDE.md) — Cookie-based authentication
