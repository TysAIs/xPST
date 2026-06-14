# xPST MCP Server Tools

The xPST MCP server exposes local xPST workflows over stdio so AI assistants and automation tools can inspect setup, check status, run posting workflows, and query the personal content knowledge base without scraping CLI text.

This reference is generated from the live tool registry in `src/xpst/mcp/server.py` (xpst_* tools) and `src/xpst/knowledge/mcp/tools.py` (kb_* handlers). **17 tools total: 12 `xpst_*` + 5 `kb_*`.**

## Setup

Requires the optional extra: `pip install "xpst[mcp]"`. The kb_* tools additionally require `pip install "xpst[knowledge]"` (they return a clear install hint if the extra is missing).

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

You can also start the server with `xpst-mcp` or `xpst mcp`.

## Guardrails: these tools touch real accounts

**`xpst_post` and `xpst_run` upload to the user's REAL social media accounts.** There is no sandbox. Before any live call:

1. Use `dry_run: true` first and show the user what would happen.
2. Get explicit user confirmation before a live `xpst_run` or `xpst_post`.
3. `xpst_backfill` also performs live uploads when not in dry-run mode.
4. `xpst_delete` removes local post records; deleting a record for content that is still live on a platform can cause the engine to consider it "new" again. Treat it as destructive.

Metadata-only tools (`xpst_providers`, `xpst_config_show`, `xpst_auth_status`) never initialize the posting engine and are always safe to call.

## Tool index

| Tool | Purpose | Engine started | Live-account risk |
|------|---------|----------------|-------------------|
| `xpst_providers` | List source/destination providers and capabilities | No | None |
| `xpst_config_show` | Show sanitized configuration | No | None |
| `xpst_auth_status` | Credential storage status and quota remaining | No | None |
| `xpst_status` | Local state statistics and health | Yes | None (read-only) |
| `xpst_health` | Live source/platform connectivity checks | Yes | Touches credentials, no uploads |
| `xpst_analytics` | Per-post and per-platform engagement metrics | No by default | `live: true` touches platform APIs |
| `xpst_schedule_list` | List scheduled posts | No | None (read-only) |
| `xpst_schedule_add` | Schedule a local video for later posting | No | Mutates local schedule |
| `xpst_run` | Fetch new content and cross-post it | Yes | **POSTS TO REAL ACCOUNTS** |
| `xpst_post` | Manually post a local video or carousel | Yes | **POSTS TO REAL ACCOUNTS** |
| `xpst_backfill` | Retry failed or incomplete posts | Yes | **POSTS TO REAL ACCOUNTS** |
| `xpst_delete` | Remove a post record from local state | Yes | Destructive to local state |
| `kb_add` | Ingest a file/URL into the knowledge base | No | Downloads + transcribes locally |
| `kb_query` | Search stored knowledge nuggets | No | None (read-only) |
| `kb_organize` | Cluster nuggets into areas, tag difficulty | No | Rewrites KB area assignments |
| `kb_areas` | List knowledge areas in course order | No | None (read-only) |
| `kb_course` | Assemble organized areas into a cited course outline | No | None (read-only) |

---

## xpst_providers

Lists all discovered content sources and posting destinations with auth modes and capabilities. Call this first so the agent adapts to the installed provider set instead of assuming a fixed platform list. Note that TikTok appears only under `sources`: xPST does not post to TikTok.

Arguments: none.

Example call:

```json
{ "name": "xpst_providers", "arguments": {} }
```

Example response shape:

