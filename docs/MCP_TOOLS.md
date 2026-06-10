# xPST MCP Server Tools

The xPST MCP server exposes local xPST workflows over stdio so AI assistants and automation tools can inspect setup, check status, and run posting workflows without scraping CLI text.

## Setup

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

You can also start the server with:

```bash
xpst-mcp
```

## Tools

Current tools exposed by `src/xpst/mcp/server.py`:

| Tool | Purpose | Engine required |
|------|---------|-----------------|
| `xpst_providers` | List supported source and destination providers with auth modes and capabilities. | No |
| `xpst_config_show` | Show sanitized configuration. | No |
| `xpst_auth_status` | Show credential storage status, stored credential keys, and quota remaining. | No |
| `xpst_status` | Show state statistics and health status. | Yes |
| `xpst_health` | Run live source/platform health checks. | Yes |
| `xpst_run` | Fetch new content and cross-post it. | Yes |
| `xpst_post` | Manually post a local video or carousel. | Yes |
| `xpst_backfill` | Retry failed or incomplete posts. | Yes |
| `xpst_delete` | Remove a post record from local state. | Yes |

Metadata-only tools do not initialize the posting engine, which keeps support and discovery calls fast and low-risk.

## `xpst_providers`

Lists all discovered content sources and posting destinations. Use this before calling posting tools so an assistant can adapt to the installed provider set instead of assuming a fixed platform list.

Input:

```json
{}
```

Example output:

```json
{
  "sources": [
    {
      "name": "local",
      "display_name": "Local Files",
      "roles": ["source"],
      "capabilities": ["list", "download", "carousel", "health", "local_only"],
      "auth_mode": "local",
      "is_official_api": false,
      "is_local_first": true
    },
    {
      "name": "tiktok",
      "display_name": "TikTok",
      "roles": ["source"],
      "capabilities": ["list", "download", "carousel", "health", "cookie_auth", "rate_limits"],
      "auth_mode": "cookies",
      "is_official_api": false,
      "is_local_first": true
    }
  ],
  "destinations": [
    {
      "name": "youtube",
      "display_name": "YouTube Shorts",
      "roles": ["destination"],
      "capabilities": ["upload", "delete", "health", "official_api", "oauth", "rate_limits"],
      "auth_mode": "oauth",
      "is_official_api": true,
      "is_local_first": true
    }
  ]
}
```

## `xpst_run`

Checks a source for new videos and cross-posts them.

Parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_posts` | integer | `5` | Maximum posts to process. |
| `source` | string | `tiktok` | Source provider name. |
| `catch_up` | boolean | `false` | Fetch extra videos for catch-up mode. |
| `dry_run` | boolean | `false` | Preview without uploading. |

Dry-run output includes the videos that would be fetched and the current destination targets.

## `xpst_post`

Posts a local video file or carousel.

Parameters:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `video_path` | string | yes | Local path to the video or first carousel item. |
| `caption` | string | yes | Caption/title for the post. |
| `platforms` | string array | no | Destination provider names. Defaults to all configured destinations. |
| `carousel_paths` | string array | no | Additional image/video paths for carousel posts. |
| `dry_run` | boolean | no | Preview without uploading. |

## `xpst_health`

Runs live source and platform health checks. This may touch provider clients and credentials.

Input:

```json
{}
```

## `xpst_status`

Returns local state statistics, including tracked videos, processed counts, platform health, and dead-letter state.

Input:

```json
{}
```

## `xpst_backfill`

Retries incomplete or failed posts.

Parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_count` | integer | `10` | Maximum videos to backfill. |
| `source` | string | `tiktok` | Source provider name. |
| `platforms` | string array | all | Destination provider names. |
| `dry_run` | boolean | `false` | Preview without uploading. |

## `xpst_config_show`

Returns sanitized configuration. Token, cookie, session, and password-like values are masked.

Input:

```json
{}
```

## `xpst_auth_status`

Returns credential storage mode, stored credential keys, and daily quota remaining for known destinations.

Input:

```json
{}
```

## `xpst_delete`

Deletes a post record from local state. It does not delete from the remote platform in the current MCP handler.

Parameters:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `video_id` | string | yes | Source video ID to remove from state. |
| `platform` | string | no | `youtube`, `x`, `instagram`, or `all`. Defaults to `all`. |

## Recommended Assistant Flow

1. Call `xpst_providers` to discover available sources and destinations.
2. Call `xpst_config_show` or `xpst_auth_status` to check setup without starting the posting engine.
3. Call `xpst_health` before real uploads when the user asks for a safety check.
4. Use `xpst_post` with `dry_run: true` for previews.
5. Use `xpst_post` or `xpst_run` only after the user confirms the intended action.

## Error Handling

Tools return JSON text when successful. On MCP-level failures, `isError` is set and the text content contains the error message. Platform-level failures are returned inside the JSON payload so one destination can fail without hiding the others.

## See Also

- [Agent Guide](AGENT_GUIDE.md)
- [Install Guide](INSTALL.md)
- [Open Source Integrations](OPEN_SOURCE_INTEGRATIONS.md)
- [Ship Readiness Plan](SHIP_READINESS_PLAN.md)
