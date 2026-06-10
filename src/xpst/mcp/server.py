"""MCP (Model Context Protocol) Server for xPST.

Provides stdio-based MCP server with tools for:
- Video fetching and cross-posting
- Health checks and status
- Configuration management
- Platform authentication
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from xpst.config import XPSTConfig
from xpst.engine_v2 import CrossPostEngine
from xpst.usecases import UseCaseFactory
from xpst.state import StateManager
from xpst.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


class XPSTMCPServer:
    """MCP Server for xPST cross-posting operations."""

    def __init__(self, config: XPSTConfig | None = None):
        """Initialize MCP server with optional config."""
        self.config = config or XPSTConfig()
        self.engine: CrossPostEngine | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the engine and components."""
        if self._initialized:
            return

        setup_logging(self.config.monitoring.log_level)
        self.engine = CrossPostEngine(self.config)
        await self.engine.initialize()
        self._initialized = True

    def get_engine(self) -> CrossPostEngine:
        if not self._initialized or self.engine is None:
            raise RuntimeError("Server not initialized. Call initialize() first.")
        return self.engine


# Global server instance
_server: XPSTMCPServer | None = None


async def get_server(config: XPSTConfig | None = None) -> XPSTMCPServer:
    """Get or create the global server instance."""
    global _server
    if _server is None:
        _server = XPSTMCPServer(config)
        await _server.initialize()
    return _server


# ── Tool Definitions ──

TOOLS: list[Tool] = [
    Tool(
        name="xpst_run",
        description="Check for new videos and cross-post them to configured platforms",
        inputSchema={
            "type": "object",
            "properties": {
                "max_posts": {
                    "type": "integer",
                    "description": "Maximum number of posts per cycle",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 50,
                },
                "source": {
                    "type": "string",
                    "description": "Source to fetch from",
                    "default": "tiktok",
                    "enum": ["tiktok", "youtube", "x", "instagram", "local"],
                },
                "catch_up": {
                    "type": "boolean",
                    "description": "Fetch extra videos for catch-up mode",
                    "default": False,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Show what would happen without uploading",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_post",
        description="Manually post a local video file or carousel to platforms",
        inputSchema={
            "type": "object",
            "properties": {
                "video_path": {
                    "type": "string",
                    "description": "Path to video file (or first image for carousel)",
                },
                "caption": {
                    "type": "string",
                    "description": "Caption for the post",
                },
                "platforms": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["youtube", "x", "instagram"]},
                    "description": "Target platforms (default: all configured)",
                },
                "carousel_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional image/video paths for carousel",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Show what would happen without uploading",
                    "default": False,
                },
            },
            "required": ["video_path", "caption"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_health",
        description="Test connectivity to all platforms and sources (no uploads)",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_status",
        description="Show cross-posting statistics and health status",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_backfill",
        description="Retry failed or incomplete posts from history",
        inputSchema={
            "type": "object",
            "properties": {
                "max_count": {
                    "type": "integer",
                    "description": "Maximum videos to backfill",
                    "default": 10,
                },
                "source": {
                    "type": "string",
                    "description": "Source to fetch from",
                    "default": "tiktok",
                },
                "platforms": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["youtube", "x", "instagram"]},
                    "description": "Target platforms",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Show what would be backfilled",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_config_show",
        description="Display current configuration (with sensitive values masked)",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_auth_status",
        description="Show authentication status for all platforms",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_delete",
        description="Delete a post record from state",
        inputSchema={
            "type": "object",
            "properties": {
                "video_id": {
                    "type": "string",
                    "description": "Video ID to delete",
                },
                "platform": {
                    "type": "string",
                    "description": "Platform to delete from (or all)",
                    "enum": ["youtube", "x", "instagram", "all"],
                    "default": "all",
                },
            },
            "required": ["video_id"],
            "additionalProperties": False,
        },
    ),
]


async def handle_call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Handle a tool call."""
    server = await get_server()
    engine = server.get_engine()

    try:
        if name == "xpst_run":
            return await _handle_run(engine, arguments)
        elif name == "xpst_post":
            return await _handle_post(engine, arguments)
        elif name == "xpst_health":
            return await _handle_health(engine)
        elif name == "xpst_status":
            return await _handle_status(engine)
        elif name == "xpst_backfill":
            return await _handle_backfill(engine, arguments)
        elif name == "xpst_config_show":
            return await _handle_config_show(server.config)
        elif name == "xpst_auth_status":
            return await _handle_auth_status(server.config)
        elif name == "xpst_delete":
            return await _handle_delete(engine, arguments)
        else:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
    except Exception as e:
        logger.exception(f"Error in tool {name}: {e}")
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {str(e)}")],
            isError=True,
        )


async def _handle_run(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_run tool."""
    dry_run = args.get("dry_run", False)
    max_posts = args.get("max_posts", 5)
    source = args.get("source", "tiktok")
    catch_up = args.get("catch_up", False)

    if dry_run:
        fetch_uc = engine.usecase_factory.create_fetch_videos()
        result = await fetch_uc.execute(
            source_name=source,
            max_count=max_posts,
            catch_up=catch_up,
        )
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": True,
                    "fetch_count": result.fetch_count,
                    "videos": [
                        {
                            "video_id": v.video_id,
                            "caption": v.caption[:100] if v.caption else "",
                            "source": v.source_platform,
                            "targets": list(engine.platforms.keys()),
                        }
                        for v in result.videos
                    ],
                }, indent=2, default=str),
            )],
        )

    await engine.run(max_posts=max_posts, source=source, catch_up=catch_up)
    return CallToolResult(
        content=[TextContent(type="text", text="Cross-post cycle completed successfully")],
    )


