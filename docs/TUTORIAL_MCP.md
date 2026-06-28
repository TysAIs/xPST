# xPST MCP (Model Context Protocol) Tutorial

> **Complete guide** to using xPST's 23 MCP tools with AI agents. Drive the entire cross-posting workflow from Claude Desktop, Cursor, or any MCP-compatible client.

---

## Screenshots

> Client integration screenshots are referenced from `docs/assets/`. Capture
> and replace the placeholders below before a public release.

| Step | Placeholder | Description |
|------|-------------|-------------|
| 1 | `docs/assets/screenshot-mcp-claude.png` | Claude Desktop MCP config and tool invocation |
| 2 | `docs/assets/screenshot-mcp-cursor.png` | Cursor agent calling an xPST MCP tool |
| 3 | `docs/assets/screenshot-mcp-health.png` | `xpst_health` tool output in an agent session |
| 4 | `docs/assets/screenshot-mcp-post.png` | `xpst_post` cross-posting workflow result |

---

## Table of Contents

1. [What is MCP?](#what-is-mcp)
2. [Starting the MCP Server](#starting-the-mcp-server)
3. [Connecting to Claude Desktop](#connecting-to-claude-desktop)
4. [Tool Reference](#tool-reference)
5. [Knowledge Base Tools](#knowledge-base-tools)
6. [Security & Guardrails](#security--guardrails)
7. [Examples](#examples)
8. [Troubleshooting](#troubleshooting)

---

## What is MCP?

The **Model Context Protocol** (MCP) is an open standard that lets AI assistants interact with external tools and data sources. xPST exposes 23 MCP tools that let any MCP-compatible AI agent:

- Fetch and post videos across platforms
- Check platform health and auth status
- View analytics and manage schedules
- Query a semantic knowledge base of your content
- Delete posts and manage configuration

This means you can tell Claude "post my latest video to YouTube and X" and it will happen automatically.

---

## Starting the MCP Server

### From the CLI

```bash
# Start the MCP server (runs over stdio)
xpst mcp
```

The server reads JSON-RPC messages from stdin and writes responses to stdout. It runs indefinitely until the client disconnects.

### From the Desktop App

1. Open xPST desktop app
2. Go to **Settings**
3. Find the **MCP Server** section
4. Click **Start**

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `XPST_MCP_READONLY` | `false` | When `true`, only read-only tools (status, health, analytics, config) are available |
| `XPST_MCP_REQUIRE_CONFIRM` | `false` | When `true`, mutating tools require explicit confirmation |

---

## Connecting to Claude Desktop

Add xPST to your Claude Desktop configuration file:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Linux:** `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "xpst": {
      "command": "python",
      "args": ["-m", "xpst", "mcp"],
      "env": {
        "XPST_MCP_READONLY": "false"
      }
    }
  }
}
```

Restart Claude Desktop. You should see "xpst" in the tools list.

### From Source

If running from a source checkout:

```json
{
  "mcpServers": {
    "xpst": {
      "command": "/path/to/xPST/.venv/bin/python",
      "args": ["-m", "xpst", "mcp"],
      "cwd": "/path/to/xPST"
    }
  }
}
```

---

## Tool Reference

xPST exposes 23 MCP tools organized into 5 categories: Core Operations (6), Analytics & Insights (5), Content & Knowledge (4), Configuration & Scheduling (4), and Knowledge Base (4). All posting destinations — YouTube, Instagram, X/Twitter, TikTok, Threads, and LinkedIn — are valid `platforms` values.

### Core Operations (6 tools)

#### `xpst_run`

Check for new videos from a source and cross-post them to all enabled platforms.

```json
{
  "source": "tiktok",        // tiktok | youtube | x | instagram | local | all
  "dry_run": false,          // preview without uploading
  "max_posts": 5             // optional limit
}
```

**Returns:** List of cross-post results with per-platform status.

#### `xpst_post`

Manually post a video file to selected platforms.

```json
{
  "video_path": "/path/to/video.mp4",
  "caption": "Check out this video!",
  "platforms": ["youtube", "x", "threads", "linkedin"]  // optional; any of youtube, instagram, x, tiktok, threads, linkedin; defaults to all enabled
}
```

**Returns:** Cross-post result with post URLs and per-platform status.

#### `xpst_delete`

Remove a post record from local state (does not call a platform delete API).

```json
{
  "video_id": "video-abc123",
  "platform": "youtube"  // youtube | instagram | x | tiktok | threads | linkedin | all
}
```

**Returns:** Deletion confirmation.

#### `xpst_backfill`

Retry failed or incomplete posts.

```json
{
  "max_count": 10,
  "source": "tiktok",
  "platforms": ["youtube", "instagram", "x", "tiktok", "threads", "linkedin"]
}
```

**Returns:** List of retried posts with outcomes.

#### `xpst_health`

Test connectivity to all platforms without uploading.

```json
{}
```

**Returns:** Per-platform health status (ok/error with details).

#### `xpst_status`

Show overall xPST status — engine state, platform availability, recent activity.

```json
{}
```

**Returns:** JSON status object.

---

### Analytics & Insights (5 tools)

#### `xpst_analytics`

Retrieve per-post and per-platform engagement metrics with snapshot history.

```json
{
  "platform": "youtube",  // optional; youtube | instagram | x | tiktok | threads | linkedin; defaults to all
  "live": false           // optional; fetch fresh metrics instead of stored snapshots
}
```

**Returns:** Analytics data with views, likes, comments, shares per platform.

#### `xpst_cross_post_analytics`

Cross-post correlation analytics: how one video performed across every platform it was posted to, aggregated.

```json
{}
```

**Returns:** Per-video records correlating a source video against its destination posts.

#### `xpst_followers`

Follower counts per platform with growth history.

```json
{}
```

**Returns:** Per-platform follower counts plus historical growth points.

#### `xpst_best_time`

Best time to post per platform, derived from engagement history.

```json
{
  "platform": "instagram"  // optional; limit to one platform
}
```

**Returns:** Recommended posting windows per platform.

#### `xpst_schedule_list`

List all scheduled posts (pending, completed, failed).

```json
{}
```

**Returns:** List of scheduled posts with timestamps and target platforms.

---

### Content & Knowledge (4 tools)

#### `xpst_suggest_caption`

Generate AI caption suggestions for a video file.

```json
{
  "video_path": "/path/to/video.mp4",
  "platform": "instagram"  // optional; tune for a target platform
}
```

**Returns:** One or more suggested captions.

#### `xpst_transcript`

Get the transcript for a video by content hash or video ID.

```json
{
  "video_id": "7301234567890"
}
```

**Returns:** Transcript text (with timing segments when available).

#### `xpst_search`

Search the knowledge base for nuggets, clips, and topics.

```json
{
  "query": "best thumbnail tips",
  "limit": 10
}
```

**Returns:** Matching knowledge nuggets/clips with provenance.

#### `xpst_security_audit`

Run an automated security check on the xPST installation.

```json
{}
```

**Returns:** Audit findings with severity and remediation hints.

---

### Configuration & Scheduling (4 tools)

#### `xpst_config_show`

Display current configuration (secrets redacted).

```json
{}
```

**Returns:** Configuration JSON with all settings.

#### `xpst_auth_status`

Check authentication status for all platforms.

```json
{}
```

**Returns:** Per-platform auth status (authenticated/not authenticated + details).

#### `xpst_providers`

List all supported source and destination providers with capabilities.

```json
{}
```

**Returns:** Provider catalog with auth modes, capabilities, and roles.

#### `xpst_schedule_add`

Schedule a new post for future publishing.

```json
{
  "video_path": "/path/to/video.mp4",
  "caption": "Scheduled post",
  "platforms": ["youtube", "instagram", "threads", "linkedin"],
  "scheduled_time": "2026-06-20T14:00:00Z",
  "repeat_rule": "weekly"  // optional; daily | weekly | monthly
}
```

**Returns:** Schedule confirmation with ID.

---

### Knowledge Base (4 tools)

The knowledge base ingests your posted content into a vector store (LanceDB) for semantic search.

#### `kb_add`

Add a video's knowledge to the knowledge base.

```json
{
  "video_path": "/path/to/video.mp4",
  "caption": "How to configure xPST",
  "topics": ["tutorial", "config"]
}
```

**Returns:** Confirmation with extracted knowledge summary.

#### `kb_query`

Semantic search across your knowledge base.

```json
{
  "query": "How do I set up YouTube OAuth?",
  "limit": 5
}
```

**Returns:** Matching knowledge entries with relevance scores.

#### `kb_organize`

Organize knowledge entries into topic areas.

```json
{
  "action": "create",
  "area_name": "YouTube Setup",
  "entry_ids": ["entry-1", "entry-2"]
}
```

**Returns:** Updated organization structure.

#### `kb_areas`

List all knowledge areas and their entries.

```json
{}
```

**Returns:** Area catalog with entry counts and descriptions.

---

## Security & Guardrails

### Read-Only Mode

Set `XPST_MCP_READONLY=true` to restrict the MCP server to read-only tools only:

- ✅ `xpst_status`, `xpst_health`, `xpst_analytics`, `xpst_cross_post_analytics`, `xpst_followers`, `xpst_best_time`, `xpst_security_audit`, `xpst_suggest_caption`, `xpst_transcript`, `xpst_search`, `xpst_config_show`, `xpst_auth_status`, `xpst_providers`, `xpst_schedule_list`, `kb_query`, `kb_areas`
- ❌ `xpst_run`, `xpst_post`, `xpst_delete`, `xpst_backfill`, `xpst_schedule_add`, `kb_add`, `kb_organize`

### Confirmation Mode

Set `XPST_MCP_REQUIRE_CONFIRM=true` to require explicit user confirmation before executing any mutating tool. The server returns a confirmation prompt that the client must approve.

### Mutating Tools

The following tools are classified as **mutating** (can change state):

- `xpst_run` — Posts videos
- `xpst_post` — Posts videos
- `xpst_delete` — Deletes posts
- `xpst_backfill` — Retries failed posts
- `xpst_schedule_add` — Creates schedules
- `kb_add` — Adds to knowledge base
- `kb_organize` — Reorganizes knowledge base

### Credential Safety

- No credentials, tokens, or cookies are ever returned by any MCP tool
- `xpst_config_show` redacts all secrets before returning
- All credential files are stored encrypted at `~/.xpst/credentials/`

---

## Examples

### Example 1: "Post my latest local video to YouTube and X"

The AI agent would:
1. Call `xpst_run` with `source: "local"` to check for new local videos
2. Or call `xpst_post` with an explicit video path and `platforms: ["youtube", "x"]`

### Example 2: "Check if all my platforms are connected"

The AI agent would:
1. Call `xpst_health` to test connectivity
2. Call `xpst_auth_status` to check authentication

### Example 3: "What are my best performing posts?"

The AI agent would:
1. Call `xpst_analytics` with `days: 30`
2. Parse the results to identify top-performing platforms and metrics

### Example 4: "Schedule a post for tomorrow at 2pm"

The AI agent would:
1. Call `xpst_schedule_add` with the video path, caption, platforms, and ISO timestamp

### Example 5: "Search my knowledge base for Instagram tips"

The AI agent would:
1. Call `kb_query` with `query: "Instagram tips and best practices"`
2. Return matching entries from your content library

---

## Troubleshooting

### MCP server won't start

```bash
# Check the MCP extra is installed
pip show mcp

# Try starting manually
python -m xpst mcp

# Check for import errors
python -c "from xpst.mcp.server import cli_main; print('OK')"
```

### Claude Desktop can't find xPST

- Ensure the `command` path in `claude_desktop_config.json` points to a valid Python with xPST installed
- Use the full path to your virtualenv Python if running from source
- Check Claude Desktop logs for connection errors

### Tools not appearing

- Restart Claude Desktop after changing the config
- Ensure no other process is using the same stdio pipe
- Check that `XPST_MCP_READONLY` is not preventing tool access

### Knowledge base tools fail

- Ensure LanceDB is installed: `pip install lancedb`
- Check that `~/.xpst/knowledge/` is writable
- Run `xpst kb doctor` to diagnose issues

### Guardrail errors

- If you get "Tool requires confirmation" — set `XPST_MCP_REQUIRE_CONFIRM=false` or approve the confirmation
- If you get "Tool is read-only" — set `XPST_MCP_READONLY=false` to enable mutating tools

---

## Advanced: Programmatic Access

You can also interact with the MCP server programmatically:

```python
import asyncio
from xpst.mcp.server import handle_call_tool, list_tools

async def main():
    # List available tools
    tools = await list_tools()
    print(f"{len(tools.tools)} tools available")

    # Call a tool
    result = await handle_call_tool("xpst_status", {})
    print(result)

asyncio.run(main())
```

---

## See Also

- 📖 [CLI Tutorial](TUTORIAL_CLI.md) — All 28 CLI commands
- 🖥️ [Desktop App Tutorial](TUTORIAL_APP.md) — Full GUI walkthrough
- 🏠 [README](../README.md) — Project overview and installation
- 🐛 [Report Issues](https://github.com/TysAIs/xPST/issues)
