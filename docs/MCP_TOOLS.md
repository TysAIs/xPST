# xPST MCP Server Tools

The xPST MCP server exposes local xPST workflows over stdio so AI assistants and automation tools can inspect setup, check status, run posting workflows, and query the personal content knowledge base without scraping CLI text.

This reference is generated from the live tool registry in `src/xpst/mcp/server.py` (xpst_* tools) and `src/xpst/knowledge/mcp/tools.py` (kb_* handlers). **23 tools total: 19 `xpst_*` + 4 `kb_*`.**

xPST posts to six destinations — YouTube, Instagram, X/Twitter, TikTok, Threads, and LinkedIn — and pulls source video from TikTok, YouTube, Instagram, X, and local files.

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
| `xpst_analytics` | Per-post & per-platform engagement metrics | Yes | None (read-only) |
| `xpst_cross_post_analytics` | One video across platforms, aggregated | Yes | None (read-only) |
| `xpst_followers` | Follower counts per platform with growth history | Yes | None (read-only) |
| `xpst_best_time` | Best time to post per platform | Yes | None (read-only) |
| `xpst_security_audit` | Automated security check on the install | No | None |
| `xpst_suggest_caption` | Generate AI caption suggestions for a video | No | None |
| `xpst_transcript` | Get transcript for a video by hash or ID | Yes | None (read-only) |
| `xpst_search` | Search the knowledge base | Yes | None (read-only) |
| `xpst_schedule_list` | List scheduled posts | Yes | None (read-only) |
| `xpst_schedule_add` | Schedule a post for a future time | Yes | **SCHEDULES REAL POSTS** |
| `xpst_run` | Fetch new content and cross-post it | Yes | **POSTS TO REAL ACCOUNTS** |
| `xpst_post` | Manually post a local video or carousel | Yes | **POSTS TO REAL ACCOUNTS** |
| `xpst_backfill` | Retry failed or incomplete posts | Yes | **POSTS TO REAL ACCOUNTS** |
| `xpst_delete` | Remove a post record from local state | Yes | Destructive to local state |
| `kb_add` | Ingest a file/URL into the knowledge base | No | Downloads + transcribes locally |
| `kb_query` | Search stored knowledge nuggets | No | None (read-only) |
| `kb_organize` | Cluster nuggets into areas, tag difficulty | No | Rewrites KB area assignments |
| `kb_areas` | List knowledge areas in course order | No | None (read-only) |

---

## xpst_providers

Lists all discovered content sources and posting destinations with auth modes and capabilities. Call this first so the agent adapts to the installed provider set instead of assuming a fixed platform list. Destinations are YouTube Shorts, Instagram Reels, X, TikTok, Threads, and LinkedIn; sources are TikTok, YouTube, Instagram, X, and local files. TikTok now appears under both `sources` and `destinations` (posting via the official Content Posting API).

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
    { "video_id": "7301...", "caption": "First 100 chars...", "source": "tiktok", "targets": ["youtube", "instagram", "x", "tiktok", "threads", "linkedin"] }
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
| `platforms` | string[] | no | all configured | Subset of `youtube`, `instagram`, `x`, `tiktok`, `threads`, `linkedin`. |
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
| `platforms` | string[] | no | all configured | Subset of `youtube`, `instagram`, `x`, `tiktok`, `threads`, `linkedin`. |
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
| `platform` | string | no | `"all"` | `youtube`, `instagram`, `x`, `tiktok`, `threads`, `linkedin`, or `all`. |

Example call:

```json
{ "name": "xpst_delete", "arguments": { "video_id": "7301234567890", "platform": "all" } }
```

Example response:

```json
{ "video_id": "7301234567890", "platform": "all", "removed": ["youtube", "x"], "success": true }
```

## xpst_analytics

Returns per-post and per-platform engagement metrics with snapshot history. Read-only; does not upload.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `platform` | string | no | all | `youtube`, `instagram`, `x`, `tiktok`, `threads`, or `linkedin`. |
| `live` | boolean | no | `false` | Fetch fresh metrics from platforms instead of stored snapshots. |

Example call:

```json
{ "name": "xpst_analytics", "arguments": { "platform": "youtube", "live": false } }
```

Response shape: per-platform engagement metrics (views, likes, comments, shares) with snapshot history.

## xpst_cross_post_analytics

Cross-post correlation analytics: aggregates how a single video performed across every platform it was posted to. Read-only.

Arguments: none.

Example call:

```json
{ "name": "xpst_cross_post_analytics", "arguments": {} }
```

Response shape: per-video records correlating one source video against its destination posts and their aggregated engagement.

## xpst_followers

Returns follower counts per platform with growth history. Read-only.

Arguments: none.

Example call:

```json
{ "name": "xpst_followers", "arguments": {} }
```

Response shape: per-platform follower counts plus historical growth points.

## xpst_best_time

Recommends the best time to post per platform based on engagement history. Read-only.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `platform` | string | no | all | Limit the recommendation to one platform. |

Example call:

```json
{ "name": "xpst_best_time", "arguments": { "platform": "instagram" } }
```

Response shape: recommended posting windows per platform derived from past engagement.

## xpst_security_audit

Runs an automated security check on the xPST installation (credential storage, file permissions, configuration hygiene). Read-only.

Arguments: none.

Example call:

```json
{ "name": "xpst_security_audit", "arguments": {} }
```

Response shape: a list of audit findings with severity and remediation hints.

## xpst_suggest_caption

Generates AI caption suggestions for a video file, optionally tuned for a target platform.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `video_path` | string | yes | — | Path to the video to caption. |
| `platform` | string | no | `"instagram"` | Target platform to tune the caption for. |

Example call:

```json
{ "name": "xpst_suggest_caption", "arguments": { "video_path": "/home/user/clips/demo.mp4", "platform": "instagram" } }
```

Response shape: one or more suggested captions.

## xpst_transcript

Returns the transcript for a video identified by content hash or video ID. Read-only.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `video_id` | string | yes | — | Video ID or content hash to fetch the transcript for. |

Example call:

```json
{ "name": "xpst_transcript", "arguments": { "video_id": "7301234567890" } }
```

Response shape: the transcript text (and timing segments when available) for the requested video.

## xpst_search

Searches the knowledge base for nuggets, clips, and topics. Read-only.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `query` | string | yes | — | Search text. |
| `limit` | integer | no | `10` | Maximum results to return. |

Example call:

```json
{ "name": "xpst_search", "arguments": { "query": "best thumbnail tips", "limit": 10 } }
```

Response shape: matching knowledge nuggets/clips with provenance.

## xpst_schedule_list

Lists scheduled posts (pending, completed, and failed). Read-only.

Arguments: none.

Example call:

```json
{ "name": "xpst_schedule_list", "arguments": {} }
```

Response shape: a list of scheduled posts with their status, target platforms, and scheduled times.

## xpst_schedule_add

Schedules a post for a future time: video + caption + ISO-8601 time, with an optional repeat rule. Creates a pending scheduled post.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `video_path` | string | yes | — | Path to the video to schedule. |
| `caption` | string | yes | — | Caption/title for the post. |
| `scheduled_time` | string | yes | — | ISO-8601 timestamp for publishing. |
| `platforms` | string[] | no | all configured | Subset of `youtube`, `instagram`, `x`, `tiktok`, `threads`, `linkedin`. |
| `repeat_rule` | string | no | none | `daily`, `weekly`, or `monthly`. |

Example call:

```json
{
  "name": "xpst_schedule_add",
  "arguments": {
    "video_path": "/home/user/clips/demo.mp4",
    "caption": "Scheduled demo!",
    "scheduled_time": "2026-07-01T14:00:00Z",
    "platforms": ["youtube", "instagram", "threads"]
  }
}
```

Response shape: a confirmation with the scheduled post's ID and stored details.

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

Note: `kb course` and `kb doctor` exist as CLI commands only (`xpst kb course`, `xpst kb doctor`) and are not exposed over MCP yet.

---

## Recommended assistant flow

1. `xpst_providers` to discover available sources and destinations.
2. `xpst_config_show` / `xpst_auth_status` to check setup without starting the posting engine.
3. `xpst_health` before real uploads when the user asks for a safety check.
4. `xpst_run` / `xpst_post` with `dry_run: true` for previews.
5. Live `xpst_run` / `xpst_post` only after the user explicitly confirms.
6. `kb_add` / `kb_query` / `kb_organize` / `kb_areas` to build and mine the user's content knowledge base.

## Error handling

Tools return JSON text when successful. On MCP-level failures, `isError` is set and the text content contains the error message. Platform-level failures are returned inside the JSON payload so one destination can fail without hiding the others. A missing optional extra (mcp/knowledge) surfaces as a clear install-hint error, never a crash.

## See also

- [Install Guide](INSTALL.md)
