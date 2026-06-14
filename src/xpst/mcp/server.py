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
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

# The 'mcp' package is an optional extra. Import it gracefully so that simply
# importing this module (e.g. for build_provider_catalog) does not hard-fail
# when the extra is not installed. The clear install message is surfaced only
# when the server is actually invoked (see _require_mcp / get_server / main).
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        CallToolResult,
        ListToolsResult,
        TextContent,
        Tool,
    )
    _MCP_IMPORT_ERROR: ImportError | None = None
    HAS_MCP = True
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _MCP_IMPORT_ERROR = exc
    HAS_MCP = False

    class _MCPStub:
        """Lightweight stand-in so this module imports without the 'mcp' extra.

        Instances simply record their keyword arguments as attributes. Real MCP
        behavior (running the server) is gated behind ``_require_mcp`` and only
        attempted at invocation time, where a clear install message is raised.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            # Used for ``@app.list_tools()`` style decorators: return the
            # decorated function unchanged so module import succeeds.
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return self

        def __getattr__(self, name: str) -> Any:
            # Any attribute access (e.g. app.list_tools) returns a no-op factory
            # that yields a pass-through decorator, so module-level decorators
            # like ``@app.list_tools()`` succeed without the real mcp package.
            return _MCPStub()

        def create_initialization_options(self, *args: Any, **kwargs: Any) -> Any:
            return None

    Server = _MCPStub  # type: ignore[assignment,misc]
    stdio_server = None  # type: ignore[assignment]
    CallToolResult = _MCPStub  # type: ignore[assignment,misc]
    ListToolsResult = _MCPStub  # type: ignore[assignment,misc]
    TextContent = _MCPStub  # type: ignore[assignment,misc]
    Tool = _MCPStub  # type: ignore[assignment,misc]

from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.post_preflight import build_post_preflight
from xpst.utils.logger import get_logger, setup_logging

if TYPE_CHECKING:
    from xpst.sources.base import VideoMetadata

logger = get_logger(__name__)

def _provider_enums() -> tuple[list[str], list[str]]:
    """Platform/source enums for tool schemas, derived from the live provider
    catalog instead of hardcoded literals (G25) so plugin providers are
    reachable over MCP. Falls back to the built-ins if discovery fails."""
    platforms = ["youtube", "x", "instagram"]
    sources = ["tiktok", "youtube", "x", "instagram", "local"]
    try:
        from xpst.platforms.base import PlatformRegistry

        PlatformRegistry.auto_discover()
        discovered = [m.name for m in PlatformRegistry.list_manifests(None)]
        if discovered:
            platforms = sorted(set(platforms) | set(discovered))
    except Exception:  # noqa: BLE001 — schema fallback must never crash startup
        pass
    try:
        from xpst.sources.base import SourceRegistry

        SourceRegistry.auto_discover()
        discovered_sources = [m.name for m in SourceRegistry.list_manifests(None)]
        if discovered_sources:
            sources = sorted(set(sources) | set(discovered_sources))
    except Exception:  # noqa: BLE001
        pass
    return platforms, sources


_PLATFORM_ENUM, _SOURCE_ENUM = _provider_enums()


_MCP_INSTALL_HINT = "The MCP server requires the optional 'mcp' extra. Install it with: pip install 'xpst[mcp]'"


def _require_mcp() -> None:
    """Raise a clear, actionable error if the optional 'mcp' extra is missing."""
    if not HAS_MCP:
        raise ModuleNotFoundError(_MCP_INSTALL_HINT) from _MCP_IMPORT_ERROR


class XPSTMCPServer:
    """MCP Server for xPST cross-posting operations."""

    def __init__(self, config: XPSTConfig | None = None):
        """Initialize MCP server with optional config."""
        self.config = config or XPSTConfig()
        self.engine: CrossPostEngine | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the engine and components.

        The canonical :class:`~xpst.engine.CrossPostEngine` wires all of its
        sub-components synchronously in ``__init__`` (state, services, platforms,
        crash recovery), so there is no separate async init step. This method
        stays a coroutine to preserve the lazy-init contract used by the MCP
        tool dispatch (engine is built only when an engine-backed tool runs).
        """
        if self._initialized:
            return

        setup_logging(self.config.monitoring.log_level)
        self.engine = CrossPostEngine(self.config)
        self._initialized = True

    def get_engine(self) -> CrossPostEngine:
        if not self._initialized or self.engine is None:
            raise RuntimeError("Server not initialized. Call initialize() first.")
        return self.engine