async def _handle_post(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_post tool."""
    dry_run = args.get("dry_run", False)

    if dry_run:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": True,
                    "video": args["video_path"],
                    "caption": args["caption"][:100],
                    "carousel": len(args.get("carousel_paths", [])) > 0,
                    "targets": args.get("platforms") or list(engine.platforms.keys()),
                }, indent=2),
            )],
        )

    result = await engine.manual_post(
        video_path=args["video_path"],
        caption=args["caption"],
        platforms=args.get("platforms"),
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
    )


async def _handle_health(engine: CrossPostEngine) -> CallToolResult:
    """Handle xpst_health tool."""
    health = await engine.health_check()
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(health, indent=2, default=str))],
    )


async def _handle_status(engine: CrossPostEngine) -> CallToolResult:
    """Handle xpst_status tool."""
    stats = engine.state.get_statistics()
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(stats, indent=2, default=str))],
    )


async def _handle_backfill(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_backfill tool."""
    dry_run = args.get("dry_run", False)
    max_count = args.get("max_count", 10)
    source = args.get("source", "tiktok")
    platforms = args.get("platforms")

    if dry_run:
        # Just show what would be backfilled
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": True,
                    "source": source,
                    "max_count": max_count,
                    "targets": platforms or list(engine.platforms.keys()),
                }, indent=2),
            )],
        )

    result = await engine.backfill(source=source, max_count=max_count, platforms=platforms)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
    )


async def _handle_config_show(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_config_show tool."""
    # Return config with sensitive fields masked
    masked = {
        "accounts": {},
        "video": config.video.__dict__ if hasattr(config.video, '__dict__') else {},
        "monitoring": config.monitoring.__dict__ if hasattr(config.monitoring, '__dict__') else {},
        "schedule": config.schedule.__dict__ if hasattr(config.schedule, '__dict__') else {},
    }
    for platform, acc in config.accounts.__dict__.items():
        if platform == "tiktok":
            masked["accounts"][platform] = {**acc.__dict__}
        else:
            masked["accounts"][platform] = {**acc.__dict__}
            # Mask sensitive fields
            for key in ["client_secrets", "token_file", "cookies_file", "session_file", "password"]:
                if key in masked["accounts"][platform] and masked["accounts"][platform][key]:
                    masked["accounts"][platform][key] = "***MASKED***"

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(masked, indent=2, default=str))],
    )


async def _handle_auth_status(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_auth_status tool."""
    from xpst.utils.credentials import CredentialStore
    from xpst.utils.quota import QuotaManager

    cred_store = CredentialStore(config.config_dir)
    quota_mgr = QuotaManager(config.config_dir)

    stored_keys = cred_store.list_keys()
    storage_type = "OS Keychain" if cred_store._use_keyring else "File Storage (encrypted fallback)"

    result = {
        "credential_storage": storage_type,
        "stored_credentials": stored_keys,
        "platforms": {},
    }

    for platform in ["youtube", "x", "instagram"]:
        creds = None
        if platform == "youtube":
            creds = cred_store.retrieve("youtube_token")
        elif platform == "x":
            creds = cred_store.retrieve_json("x_cookies")
        elif platform == "instagram":
            creds = cred_store.retrieve_json("instagram_session")

        remaining = quota_mgr.get_remaining(platform)
        result["platforms"][platform] = {
            "authenticated": bool(creds),
            "quota_remaining": remaining.get("daily", "N/A"),
        }

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
    )


async def _handle_delete(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_delete tool."""
    video_id = args["video_id"]
    platform = args.get("platform", "all")

    result = await engine.usecase_factory.create_delete_post().execute(
        video_id=video_id,
        platform=platform if platform != "all" else None,
        delete_from_platform=False,
    )

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
    )


# ── MCP Server Setup ──

app = Server("xpst-mcp")


@app.list_tools()
async def list_tools() -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    return await handle_call_tool(name, arguments)


async def main(config: XPSTConfig | None = None) -> None:
    """Run the MCP server over stdio."""
    server = await get_server(config)

    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def cli_main() -> None:
    """CLI entry point for xpst mcp command."""
    config = XPSTConfig()
    asyncio.run(main(config))


if __name__ == "__main__":
    cli_main()