```json
{
  "sources": [
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

## xpst_config_show

Displays the current configuration with sensitive account values (client secrets, token/cookie/session file paths, passwords) masked.

Arguments: none.

Example call:

```json
{ "name": "xpst_config_show", "arguments": {} }
```

Response shape: a JSON object with `accounts`, `video`, `monitoring`, and `schedule` sections mirroring `~/.xpst/config.yaml`, with sensitive account fields replaced by `***MASKED***`.

## xpst_auth_status

Returns the credential storage mode (OS keychain vs encrypted file fallback), the list of stored credential keys, and per-platform authentication plus remaining daily quota.

Arguments: none.

Example call:

```json
{ "name": "xpst_auth_status", "arguments": {} }
```

Example response shape:

```json
{
  "credential_storage": "OS Keychain",
  "stored_credentials": ["youtube_token", "x_cookies"],
  "platforms": {
    "youtube": { "authenticated": true, "quota_remaining": 5 },
    "x": { "authenticated": true, "quota_remaining": 5 },
    "instagram": { "authenticated": false, "quota_remaining": 5 }
  }
}
```

## xpst_status

Returns local state statistics: tracked videos, processed counts, platform health, and dead-letter state. Read-only.

Arguments: none.

Example call:

```json
{ "name": "xpst_status", "arguments": {} }
```

Response shape: the engine's statistics dict (post counts per platform, failure counts, circuit-breaker state, DLQ size).

## xpst_health

Runs live source and platform connectivity checks. No uploads, but this does touch provider clients and stored credentials.

Arguments: none.

Example call:

```json
{ "name": "xpst_health", "arguments": {} }
```

Response shape: a per-provider health dict (`ok`/error detail for each configured source and destination).

## xpst_analytics

Returns per-post and per-platform engagement metrics from the local analytics snapshot store. By default this is offline and fast; set `live: true` only when the user wants a fresh platform API refresh.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `platform` | string | no | all | Optional platform filter: `youtube`, `x`, `instagram`, or `tiktok`. |
| `live` | boolean | no | `false` | Refresh from platform APIs before reading snapshots. |

Example call:

```json
{ "name": "xpst_analytics", "arguments": { "platform": "youtube", "live": false } }
```

Response shape: an analytics snapshot with totals, platform summaries, and recent per-post metrics.

## xpst_schedule_list

Lists scheduled posts from the active profile's schedule store. Read-only.

Arguments: none.

Example call:

```json
{ "name": "xpst_schedule_list", "arguments": {} }
```

Example response shape:

```json
{ "schedules": [] }
```

## xpst_schedule_add

Schedules a local video file for later posting through the active profile's scheduler. This mutates local scheduler state but does not upload immediately.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `confirm` | boolean | conditional | `false` | Required when `XPST_MCP_REQUIRE_CONFIRM=1`. |
| `video_path` | string | yes | - | Local video file path. |
| `caption` | string | yes | - | Post caption. |
| `scheduled_time` | string | yes | - | ISO-8601 local datetime, for example `2026-06-12T09:30:00`. |
| `platforms` | string[] | no | all enabled | Target platforms. |
| `repeat_rule` | string | no | none | One of `daily`, `weekly`, or `monthly`. |

Example call:

```json
{
  "name": "xpst_schedule_add",
  "arguments": {
    "video_path": "/home/user/clips/demo.mp4",
    "caption": "Scheduled demo",
    "scheduled_time": "2026-06-12T09:30:00",
    "platforms": ["youtube"]
  }
}
```

Example response shape:

```json
{ "scheduled": { "id": "abc123", "status": "pending" } }
```

## xpst_run

Checks a source for new videos and cross-posts them to all configured destinations. **Live mode posts to real accounts.**

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `max_posts` | integer | no | `5` | Maximum posts per cycle (1-50). |
| `source` | string | no | `"tiktok"` | One of `tiktok`, `youtube`, `x`, `instagram`, `local`. |
| `catch_up` | boolean | no | `false` | Fetch extra videos for catch-up mode. |
| `dry_run` | boolean | no | `false` | Preview without uploading. Always use first. |

Example call (safe preview):

```json
{ "name": "xpst_run", "arguments": { "source": "tiktok", "max_posts": 3, "dry_run": true } }
```

Dry-run response shape:

```json
{
  "dry_run": true,
  "fetch_count": 2,
  "videos": [
    { "video_id": "7301...", "caption": "First 100 chars...", "source": "tiktok", "targets": ["youtube", "instagram", "x"] }
  ]
}
```

Live-run response: currently a plain confirmation string (`"Cross-post cycle completed successfully"`). Per-post results with URLs are on the roadmap; use `xpst_status` to inspect outcomes after a run.

## xpst_post

Manually posts a local video file, or a carousel when `carousel_paths` is given. **Live mode posts to real accounts.**

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `video_path` | string | yes | — | Path to the video (or first carousel item). |
| `caption` | string | yes | — | Caption/title for the post. |
| `platforms` | string[] | no | all configured | Subset of `youtube`, `x`, `instagram`. |
| `carousel_paths` | string[] | no | `[]` | Additional image/video paths for a carousel. |
| `dry_run` | boolean | no | `false` | Preview without uploading. Always use first. |

Example call:

```json
{
  "name": "xpst_post",
  "arguments": {
    "video_path": "/home/user/clips/demo.mp4",
    "caption": "New demo!",
    "platforms": ["youtube", "x"],
    "dry_run": true
  }
}
```

Live response shape (per-platform upload results):

```json
{
  "video_id": "demo",
  "caption": "New demo!",
  "results": {
    "youtube": { "success": true, "url": "https://youtube.com/shorts/...", "error": null },
    "x": { "success": false, "url": null, "error": "..." }
  },
  "all_success": false,
  "partial_success": true
}
```

## xpst_backfill

Retries failed or incomplete posts from history. **Live mode posts to real accounts.**

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `max_count` | integer | no | `10` | Maximum videos to backfill. |
| `source` | string | no | `"tiktok"` | Source provider name. |
| `platforms` | string[] | no | all configured | Subset of `youtube`, `x`, `instagram`. |
| `dry_run` | boolean | no | `false` | Preview what would be backfilled. |

Example call:

```json
{ "name": "xpst_backfill", "arguments": { "max_count": 5, "dry_run": true } }
```

Live response shape:

```json
{
  "attempted": 2,
  "successful": 1,
  "results": [ { "video_id": "...", "results": { "youtube": { "success": true } }, "all_success": true, "partial_success": false } ]
}
```

## xpst_delete

Removes a post **record** from local state. This is state-only: it does NOT call any platform's delete API (use the CLI `xpst delete` for live deletion). Removing a record can make previously-posted content look "new" to the engine again, so confirm with the user.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `video_id` | string | yes | — | Video ID to remove from state. |
| `platform` | string | no | `"all"` | `youtube`, `x`, `instagram`, or `all`. |

Example call:

```json
{ "name": "xpst_delete", "arguments": { "video_id": "7301234567890", "platform": "all" } }
```

Example response:

```json
{ "video_id": "7301234567890", "platform": "all", "removed": ["youtube", "x"], "success": true }
```

---

## Knowledge-base tools

These mirror the `xpst kb` CLI and require the `xpst[knowledge]` extra. Handlers lazy-import the heavy subsystem (faster-whisper / fastembed / lancedb) only when invoked and run in a worker thread, so listing tools stays fast. All KB data lives in local workspaces (isolated data directories); `workspace` defaults to `"default"` on every tool.

## kb_add

Ingests a local file or URL into the knowledge base: downloads (if URL), transcribes (faster-whisper), extracts cited knowledge nuggets, embeds them, and stores them in the workspace. Transcription is CPU-bound and can take a while for long videos.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `source` | string | yes | — | Local file path or URL to ingest. |
| `workspace` | string | no | `"default"` | Workspace name (isolated data dir). |

Example call:

```json
{ "name": "kb_add", "arguments": { "source": "https://www.tiktok.com/@me/video/7301234567890" } }
```

Example response (`status` is `ingested`, `skipped`, or `failed`):

```json
{ "status": "ingested", "source": "https://...", "workspace": "default", "nugget_count": 12 }
```

## kb_query

Searches stored knowledge nuggets. The query is embedded and vector-searched against the store (top 8 results over MCP); when embeddings are unavailable it automatically falls back to exact-text substring matching. The response's `mode` field reports which path answered (`"semantic"` or `"substring"`). Every hit carries provenance and a similarity score (`score` is `null` in substring mode).

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `text` | string | yes | — | Query text. |
| `workspace` | string | no | `"default"` | Workspace name. |

Example call:

```json
{ "name": "kb_query", "arguments": { "text": "what makes a good thumbnail" } }
```

Example response shape:

```json
{
  "workspace": "default",
  "query": "what makes a good thumbnail",
  "mode": "semantic",
  "count": 1,
  "nuggets": [
    {
      "point": "Custom thumbnails lift CTR most in the first 24 hours.",
      "citation": "https://www.tiktok.com/@me/video/7301234567890",
      "source_url": "https://www.tiktok.com/@me/video/7301234567890",
      "timestamp_start": 42.5,
      "timestamp_end": 51.0,
      "score": 0.83,
      "area_id": "area-2"
    }
  ]
}
```

## kb_organize

Discovers knowledge areas by clustering nugget embeddings, tags difficulty, and assigns nuggets to areas. Rewrites the workspace's area assignments.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `workspace` | string | no | `"default"` | Workspace name. |
| `threshold` | number | no | built-in default | Cosine similarity threshold for clustering/routing. |

Example call:

```json
{ "name": "kb_organize", "arguments": { "workspace": "default" } }
```

Example response:

```json
{ "workspace": "default", "nugget_count": 48, "area_count": 5, "assigned": 46 }
```

## kb_areas

Lists discovered knowledge areas in course order (beginner to advanced). Read-only.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `workspace` | string | no | `"default"` | Workspace name. |

Example call:

```json
{ "name": "kb_areas", "arguments": {} }
```

Example response shape:

```json
{
  "workspace": "default",
  "count": 2,
  "areas": [
    { "order": 1, "label": "Hooks and openers", "nugget_count": 14 },
    { "order": 2, "label": "Retention editing", "nugget_count": 9 }
  ]
}
```

## kb_course

Assembles organized areas and cited nuggets into a course outline that an agent can turn into prose without inventing the structure. Read-only.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `workspace` | string | no | `"default"` | Workspace name. |
| `area_id` | string | no | all areas | Optional area id to assemble only one area. |

Example call:

```json
{ "name": "kb_course", "arguments": { "workspace": "default" } }
```

Example response shape:

```json
{
  "workspace": "default",
  "area_count": 1,
  "nugget_count": 3,
  "areas": [
    {
      "label": "Hooks and openers",
      "nuggets": [
        { "point": "Open with the result first.", "citation": "https://example.com/v" }
      ]
    }
  ]
}
```

Note: `kb doctor` exists as a CLI command only (`xpst kb doctor`) and is not exposed over MCP yet.

---

## Recommended assistant flow

1. `xpst_providers` to discover available sources and destinations.
2. `xpst_config_show` / `xpst_auth_status` to check setup without starting the posting engine.
3. `xpst_health` before real uploads when the user asks for a safety check.
4. `xpst_run` / `xpst_post` with `dry_run: true` for previews.
5. Live `xpst_run` / `xpst_post` only after the user explicitly confirms.
6. `kb_add` / `kb_query` / `kb_organize` / `kb_areas` / `kb_course` to build, mine, and assemble the user's content knowledge base.

## Error handling

Tools return JSON text when successful. On MCP-level failures, `isError` is set and the text content contains the error message. Platform-level failures are returned inside the JSON payload so one destination can fail without hiding the others. A missing optional extra (mcp/knowledge) surfaces as a clear install-hint error, never a crash.

## See also

- [Agent Guide](AGENT_GUIDE.md)
- [Install Guide](INSTALL.md)
- [Open Source Integrations](OPEN_SOURCE_INTEGRATIONS.md)