# Global server instance
_server: XPSTMCPServer | None = None


async def get_server(
    config: XPSTConfig | None = None,
    *,
    initialize: bool = True,
) -> XPSTMCPServer:
    """Get or create the global server instance."""
    global _server
    if _server is None:
        _server = XPSTMCPServer(config)
    if initialize:
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
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
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
                    "enum": _SOURCE_ENUM,
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
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
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
                    "items": {"type": "string", "enum": _PLATFORM_ENUM},
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
        name="xpst_analytics",
        description=(
            "Per-post and per-platform engagement metrics (views, likes, "
            "comments, shares) with persisted snapshot history. live=false "
            "reads the local snapshot store only (fast, offline); live=true "
            "refreshes from the platform APIs first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "Limit to one platform",
                    "enum": _PLATFORM_ENUM + ["tiktok"],
                },
                "live": {
                    "type": "boolean",
                    "description": "Refresh from platform APIs before reading",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_schedule_list",
        description="List scheduled posts (pending, completed, failed) with times and targets",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_schedule_add",
        description=(
            "Schedule a post for later: local video file + caption + ISO-8601 "
            "time, optional platform list and repeat rule (daily/weekly/monthly)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "video_path": {"type": "string", "description": "Local video file path"},
                "caption": {"type": "string", "description": "Post caption"},
                "scheduled_time": {"type": "string", "description": "ISO-8601 local datetime, e.g. 2026-06-12T09:30:00"},
                "platforms": {"type": "array", "items": {"type": "string", "enum": _PLATFORM_ENUM}, "description": "Targets (default: all enabled)"},
                "repeat_rule": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "Optional repeat"},
            },
            "required": ["video_path", "caption", "scheduled_time"],
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
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
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
                    "items": {"type": "string", "enum": _PLATFORM_ENUM},
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
        name="xpst_providers",
        description="List supported content sources and posting destinations with capabilities",
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
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "video_id": {
                    "type": "string",
                    "description": "Video ID to delete",
                },
                "platform": {
                    "type": "string",
                    "description": "Platform to delete from (or all)",
                    "enum": [*_PLATFORM_ENUM, "all"],
                    "default": "all",
                },
            },
            "required": ["video_id"],
            "additionalProperties": False,
        },
    ),
    # ── Knowledge-base tools (optional 'knowledge' extra) ──
    # These mirror the `xpst kb ...` CLI. Their handlers lazy-import the heavy KB
    # subsystem (faster-whisper / fastembed / lancedb) only when invoked, so
    # listing them here keeps the cold import path light.
    Tool(
        name="kb_add",
        description="Ingest a local file or URL into the knowledge base",
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "source": {
                    "type": "string",
                    "description": "Local file path or URL to ingest",
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace name (isolated data dir)",
                    "default": "default",
                },
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="kb_query",
        description="Return stored knowledge nuggets whose text matches the query",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to match against stored nuggets",
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace name (isolated data dir)",
                    "default": "default",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of nuggets to return",
                    "minimum": 1,
                    "default": 8,
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="kb_organize",
        description="Discover areas, tag difficulty, and assign nuggets",
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "workspace": {
                    "type": "string",
                    "description": "Workspace name (isolated data dir)",
                    "default": "default",
                },
                "threshold": {
                    "type": "number",
                    "description": "Cosine similarity threshold for clustering/routing",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="kb_areas",
        description="List discovered knowledge areas in course order (beginner -> advanced)",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Workspace name (isolated data dir)",
                    "default": "default",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="kb_course",
        description="Assemble organized areas and cited nuggets into a course outline",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Workspace name (isolated data dir)",
                    "default": "default",
                },
                "area_id": {
                    "type": "string",
                    "description": "Optional area id to assemble; omit for the full course",
                },
            },
            "additionalProperties": False,
        },
    ),
]


# Tools that mutate state or post to REAL accounts (G52). XPST_MCP_READONLY=1
# blocks them entirely; XPST_MCP_REQUIRE_CONFIRM=1 requires confirm=true in
# the arguments — a consent tier for an otherwise unauthenticated local
# surface that any connected agent can drive.
_MUTATING_TOOLS = {
    "xpst_run", "xpst_post", "xpst_backfill", "xpst_delete",
    "xpst_schedule_add",
    "kb_add", "kb_organize",
}


def _guardrail_block(name: str, arguments: dict[str, Any]) -> CallToolResult | None:
    import os

    if name not in _MUTATING_TOOLS:
        return None
    if os.environ.get("XPST_MCP_READONLY", "").lower() in {"1", "true", "yes"}:
        return CallToolResult(
            isError=True,
            content=[TextContent(
                type="text",
                text=f"Blocked: {name} is disabled (XPST_MCP_READONLY is set). "
                     "Unset it to allow posting/mutating tools.",
            )],
        )
    if os.environ.get("XPST_MCP_REQUIRE_CONFIRM", "").lower() in {"1", "true", "yes"}:
        if not arguments.get("confirm"):
            return CallToolResult(
                isError=True,
                content=[TextContent(
                    type="text",
                    text=f"{name} posts to or mutates REAL accounts and "
                         "XPST_MCP_REQUIRE_CONFIRM is set. Re-call with "
                         '"confirm": true to proceed.',
                )],
            )
        arguments.pop("confirm", None)
    return None


async def handle_call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Handle a tool call."""
    blocked = _guardrail_block(name, arguments)
    if blocked is not None:
        return blocked

    engine_tools = {"xpst_run", "xpst_post", "xpst_health", "xpst_status", "xpst_backfill", "xpst_delete"}
    server = await get_server(initialize=name in engine_tools)

    try:
        if name == "xpst_run":
            engine = server.get_engine()
            return await _handle_run(engine, arguments)
        elif name == "xpst_post":
            engine = server.get_engine()
            return await _handle_post(engine, arguments)
        elif name == "xpst_health":
            engine = server.get_engine()
            return await _handle_health(engine)
        elif name == "xpst_status":
            engine = server.get_engine()
            return await _handle_status(engine)
        elif name == "xpst_backfill":
            engine = server.get_engine()
            return await _handle_backfill(engine, arguments)
        elif name == "xpst_analytics":
            return await _handle_analytics(server.config, arguments)
        elif name == "xpst_schedule_list":
            return await _handle_schedule_list(server.config)
        elif name == "xpst_schedule_add":
            return await _handle_schedule_add(server.config, arguments)
        elif name == "xpst_config_show":
            return await _handle_config_show(server.config)
        elif name == "xpst_auth_status":
            return await _handle_auth_status(server.config)
        elif name == "xpst_providers":
            return await _handle_providers(server.config)
        elif name == "xpst_delete":
            engine = server.get_engine()
            return await _handle_delete(engine, arguments)
        elif name in {"kb_add", "kb_query", "kb_organize", "kb_areas", "kb_course"}:
            return await _handle_kb_tool(name, arguments, server.config)
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
        actual_max = 20 if catch_up else max_posts
        videos = await engine.source_service.fetch_new_videos(source, actual_max)
        new_videos: list[VideoMetadata] = engine.source_service.filter_new(
            videos, engine.state, engine._platforms
        )
        targets = list(engine._platforms.keys())
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": True,
                    "fetch_count": len(new_videos),
                    "videos": [
                        {
                            "video_id": v.video_id,
                            "caption": v.caption[:100] if v.caption else "",
                            "source": v.source_platform,
                            "targets": targets,
                        }
                        for v in new_videos
                    ],
                }, indent=2, default=str),
            )],
        )

    results = await engine.check_and_post(
        catch_up=catch_up, source=source, max_posts=max_posts
    )
    # G28: agents need the per-video outcomes and post URLs, not a bare
    # success string.
    payload = {
        "ok": True,
        "processed": len(results),
        "results": [_serialize_result(r) for r in results],
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, default=str))],
    )


def _serialize_result(result: CrossPostResult) -> dict[str, Any]:
    """Convert a :class:`CrossPostResult` into a JSON-serializable dict.

    Mirrors the response shape previously emitted by the MCP server: the
    per-platform :class:`UploadResult` dataclasses are expanded into plain
    dicts so MCP clients see the full upload outcome.
    """
    return {
        "video_id": result.video_id,
        "caption": result.caption,
        "results": {p: asdict(r) for p, r in result.results.items()},
        "all_success": result.all_success,
        "partial_success": result.partial_success,
    }


async def _handle_post(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_post tool."""
    dry_run = args.get("dry_run", False)
    carousel_paths = args.get("carousel_paths", [])
    preflight = build_post_preflight(
        config=engine.config,
        video_path=args["video_path"],
        caption=args["caption"],
        platforms=args.get("platforms"),
        carousel_paths=carousel_paths,
    )

    if dry_run:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": True,
                    **preflight,
                }, indent=2, default=str),
            )],
        )

    if not preflight["ready"]:
        return CallToolResult(
            isError=True,
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "ok": False,
                    "error": preflight["blocking"][0] if preflight["blocking"] else "Post is not ready.",
                    "preflight": preflight,
                }, indent=2, default=str),
            )],
        )

    from pathlib import Path

    if carousel_paths:
        media_paths = [Path(args["video_path"]), *(Path(p) for p in carousel_paths)]
        result = await engine.post_manual_carousel(
            media_paths=media_paths,
            caption=args["caption"],
            platforms=args.get("platforms"),
        )
    else:
        result = await engine.post_manual(
            video_path=Path(args["video_path"]),
            caption=args["caption"],
            platforms=args.get("platforms"),
        )
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps(_serialize_result(result), indent=2, default=str),
        )],
    )


async def _handle_health(engine: CrossPostEngine) -> CallToolResult:
    """Handle xpst_health tool."""
    health = await engine.check_health()
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
                    "targets": platforms or list(engine._platforms.keys()),
                }, indent=2),
            )],
        )

    results = await engine.backfill(platforms=platforms, limit=max_count, source=source)
    successful = sum(1 for r in results if r.all_success)
    payload = {
        "attempted": len(results),
        "successful": successful,
        "results": [_serialize_result(r) for r in results],
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_schedule_list(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_schedule_list (G29)."""
    from xpst.schedule_manager import ScheduleManager

    manager = ScheduleManager(config.config_dir)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(
            {"schedules": manager.list()}, default=str,
        ))],
    )


async def _handle_schedule_add(config: XPSTConfig, arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_schedule_add (G29)."""
    from datetime import datetime
    from pathlib import Path

    from xpst.schedule_manager import ScheduleManager

    video_path = Path(arguments["video_path"]).expanduser()
    if not video_path.exists():
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=f"Video not found: {video_path}")],
        )
    try:
        when = datetime.fromisoformat(arguments["scheduled_time"])
    except ValueError as exc:
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=f"Bad scheduled_time (need ISO-8601): {exc}")],
        )
    manager = ScheduleManager(config.config_dir)
    entry = manager.add(
        video_path=str(video_path),
        caption=arguments["caption"],
        scheduled_time=when,
        platforms=arguments.get("platforms"),
        repeat_rule=arguments.get("repeat_rule"),
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"scheduled": entry}, default=str))],
    )


async def _handle_analytics(
    config: XPSTConfig | dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> CallToolResult:
    """Handle xpst_analytics (G27): expose the same numbers the UI shows.

    Reads the persisted snapshot store by default; live=true runs a real
    collection first (network, may take seconds and consume API quota).
    """
    from xpst.analytics import AnalyticsCollector

    if arguments is None:
        if isinstance(config, dict):
            arguments = config
            config = None
        else:
            arguments = {}
    if config is None:
        config = XPSTConfig()

    platform = arguments.get("platform")
    live = bool(arguments.get("live", False))

    collector = AnalyticsCollector(config.config_dir)
    if live:
        await collector.collect_all()

    store = collector.store
    latest = store.latest(platform)
    per_platform: dict[str, dict[str, Any]] = {}
    for row in latest:
        agg = per_platform.setdefault(row["platform"], {
            "posts": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0,
        })
        agg["posts"] += 1
        for key in ("views", "likes", "comments", "shares"):
            agg[key] += row.get(key) or 0

    payload = {
        "live": live,
        "snapshot_count": store.snapshot_count(),
        "platforms": per_platform,
        "posts": latest,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, default=str))],
    )


async def _handle_config_show(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_config_show tool."""
    # G26: reuse the CLI's recursive masker — the previous hand-rolled
    # version dumped monitoring.__dict__ unmasked, leaking the dashboard
    # password hash (and username) to any connected agent.
    from xpst.cli import _mask_sensitive_values

    def _section(obj: Any) -> dict[str, Any]:
        return dict(obj.__dict__) if hasattr(obj, "__dict__") else {}

    masked = _mask_sensitive_values({
        "accounts": {
            platform: _section(getattr(config, platform))
            for platform in ("tiktok", "youtube", "x", "instagram", "local")
            if hasattr(config, platform)
        },
        "video": _section(config.video),
        "monitoring": _section(config.monitoring),
        "schedule": _section(config.schedule),
    })

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

    result: dict[str, Any] = {
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


async def _handle_providers(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_providers tool."""
    data = build_provider_catalog(config)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, indent=2, default=str))],
    )


def build_provider_catalog(config: XPSTConfig) -> dict[str, Any]:
    """Return provider metadata for MCP clients and support tooling."""
    from xpst.platforms.base import PlatformRegistry
    from xpst.sources.base import SourceRegistry

    SourceRegistry.auto_discover()
    PlatformRegistry.auto_discover()
    sources = SourceRegistry.list_manifests(config)
    destinations = PlatformRegistry.list_manifests(config)

    return {
        "sources": [
            manifest.to_dict()
            for manifest in sorted(sources, key=lambda item: item.name)
        ],
        "destinations": [
            manifest.to_dict()
            for manifest in sorted(destinations, key=lambda item: item.name)
        ],
    }


async def _handle_delete(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_delete tool.

    Removes a post *record* from local state. ``platform="all"`` removes the
    record for every platform the video was posted to. This is a state-only
    operation and does not call the social platform's delete API (use the CLI
    ``delete`` command for live deletion).
    """
    video_id = args["video_id"]
    platform = args.get("platform", "all")

    video = engine.state.get_video(video_id)
    platforms = (
        list((video or {}).get("posted_to", {}).keys())
        if platform == "all"
        else [platform]
    )

    for plat in platforms:
        engine.state.remove_post(video_id, plat)
    engine.state.save()

    result = {
        "video_id": video_id,
        "platform": platform,
        "removed": platforms,
        "success": True,
    }

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
    )


async def _handle_kb_tool(name: str, args: dict[str, Any], config: XPSTConfig) -> CallToolResult:
    """Dispatch a knowledge-base tool call.

    The heavy KB subsystem (faster-whisper / fastembed / lancedb) is imported
    lazily here, never at module load, preserving the cold-path import wall. The
    underlying handlers are synchronous and can block (transcription, embedding,
    clustering), so they run in a worker thread to keep the event loop free. A
    missing ``xpst[knowledge]`` extra surfaces as a clear, actionable error.
    """
    try:
        from xpst.knowledge.mcp import tools as kb_tools
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=(
                    "Knowledge features need the extra: "
                    f"pip install 'xpst[knowledge]' ({exc})"
                ),
            )],
            isError=True,
        )

    def run_active_profile_kb_tool() -> dict[str, Any]:
        workspace = args.get("workspace", "default")
        home = str(config.config_dir)
        if name == "kb_add":
            return kb_tools.kb_add(args["source"], workspace, home=home)
        if name == "kb_query":
            return kb_tools.kb_query(args["text"], workspace, args.get("limit", 8), home=home)
        if name == "kb_organize":
            return kb_tools.kb_organize(workspace, args.get("threshold"), home=home)
        if name == "kb_areas":
            return kb_tools.kb_areas(workspace, home=home)
        return kb_tools.kb_course(workspace, args.get("area_id"), home=home)

    payload = await asyncio.to_thread(run_active_profile_kb_tool)

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
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
    """Run the MCP server over stdio.

    Raises:
        ModuleNotFoundError: with an install hint if the 'mcp' extra is missing.
    """
    _require_mcp()
    await get_server(config, initialize=False)

    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def cli_main() -> None:
    """CLI entry point for xpst mcp command."""
    config_dir = os.environ.get("XPST_CONFIG_DIR")
    config_path = str(Path(config_dir).expanduser() / "config.yaml") if config_dir else None
    config = XPSTConfig.load(config_path)
    asyncio.run(main(config))


if __name__ == "__main__":
    cli_main()
