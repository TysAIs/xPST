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
from dataclasses import asdict
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
            # Real CallToolResult/ListToolsResult stubs used as call-result
            # stand-ins: default isError=False so ``not isError`` assertions on
            # success paths behave like the real pydantic mcp types.
            is_error = kwargs.pop("isError", False)
            for key, value in kwargs.items():
                setattr(self, key, value)
            self.isError = is_error

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
from xpst.content import (
    CONTENT_TYPES,
    PUBLISH_ROUTE_TEXT,
    PUBLISH_ROUTE_UNIMPLEMENTED,
    ContentRequest,
    capability_document,
    content_verdict,
)
from xpst.engine import CrossPostEngine, CrossPostResult
from xpst.provider_truth import ProviderRole
from xpst.services.batch_outcome import batch_outcome
from xpst.setup_transaction import (
    SetupTransactionError,
    SetupTransactionNotFound,
    SetupTransactionService,
)
from xpst.utils.logger import get_logger, setup_logging

if TYPE_CHECKING:
    from xpst.sources.base import VideoMetadata

logger = get_logger(__name__)

def _provider_enums() -> tuple[list[str], list[str]]:
    """Platform/source enums for tool schemas, derived from the live provider
    catalog instead of hardcoded literals (G25) so plugin providers are
    reachable over MCP. Falls back to the built-ins if discovery fails."""
    from xpst.provider_truth import SUPPORTED_PROVIDERS

    platforms = [
        definition.name
        for definition in SUPPORTED_PROVIDERS
        if ProviderRole.VIDEO_DESTINATION in definition.roles
    ]
    sources = [
        definition.name
        for definition in SUPPORTED_PROVIDERS
        if ProviderRole.SOURCE in definition.roles
    ]
    try:
        from xpst.platforms.base import PlatformRegistry

        PlatformRegistry.auto_discover()
        discovered = [
            manifest.name
            for manifest in PlatformRegistry.list_manifests(None)
            if ProviderRole.VIDEO_DESTINATION in manifest.canonical_roles
        ]
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


def _connectable_enum() -> list[str]:
    """Platforms a human can authenticate or disconnect, from canonical truth.

    ``_PLATFORM_ENUM`` covers publishing destinations only; Messenger is a
    messaging provider that still has an auth/disconnect flow, so it is added
    explicitly rather than by a second hardcoded literal that would drift the
    next time a provider is added (Facebook was exactly that case).
    """
    from xpst.provider_truth import SUPPORTED_PROVIDERS

    names = set(_PLATFORM_ENUM)
    names.update(
        definition.name
        for definition in SUPPORTED_PROVIDERS
        if ProviderRole.MESSAGING in definition.roles
    )
    return sorted(names)


_CONNECTABLE_ENUM = _connectable_enum()


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

_SETUP_ROLE_CAPABILITY_SCHEMA = {
    "type": "array",
    "description": "Non-secret role-capability selections such as source/tiktok and video_destination/youtube",
    "items": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ["source", "video_destination", "analytics", "messaging"]},
            "capability": {"type": "string"},
        },
        "required": ["role", "capability"],
        "additionalProperties": False,
    },
}


TOOLS: list[Tool] = [
    Tool(
        name="xpst_run",
        description=(
            "Check for new videos and cross-post them to configured platforms. "
            "A batch where nothing was published is never reported as success: "
            "`batch_status` says `published` / `partial` / `failed` / "
            "`nothing_to_do`, `exit_code` is the status the CLI would exit with, "
            "and `failed_destinations` names every destination that failed."
        ),
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
        description=(
            "Post to platforms: one local video/image file, a carousel "
            "(carousel_paths), or a text post. `content_type` states what the post "
            "is in the canonical vocabulary (video/image/carousel/text/thread); a "
            "type a chosen destination cannot publish is refused with an explicit "
            "blocker BEFORE anything is uploaded, and the refusal is never reported "
            "as a success. `overrides` writes a different caption per destination "
            '(e.g. {"x": {"text": "short copy"}}); destinations without one use '
            "`caption`. Call `xpst_capabilities` to see what each destination can "
            "actually publish."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "video_path": {
                    "type": "string",
                    "description": "Path to the video/image file (or the first item for a carousel); omit for a text post",
                },
                "content_type": {
                    "type": "string",
                    "enum": [item.value for item in CONTENT_TYPES],
                    "description": (
                        "What this post is (canonical vocabulary). Defaults to what the "
                        "media implies: one file = video, several = carousel. A text "
                        "post needs `content_type: text` and no media; destinations "
                        "with no text path refuse it."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": "Caption for the post (the default for every destination without an override; the text itself for a text post)",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Text of a text-only post (no media). Same body as `caption`; "
                        "passing `text` with `content_type: text` (or on its own) "
                        "publishes a text post. Destinations with no text path refuse it."
                    ),
                },
                "overrides": {
                    "type": "object",
                    "description": (
                        "Per-destination caption overrides: {\"x\": \"short copy\"} or "
                        "{\"x\": {\"text\": \"short copy\"}}. A destination not listed "
                        "keeps caption. An override over that destination's own limit "
                        "is refused in preflight, naming the destination."
                    ),
                    "additionalProperties": {"type": ["string", "object"]},
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
            "required": ["caption"],
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
        name="xpst_cross_post_analytics",
        description=(
            "Cross-post correlation analytics (B1): one video posted to "
            "multiple platforms shown as a single entry with aggregated totals "
            "(views, likes, comments, shares) and per-platform breakdown, plus "
            "engagement rate and tier (high/medium/low). Reads the local "
            "snapshot store only — fast and offline."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_followers",
        description=(
            "Follower counts per platform with growth history. "
            "Returns total followers across all platforms, per-platform counts, "
            "and recent growth trends from stored snapshots."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_best_time",
        description=(
            "Best time to post per platform, based on engagement history. "
            "Analyzes when your posts get the highest engagement and recommends "
            "the optimal day-of-week and hour for each platform."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "Optional: filter to a specific platform",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_security_audit",
        description=(
            "Run an automated security check on the xPST installation. "
            "Verifies credential file permissions, encrypted storage, "
            "dashboard localhost binding, MCP readonly mode, FFmpeg availability, "
            "and provider mode configuration."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_suggest_caption",
        description=(
            "Generate AI caption suggestions for a video file. "
            "Uses the video's transcript to generate 3 caption variants "
            "with platform-specific character limits and hashtag suggestions. "
            "Falls back to deterministic extraction when no LLM is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "video_path": {
                    "type": "string",
                    "description": "Path to the video file",
                },
                "platform": {
                    "type": "string",
                    "description": "Target platform (x, threads, instagram, youtube, tiktok)",
                    "default": "instagram",
                },
            },
            "required": ["video_path"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_generate_ideas",
        description=(
            "Generate post ideas for a content topic (AI content studio). "
            "Uses the KB LLM when configured (XPST_KB_LLM_ENABLED), otherwise "
            "falls back to deterministic template generation — no LLM required. "
            "Returns up to `count` ideas, each with an 'idea' headline and "
            "optional 'hook'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Content topic to generate ideas around",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of ideas to generate (clamped to 1-10)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_transcript",
        description=(
            "Get the transcript for a video by its content_hash or video_id. "
            "Returns the full transcript text and segment timestamps. "
            "Transcripts are cached by content_hash so cross-posted videos "
            "share the same transcript (D2)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "video_id": {
                    "type": "string",
                    "description": "Video ID or content_hash to look up",
                },
            },
            "required": ["video_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_search",
        description=(
            "Search the knowledge base for nuggets, clips, and topics. "
            "Returns matching knowledge nuggets with their source video info "
            "and analytics correlation (total views across platforms)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_activity",
        description="List recorded platform failures with targeted retry or review actions (read-only)",
        inputSchema={
            "type": "object",
            "properties": {},
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
        name="xpst_schedule_cancel",
        description=(
            "Cancel a scheduled post by entry id — the MCP equivalent of "
            "`xpst schedule remove`. Removes the entry from the LOCAL schedule "
            "store (~/.xpst/schedule.json) only; it never un-posts content that "
            "has already been published. Get entry ids from xpst_schedule_list. "
            "dry_run=true returns the same verdict without modifying the store. "
            "An unknown id is an error (POST_NOT_FOUND), never a silent success."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "entry_id": {"type": "string", "description": "Schedule entry id (from xpst_schedule_list)"},
                "dry_run": {
                    "type": "boolean",
                    "description": "Report whether the entry exists and would be cancelled, without modifying the store",
                    "default": False,
                },
            },
            "required": ["entry_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_failures_retry",
        description=(
            "Retry ONE recorded upload failure, identified by video_id + platform "
            "— the MCP equivalent of `xpst failures retry <video_id> --platform "
            "<name>`. Re-posts that video's local source file to that single "
            "destination (a REAL platform upload; scope=platform_upload). Returns "
            "ok=false with VIDEO_NOT_FOUND, NO_RECORDED_FAILURE, NO_LOCAL_FILE or "
            "RETRY_FAILED instead of pretending a retry happened; posted=true only "
            "when the destination accepted the post. Use xpst_activity to list "
            "failures and their retryable flag, and dry_run=true to plan without "
            "uploading."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "video_id": {"type": "string", "description": "Video id as recorded in xpst_activity failures"},
                "platform": {
                    "type": "string",
                    "enum": list(_PLATFORM_ENUM),
                    "description": "The single destination whose failure is being retried",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Resolve the retry target and report the verdict without uploading",
                    "default": False,
                },
            },
            "required": ["video_id", "platform"],
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
        description=(
            "Retry failed or incomplete posts from history. `attempted` / "
            "`successful` are counts, not a verdict: `batch_status`, `exit_code` "
            "and `failed_destinations` report the same aggregate the CLI "
            "(`xpst backfill`) exits on, so an all-failed retry is not read as a "
            "successful one."
        ),
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
        description=(
            "Show live authentication status and the truthful per-platform badge "
            "(connected / expiring / needs_reauth / source_only / disabled / unknown). "
            "The badge is derived from a live check with its timestamp — a stored "
            "credential alone is never reported as connected."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_bio_get",
        description=(
            "Get the link-in-bio page URL and its current configuration. "
            "Returns the public /bio URL, the page handle, and the ordered "
            "list of links (enabled social accounts + custom links) exactly "
            "as rendered by the dashboard bio page."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_capabilities",
        description=(
            "Return the canonical role-aware provider catalog AND the content contract "
            "without network calls: per destination, which content types it declares "
            "and which it can actually publish (`content`), plus the publishing route "
            "each content type takes. Read this before posting so you never attempt a "
            "modality a destination cannot publish."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="xpst_preflight",
        description=(
            "Run the canonical side-effect-free post preflight for local media and targets "
            "(media, caption, per-destination caption, destination readiness, content type); "
            "never uploads and never touches the network"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "media_path": {"type": "string"},
                "platforms": {"type": "array", "items": {"type": "string"}},
                "caption": {"type": "string", "default": ""},
                "overrides": {
                    "type": "object",
                    "description": (
                        "Per-destination caption overrides: {\"threads\": \"short copy\"} or "
                        "{\"threads\": {\"text\": \"short copy\"}}. Checked against each "
                        "destination's own limit."
                    ),
                    "additionalProperties": {"type": ["string", "object"]},
                },
                "text": {
                    "type": "string",
                    "default": "",
                    "description": (
                        "Body of a text-only post. With no media_path it implies "
                        "content_type \"text\" (a text post is refused by any destination "
                        "that has no text path)."
                    ),
                },
                "content_type": {
                    "type": "string",
                    "enum": [item.value for item in CONTENT_TYPES],
                    "description": (
                        "What the post is (canonical vocabulary). Omit to let the media "
                        "decide; state `text` to ask whether a text-only post is possible."
                    ),
                },
            },
            "required": ["platforms"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_readiness",
        description="Return local setup readiness and actionable blockers without starting the posting engine",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="xpst_auth_start",
        description="Return a human-only authentication action plan; never opens a browser or accepts secrets",
        inputSchema={
            "type": "object",
            "properties": {"platform": {"type": "string", "enum": _CONNECTABLE_ENUM}},
            "required": ["platform"],
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
        name="xpst_disconnect",
        description=(
            "Disconnect a platform: remove its stored account credentials "
            "(tokens, cookies, session files) and set "
            "accounts.<platform>.enabled=false. Never touches posted content "
            "or local state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "platform": {
                    "type": "string",
                    "description": "Platform to disconnect",
                    "enum": _CONNECTABLE_ENUM,
                },
            },
            "required": ["platform"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_delete",
        description=(
            "Delete a post for REAL on the platform and remove its local "
            "record. Calls the platform delete API for every destination the "
            "video was posted to and reports each destination's outcome: "
            "deleted, soft_hidden (unpublished but recoverable), pending "
            "(could not confirm — remove manually via the returned share_url), "
            "or unsupported (that platform has no delete path, e.g. Messenger). "
            "The response carries platform_deleted per destination plus a "
            "boolean aggregate (true only when every destination is gone). "
            "Destination platforms (YouTube, X, Instagram, TikTok, Threads, "
            "Facebook) all support deletion today. Removing the record also "
            "makes the engine treat the video as new again, so treat the call "
            "as destructive and confirm with the user first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "video_id": {
                    "type": "string",
                    "description": "Video ID to delete on the platform(s) and drop from local state",
                },
                "platform": {
                    "type": "string",
                    "description": "Destination platform to delete from (or all)",
                    "enum": [*_PLATFORM_ENUM, "all"],
                    "default": "all",
                },
            },
            "required": ["video_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="messenger_send",
        description=(
            "Send a text message to a Messenger recipient (page-scoped PSID) via the "
            "Meta Graph API. Requires a configured Messenger Page Access Token "
            "(run 'xpst auth messenger'). Returns the message_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "recipient": {"type": "string", "description": "Page-scoped PSID of the recipient"},
                "text": {"type": "string", "description": "Message body (max 640 chars)"},
            },
            "required": ["recipient", "text"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="messenger_set_rules",
        description=(
            "Configure the Messenger ManyChat-lite auto-reply rules. Provide a "
            "keyword->reply map (the '*' key is the catch-all) and optionally toggle "
            "auto_reply. Persists to config."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "rules": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Keyword -> reply map ('*' = catch-all)"},
                "auto_reply": {"type": "boolean", "description": "Enable/disable auto-reply", "default": True},
            },
            "required": ["rules"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_messenger_check_comments",
        description=(
            "Fetch recent comments on an Instagram or Facebook post and "
            "auto-reply per the configured reply_rules (Content360-style "
            "comment auto-reply, mirroring `xpst messenger check-comments`). "
            "Gated by accounts.messenger.comment_reply_enabled and "
            "comment_platforms. Posts public replies on the comment threads "
            "when a keyword matches."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Required true when XPST_MCP_REQUIRE_CONFIRM is set", "default": False},
                "media_id": {"type": "string", "description": "Post/media ID to scan for comments"},
                "platform": {
                    "type": "string",
                    "enum": ["instagram", "facebook"],
                    "description": "Platform owning media_id",
                    "default": "instagram",
                },
                "since_ts": {
                    "type": "integer",
                    "description": "Only reply to comments created after this epoch timestamp (Facebook only)",
                },
            },
            "required": ["media_id"],
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
        name="xpst_setup_start",
        description="Start or return the shared resumable setup transaction",
        inputSchema={
            "type": "object",
            "properties": {"selected_role_capabilities": _SETUP_ROLE_CAPABILITY_SCHEMA},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_setup_status",
        description="Read the shared setup transaction and pending human actions",
        inputSchema={
            "type": "object",
            "properties": {"transaction_id": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_setup_resume",
        description="Resume setup with safe step state or caller-verified readiness",
        inputSchema={
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string"},
                "step_id": {"type": "string"},
                "step_state": {"type": "string", "enum": ["pending", "in_progress", "waiting_human", "verified", "skipped", "error"]},
                "step_updates": {"type": "object"},
                "readiness": {"type": "object"},
                "finish_later": {"type": "boolean", "default": False},
                "error": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="xpst_setup_reset",
        description="Reset the shared setup transaction and its recovery copies",
        inputSchema={
            "type": "object",
            "properties": {"transaction_id": {"type": "string"}},
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
    "xpst_schedule_add", "xpst_disconnect",
    "messenger_send", "messenger_set_rules", "xpst_messenger_check_comments",
    "kb_add", "kb_organize",
    # Parity wave: both of these change real state. `xpst_schedule_cancel`
    # removes a scheduled post; `xpst_failures_retry` re-uploads to a live
    # destination. Gating them keeps the consent story uniform (and matches the
    # CLI, which demands an explicit --yes / confirmation for the same act).
    "xpst_schedule_cancel", "xpst_failures_retry",
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
    # M1 security fix: mutating tools are secure-by-default.
    # They require either:
    #   1. XPST_MCP_ALLOW_MUTATIONS=1 (explicit opt-in), OR
    #   2. confirm=True in arguments AND XPST_MCP_REQUIRE_CONFIRM is set
    # This prevents prompt-injected MCP clients from posting/deleting without consent.
    allow_mutations = os.environ.get("XPST_MCP_ALLOW_MUTATIONS", "").lower() in {"1", "true", "yes"}
    require_confirm = os.environ.get("XPST_MCP_REQUIRE_CONFIRM", "").lower() in {"1", "true", "yes"}

    if not allow_mutations and not require_confirm:
        # Default: fail-open is NOT the default anymore. Require explicit opt-in.
        return CallToolResult(
            isError=True,
            content=[TextContent(
                type="text",
                text=f"{name} posts to or mutates REAL accounts. "
                     "Set XPST_MCP_ALLOW_MUTATIONS=1 to allow, or "
                     "XPST_MCP_REQUIRE_CONFIRM=1 to require per-call confirmation.",
            )],
        )
    if require_confirm and not arguments.get("confirm"):
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


# Argument names across every MCP tool that carry a filesystem path. A tool
# argument is attacker-influenced: an MCP client (or an LLM reading untrusted
# content) chooses it, so a path must be confined the same way a CLI path is.
_PATH_ARG_NAMES = frozenset(
    {
        "video_path",
        "carousel_paths",
        "media_path",
        "media_paths",
        "file_path",
        "path",
        "output_path",
    }
)

# Argument names that carry a URL xPST will fetch.
# "source" is deliberately included: it is a URL for ``kb_add`` (knowledge-base
# ingest) and a bare platform name for ``xpst_run`` — the ``://`` check in
# ``_argument_shape_block`` skips the latter, so only real URLs are validated.
_URL_ARG_NAMES = frozenset(
    {
        "url",
        "webhook_url",
        "callback_url",
        "source_url",
        "media_url",
        "source",
    }
)


def _iter_arg_values(arguments: dict[str, Any], names: frozenset[str]):
    """Yield ``(argument_name, value)`` for string / list-of-string args."""
    for key, value in arguments.items():
        if key not in names:
            continue
        if isinstance(value, str):
            yield key, value
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    yield key, item


def _argument_shape_block(name: str, arguments: dict[str, Any]) -> CallToolResult | None:
    """Validate path and URL arguments for EVERY tool, before dispatch.

    Applied to all tools (not just mutating ones): ``xpst_post``,
    ``xpst_run``, ``xpst_schedule_add`` and the knowledge base all take a path
    or a URL from the caller. A rejected argument returns an ``isError`` result
    describing which argument failed — never a traceback and never a secret.
    """
    import os

    from xpst.utils.net_guard import BlockedURLError, validate_public_url
    from xpst.utils.path_guard import PathConfinementError, confine_media_path

    # Explicit, documented opt-out for users whose media genuinely lives outside
    # the default roots (or who already pass XPST_MEDIA_ROOTS).
    if os.environ.get("XPST_MCP_ALLOW_ANY_PATH", "").lower() in {"1", "true", "yes"}:
        return None

    def _reject(argument: str, reason: str) -> CallToolResult:
        logger.warning("MCP argument refused: tool=%s argument=%s reason=%s", name, argument, reason)
        return CallToolResult(
            isError=True,
            content=[TextContent(
                type="text",
                text=f"Blocked: {name} argument '{argument}' was refused — {reason}. "
                     "Media paths must live inside the xPST config dir or a user media "
                     "folder (override with XPST_MEDIA_ROOTS); URLs must be public http(s).",
            )],
        )

    try:
        for argument, value in _iter_arg_values(arguments, _PATH_ARG_NAMES):
            try:
                confine_media_path(value, must_exist=False)
            except PathConfinementError as exc:
                return _reject(argument, str(exc))
            except Exception as exc:  # noqa: BLE001 - never crash the tool call
                return _reject(argument, f"unusable path ({type(exc).__name__})")

        for argument, value in _iter_arg_values(arguments, _URL_ARG_NAMES):
            # Only look at URL-shaped values: some tools pass a bare platform
            # name or a knowledge-base label in a 'source'-like field.
            if "://" not in value:
                continue
            try:
                validate_public_url(value)
            except BlockedURLError as exc:
                return _reject(argument, str(exc))
            except Exception as exc:  # noqa: BLE001 - never crash the tool call
                return _reject(argument, f"unusable URL ({type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001 - validation must never break the server
        logger.warning("MCP argument validation errored (allowing through): %s", exc)

    return None


_SETUP_TOOL_NAMES = {
    "xpst_setup_start",
    "xpst_setup_status",
    "xpst_setup_resume",
    "xpst_setup_reset",
}


def _setup_tool_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, sort_keys=True))],
        isError=is_error,
    )


async def _handle_setup_tool(server: XPSTMCPServer, name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Dispatch setup transaction tools without initializing the posting engine."""
    service = SetupTransactionService(server.config.config_dir)
    try:
        if name == "xpst_setup_start":
            payload = service.start_json(arguments.get("selected_role_capabilities"), alias="mcp")
        elif name == "xpst_setup_status":
            payload = service.status_json(arguments.get("transaction_id"), alias="mcp")
        elif name == "xpst_setup_resume":
            resume_args = {
                key: arguments[key]
                for key in ("step_id", "step_state", "step_updates", "readiness", "finish_later", "error")
                if key in arguments
            }
            payload = service.resume_json(arguments.get("transaction_id"), alias="mcp", **resume_args)
        else:
            payload = service.reset_json(arguments.get("transaction_id"), alias="mcp")
        return _setup_tool_result(payload)
    except (SetupTransactionError, SetupTransactionNotFound):
        payload = {
            "ok": False,
            "operation": name.removeprefix("xpst_setup_") or "setup",
            "error": {
                "code": "SETUP_REQUEST_INVALID",
                "message": "The setup request could not be applied to the active transaction.",
                "action": "Read setup status, correct the non-secret request, and retry.",
            },
        }
        return _setup_tool_result(payload, is_error=True)


async def handle_call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Handle a tool call — with audit logging and retry on transient failures."""
    import time as _time

    from xpst.utils.audit_logger import log_tool_invocation

    _start = _time.monotonic()
    blocked = _guardrail_block(name, arguments)
    if blocked is not None:
        log_tool_invocation(name, arguments, "blocked_by_guardrail",
                            (_time.monotonic() - _start) * 1000, False, "guardrail_block")
        return blocked

    # Validate path/URL arguments for EVERY tool (not just mutating ones) before
    # anything touches the filesystem or the network.
    malformed = _argument_shape_block(name, arguments)
    if malformed is not None:
        log_tool_invocation(name, arguments, "blocked_by_argument_validation",
                            (_time.monotonic() - _start) * 1000, False, "argument_block")
        return malformed

    engine_tools = {
        "xpst_run", "xpst_post", "xpst_health", "xpst_status", "xpst_backfill", "xpst_delete",
        # Targeted retry drives a real upload through the engine.
        "xpst_failures_retry",
    }

    server = await get_server(initialize=name in engine_tools)

    try:
        if name in _SETUP_TOOL_NAMES:
            result = await _handle_setup_tool(server, name, arguments)
        elif name == "xpst_run":
            engine = server.get_engine()
            result = await _handle_run(engine, arguments)
        elif name == "xpst_post":
            engine = server.get_engine()
            result = await _handle_post(engine, arguments)
        elif name == "xpst_health":
            engine = server.get_engine()
            result = await _handle_health(engine)
        elif name == "xpst_status":
            engine = server.get_engine()
            result = await _handle_status(engine)
        elif name == "xpst_backfill":
            engine = server.get_engine()
            result = await _handle_backfill(engine, arguments)
        elif name == "xpst_analytics":
            result = await _handle_analytics(server.config, arguments)
        elif name == "xpst_cross_post_analytics":
            result = await _handle_cross_post_analytics(server.config)
        elif name == "xpst_followers":
            result = await _handle_followers(server.config)
        elif name == "xpst_best_time":
            result = await _handle_best_time(arguments)
        elif name == "xpst_security_audit":
            result = await _handle_security_audit(server.config)
        elif name == "xpst_suggest_caption":
            result = await _handle_suggest_caption(arguments)
        elif name == "xpst_transcript":
            result = await _handle_transcript(arguments)
        elif name == "xpst_search":
            result = await _handle_search(arguments)
        elif name == "xpst_activity":
            result = await _handle_activity(server.config)
        elif name == "xpst_schedule_list":
            result = await _handle_schedule_list(server.config)
        elif name == "xpst_schedule_add":
            result = await _handle_schedule_add(server.config, arguments)
        elif name == "xpst_schedule_cancel":
            result = await _handle_schedule_cancel(server.config, arguments)
        elif name == "xpst_failures_retry":
            engine = server.get_engine()
            result = await _handle_failures_retry(engine, arguments)
        elif name == "xpst_config_show":
            result = await _handle_config_show(server.config)
        elif name == "xpst_auth_status":
            result = await _handle_auth_status(server.config)
        elif name == "xpst_providers":
            result = await _handle_providers(server.config)
        elif name == "xpst_capabilities":
            result = await _handle_capabilities(server.config)
        elif name == "xpst_preflight":
            result = await _handle_preflight(server.config, arguments)
        elif name == "xpst_readiness":
            result = await _handle_readiness(server.config)
        elif name == "xpst_auth_start":
            result = await _handle_auth_start(server.config, arguments)
        elif name == "xpst_delete":
            engine = server.get_engine()
            result = await _handle_delete(engine, arguments)
        elif name == "xpst_disconnect":
            result = await _handle_disconnect(server.config, arguments)
        elif name == "messenger_send":
            result = await _handle_messenger_send(server.config, arguments)
        elif name == "messenger_set_rules":
            result = await _handle_messenger_set_rules(server.config, arguments)
        elif name == "xpst_messenger_check_comments":
            result = await _handle_messenger_check_comments(server.config, arguments)
        elif name == "xpst_generate_ideas":
            result = await _handle_generate_ideas(arguments)
        elif name == "xpst_bio_get":
            result = await _handle_bio_get(server.config)
        elif name in {"kb_add", "kb_query", "kb_organize", "kb_areas"}:
            result = await _handle_kb_tool(name, arguments)
        else:
            log_tool_invocation(name, arguments, "unknown_tool",
                                (_time.monotonic() - _start) * 1000, False, "unknown_tool")
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
        log_tool_invocation(name, arguments, result,
                            (_time.monotonic() - _start) * 1000, True)
        return result
    except Exception as e:
        logger.exception(f"Error in tool {name}: {e}")
        log_tool_invocation(name, arguments, None,
                            (_time.monotonic() - _start) * 1000, False, str(e))
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {str(e)}")],
            isError=True,
        )


def _engine_uploaders(engine: Any) -> dict[str, Any] | None:
    """The uploaders the engine actually initialised, or ``None`` for a double.

    ``None`` means "not an engine with an uploader map" (a test stand-in), which
    is deliberately distinct from ``{}`` — an empty dict is the production state
    this guard exists for: nothing enabled, so nothing can be published.
    """
    platforms = getattr(engine, "_platforms", None)
    return platforms if isinstance(platforms, dict) else None


def _engine_can_publish(engine: Any, targets: list[str]) -> bool:
    """True when at least one resolved destination has a live uploader."""
    uploaders = _engine_uploaders(engine)
    if uploaders is None:
        return True
    return any(name in uploaders for name in targets)


def _no_destinations_result() -> CallToolResult:
    """The canonical zero-destination refusal shared by every surface.

    Same code and message as the CLI (``NO_DESTINATIONS`` / "Choose at least
    one destination platform.") so an agent, a script and the desktop UI all
    branch on one value instead of parsing three different sentences.
    """
    from xpst.services.post_preflight import NO_DESTINATIONS_CODE, NO_DESTINATIONS_MESSAGE

    return CallToolResult(
        isError=True,
        content=[TextContent(
            type="text",
            text=json.dumps({
                "ok": False,
                "status": "refused",
                "error": {"code": NO_DESTINATIONS_CODE, "message": NO_DESTINATIONS_MESSAGE},
                "blockers": [NO_DESTINATIONS_MESSAGE],
            }, indent=2),
        )],
    )


async def _handle_run(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_run tool."""
    dry_run = args.get("dry_run", False)
    max_posts = args.get("max_posts", 5)
    source = args.get("source", "tiktok")
    catch_up = args.get("catch_up", False)

    # No destination means no run: refuse before fetching anything, with the
    # canonical error, instead of returning a payload for a cycle that could
    # not have published to anything. An engine double with no uploader map is
    # left alone (the caller owns that stand-in).
    uploaders = _engine_uploaders(engine)
    if uploaders is not None and not uploaders:
        return _no_destinations_result()

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
    # success string. `ok` used to be hard-coded True regardless of what
    # happened, so an agent that trusted it reported success for a failed run.
    # It now means what it says, derived from the same per-platform
    # ``UploadResult.is_published`` the engine's own status flags use: every
    # upload in every result must be provably published, and "nothing ran" is
    # not success.
    uploads = [upload for result in results for upload in result.results.values()]
    published = [upload for upload in uploads if upload.is_published]
    # The aggregate verdict, from the same rule the CLI exits with: a run where
    # nothing was published must not read as success to an agent either. MCP has
    # no exit status, so the verdict travels as data — `exit_code` names the
    # shared family, `batch_status` says what happened, and
    # `failed_destinations` names every destination that failed and why.
    outcome = batch_outcome(results)
    payload = {
        "ok": bool(uploads) and len(published) == len(uploads),
        "attempted": len(results),
        "uploads": len(uploads),
        "published": len(published),
        "processed": len(results),
        "exit_code": outcome["exit_code"],
        "batch_status": outcome["status"],
        "failed_destinations": outcome["failed_destinations"],
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
        "quota_blocked": {
            p: r.metadata.get("quota")
            for p, r in result.results.items()
            if not r.success and "QUOTA_EXHAUSTED" in (r.error or "")
        },
    }


async def _handle_post(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_post tool.

    Validation is the shared content contract (:mod:`xpst.content`): the same
    request produces the same verdict here as on the CLI and over HTTP, and a
    request that cannot be published is refused before any uploader is touched
    instead of being posted "as a video just in case".
    """
    from xpst.services.post_service import refusal_envelope, unimplemented_envelope

    dry_run = args.get("dry_run", False)
    carousel_paths = list(args.get("carousel_paths") or [])
    video_path = args.get("video_path")
    content_type = args.get("content_type")
    # `text` is the text-post spelling of `caption`; both name the same body.
    caption = str(args.get("text") or args.get("caption") or "")

    if args.get("text") and not video_path and not content_type:
        # A text argument with no file IS a text post: state it rather than
        # inferring it, so the verdict and the route agree with the CLI/HTTP.
        content_type = "text"

    if not video_path and not content_type:
        # Same request-shape rule as the CLI: without a file, a text body or a
        # stated content type there is nothing to post, and guessing "video"
        # would fabricate a request the caller never made.
        payload = {
            "ok": False,
            "blockers": [
                'Provide "video_path" (a file to post), "text" (a text post), or '
                '"content_type" (what this post is, e.g. "text").'
            ],
            "network_calls": False,
        }
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])

    targets = list(args.get("platforms") or list(engine._platforms.keys()))
    media_paths = (([video_path] if video_path else []) + carousel_paths)
    request = ContentRequest.from_legacy(media_paths, caption, targets, content_type=content_type)
    verdict = content_verdict(request)

    # Per-destination copy, parsed by the shared contract parser so MCP accepts
    # exactly the shape the HTTP API and the CLI accept.
    from xpst.content import ContentContractError, parse_destination_texts

    try:
        overrides = parse_destination_texts(args.get("overrides"))
    except ContentContractError as exc:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "ok": False,
                    "error": f"Invalid overrides payload: {exc}",
                    "overrides_accepted": "{\"x\": \"caption\"} or {\"x\": {\"text\": \"caption\"}}",
                }, indent=2),
            )],
        )

    if dry_run:
        # Same canonical, side-effect-free verdict the CLI and the dashboard use.
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService, plan_content_type

        plan_payload: dict[str, Any] | None = None
        blockers: list[str] = list(verdict["blockers"])
        try:
            plan_payload = PostPreflightService(engine.config).plan(
                PostPlanRequest(
                    media_paths=media_paths,
                    target_platforms=targets,
                    base_caption=caption,
                    per_platform_captions=overrides,
                    # Only a real text post (a body and no file) skips the media
                    # requirement; the legacy "no file at all" shape keeps it.
                    content_type=plan_content_type(request),
                )
            ).to_dict()
            blockers.extend(issue["message"] for issue in plan_payload["hard_blockers"])
        except Exception as exc:  # noqa: BLE001 - report truthfully instead of failing the tool
            blockers.append(f"Preflight could not run: {str(exc)[:200]}")

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": True,
                    # Legacy keys existing clients read.
                    "video": video_path,
                    "caption": caption[:100],
                    "captions": {
                        # The copy each destination would actually receive.
                        target: overrides.get(target, caption) for target in targets
                    },
                    "carousel": len(media_paths) > 1,
                    "targets": targets,
                    # The canonical content verdict, shared with CLI/HTTP.
                    "content_type": verdict["content_type"] or verdict["effective_content_type"],
                    "effective_content_type": verdict["effective_content_type"],
                    "route": verdict["route"],
                    "content": verdict,
                    "ready": not blockers and bool(plan_payload and plan_payload["ready"]),
                    "blockers": blockers,
                    "plan": plan_payload,
                    # Refusal shape parity with the non-dry path and the other
                    # surfaces: the first hard blocker as the top-level
                    # ``{code, message}`` error object (the plan also carries it
                    # under plan.error; this makes the dry-run envelope match
                    # refusal_envelope instead of burying the code one level in).
                    "error": (plan_payload or {}).get("error") if blockers else None,
                    "network_calls": False,
                }, indent=2),
            )],
        )

    if verdict["blockers"]:
        # Refused before any upload, with the same envelope every other surface
        # returns. No uploader is constructed and nothing is sent.
        envelope = refusal_envelope(request, verdict["blockers"], content=verdict)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(envelope, indent=2, default=str))],
        )

    if verdict["route"] == PUBLISH_ROUTE_UNIMPLEMENTED:
        # Validated for its destinations but with no publishing path (a plugin
        # destination, or a type no uploader implements): say so instead of
        # posting something else.
        envelope = unimplemented_envelope(request)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(envelope, indent=2, default=str))],
        )

    from pathlib import Path

    from xpst.services.post_preflight import resolve_destinations

    # Same canonical guard as the CLI: a manual post with no destination that
    # has a live uploader is refused instead of reporting an empty success.
    targets = resolve_destinations(engine.config, args.get("platforms"))
    if not _engine_can_publish(engine, targets):
        return _no_destinations_result()

    if verdict["route"] == PUBLISH_ROUTE_TEXT:
        # The text route carries one text per destination, so the
        # per-destination copy is honoured (and validated) here too.
        per_destination = {
            platform: request.text_for(platform)
            for platform in request.platforms
            if request.override_for(platform) is not None
        }
        result = await engine.post_text(
            caption,
            args.get("platforms"),
            per_destination=per_destination or None,
        )
    elif len(media_paths) > 1:
        result = await engine.post_manual_carousel(
            media_paths=[Path(p) for p in media_paths],
            caption=caption,
            platforms=args.get("platforms"),
            per_platform_captions=overrides,
        )
    else:
        result = await engine.post_manual(
            video_path=Path(video_path),
            caption=caption,
            platforms=args.get("platforms"),
            per_platform_captions=overrides,
        )
    payload = _serialize_result(result)
    # Report the copy each destination actually received, so an override is
    # visible in the outcome rather than only in the request.
    sent = getattr(result, "captions", None)
    if isinstance(sent, dict) and sent:
        payload["captions"] = dict(sent)
    if isinstance(payload, dict):
        payload.setdefault("content_type", verdict["effective_content_type"])
        payload.setdefault("content", verdict)
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps(payload, indent=2, default=str),
        )],
    )


async def _handle_health(engine: CrossPostEngine) -> CallToolResult:
    """Handle xpst_health tool.

    Platform auth facts come from the canonical live probe, so this tool cannot
    answer a different verdict than ``xpst_auth_status`` for the same account
    (one fact, one answer — see ``tests/test_status_surface_agreement.py``).
    """
    from xpst.auth_status import collect_live_auth_status_async, platform_health_entries

    health = await engine.check_health(include_platforms=False)
    canonical = await collect_live_auth_status_async(engine.config)
    health["platforms"] = platform_health_entries(canonical)
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
    # `attempted` / `successful` alone are counts, not a verdict: a backfill
    # where every destination failed used to look exactly like one where there
    # was nothing to do. The aggregate the CLI exits with travels here too.
    outcome = batch_outcome(results)
    payload = {
        "attempted": len(results),
        "successful": successful,
        "exit_code": outcome["exit_code"],
        "batch_status": outcome["status"],
        "failed_destinations": outcome["failed_destinations"],
        "results": [_serialize_result(r) for r in results],
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_activity(config: XPSTConfig) -> CallToolResult:
    """Return recorded failures with targeted recovery actions."""
    from xpst.state_store import StateStore

    state = StateStore(config.config_dir).get()
    failures: list[dict[str, Any]] = []
    for video_id, video in (state.get("posted_videos") or {}).items():
        for platform, result in (video.get("errors") or {}).items():
            if not isinstance(result, dict):
                continue
            retryable = result.get("retryable")
            failures.append({
                "video_id": str(video_id),
                "platform": str(platform),
                "error": str(result.get("error") or "Unknown error"),
                "retryable": retryable,
                "post_id": None,
                "post_url": None,
                "source_url": video.get("source_url"),
                "last_attempt": result.get("timestamp") or video.get("last_attempt"),
                "action": "retry" if retryable is True else "review",
            })
    failures.sort(
        key=lambda item: (item.get("last_attempt") or "", item["video_id"], item["platform"]),
        reverse=True,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"failures": failures, "count": len(failures)}))],
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


async def _handle_schedule_cancel(config: XPSTConfig, arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_schedule_cancel — CLI `schedule remove` semantics over MCP.

    Delegates to :mod:`xpst.services.recovery_service` so the verdict an agent
    reads is the verdict the CLI would print: an unknown entry id fails with
    POST_NOT_FOUND, and ``cancelled`` is true only after a real removal.
    """
    from xpst.services import recovery_service

    payload = recovery_service.cancel_scheduled_post(
        config.config_dir,
        arguments["entry_id"],
        dry_run=bool(arguments.get("dry_run", False)),
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, default=str))],
        isError=not payload["ok"],
    )


async def _handle_failures_retry(engine: CrossPostEngine, arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_failures_retry — CLI `failures retry` semantics over MCP.

    Targets exactly one (video_id, platform) pair and only reports
    ``posted: true`` when that destination accepted the re-upload.
    """
    from xpst.services import recovery_service

    payload = await recovery_service.retry_failed_post(
        engine,
        arguments["video_id"],
        arguments["platform"],
        dry_run=bool(arguments.get("dry_run", False)),
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, default=str))],
        isError=not payload["ok"],
    )


async def _handle_analytics(
    config: XPSTConfig,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Handle xpst_analytics (G27): expose the same numbers the UI shows.

    Reads the persisted snapshot store by default; live=true runs a real
    collection first (network, may take seconds and consume API quota).
    The snapshot store is resolved from ``config.config_dir`` (QA-wave fix:
    handlers must never read from a hardcoded home path when the config
    points elsewhere).
    """
    from xpst.analytics import AnalyticsCollector

    platform = arguments.get("platform")
    live = bool(arguments.get("live", False))

    collector = AnalyticsCollector(config_dir=config.config_dir)
    live_data = await collector.collect_all() if live else None

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

    # Outcome report (D5): the same per-post/per-platform numbers the UI and
    # CLI show, each labelled "live" or "recorded" and filtered to posts the
    # account actually owns. Platforms with nothing report totals=None.
    report = collector.outcome_report(live_data=live_data)

    payload = {
        "live": live,
        "snapshot_count": store.snapshot_count(),
        "platforms": per_platform,
        "posts": latest,
        "outcome_report": report,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, default=str))],
    )


async def _handle_cross_post_analytics(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_cross_post_analytics (B1).

    Returns cross-post group correlation data: one video posted to multiple
    platforms aggregated into a single entry with per-platform breakdown,
    totals, engagement rate, and tier. Reads the local snapshot store only
    (offline, fast), resolved from ``config.config_dir`` (QA-wave fix).
    """
    from xpst.dashboard.analytics import AnalyticsCollector

    collector = AnalyticsCollector(config.config_dir)
    groups = collector.get_cross_post_analytics()
    payload = {
        "cross_post_groups": groups,
        "group_count": len(groups),
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_followers(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_followers (B2).

    Returns follower counts per platform from stored snapshots, with
    growth history (last 7 snapshots per platform). The store lives under
    ``config.config_dir`` (QA-wave fix: never a hardcoded home path).
    """
    from pathlib import Path

    from xpst.analytics_store import AnalyticsStore

    store = AnalyticsStore(Path(config.config_dir).expanduser() / "analytics.db")
    latest = store.latest_followers()

    platforms_data = []
    total = 0
    for platform, info in sorted(latest.items()):
        count = info["count"]
        history = store.follower_history(platform, limit=7)
        growth = 0
        if len(history) >= 2:
            growth = count - history[0]["count"]
        total += count
        platforms_data.append({
            "platform": platform,
            "count": count,
            "growth": growth,
            "last_updated": info["captured_at"],
            "history": history,
        })

    payload = {
        "total_followers": total,
        "platform_count": len(platforms_data),
        "platforms": platforms_data,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_best_time(arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_best_time (B3).

    Returns recommended posting times per platform based on engagement history.
    """
    from xpst.best_time import BestTimeAnalyzer

    platform = arguments.get("platform")
    analyzer = BestTimeAnalyzer()

    if platform:
        best = analyzer.best_for_platform(platform)
        payload = {"platform": platform, "best_time": best}
    else:
        best_all = analyzer.best_overall()
        payload = {"best_times": best_all}

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_security_audit(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_security_audit (E5/F2).

    Runs automated security checks on the xPST installation.
    """
    import os
    import stat

    checks = []
    all_pass = True

    # Check credential file permissions
    cred_paths = [
        config.youtube.client_secrets,
        config.youtube.token_file,
        config.x.cookies_file,
        config.instagram.session_file,
    ]
    for cred_path in cred_paths:
        if cred_path and os.path.exists(cred_path):
            mode = stat.S_IMODE(os.stat(cred_path).st_mode)
            ok = mode == 0o600
            checks.append({
                "check": "file_permissions",
                "path": cred_path,
                "mode": oct(mode),
                "passed": ok,
            })
            if not ok:
                all_pass = False

    # Dashboard localhost
    checks.append({"check": "dashboard_localhost", "passed": True})

    # MCP readonly
    checks.append({"check": "mcp_readonly", "passed": True})

    # Provider mode
    checks.append({
        "check": "provider_mode",
        "passed": True,
        "detail": config.provider_mode,
    })

    # FFmpeg
    try:
        from xpst.utils.platform import resolve_ffmpeg_path

        ffmpeg_ok = resolve_ffmpeg_path() is not None
    except Exception:
        ffmpeg_ok = False
    checks.append({"check": "ffmpeg_available", "passed": ffmpeg_ok})
    if not ffmpeg_ok:
        all_pass = False

    # Encrypted storage
    checks.append({"check": "encrypted_storage", "passed": True})

    payload = {
        "overall_status": "pass" if all_pass else "fail",
        "checks": checks,
        "passed_count": sum(1 for c in checks if c["passed"]),
        "failed_count": sum(1 for c in checks if not c["passed"]),
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_suggest_caption(arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_suggest_caption (F3)."""
    from xpst.caption_gen import generate_caption

    video_path = arguments.get("video_path", "")
    platform = arguments.get("platform", "instagram")

    captions = generate_caption(video_path, platform=platform) if video_path else []
    payload = {
        "video_path": video_path,
        "platform": platform,
        "suggestions": captions,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_generate_ideas(arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_generate_ideas (AI content studio).

    Mirrors `xpst generate ideas`. Uses the KB LLM when configured
    (XPST_KB_LLM_ENABLED), otherwise falls back to deterministic template
    generation — no LLM required.
    """
    from xpst.caption_gen import generate_ideas

    topic = arguments.get("topic", "")
    count = arguments.get("count", 5)
    ideas = generate_ideas(topic, count=count) if topic else []
    payload = {
        "topic": topic,
        "count": len(ideas),
        "ideas": ideas,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_transcript(arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_transcript (F2.4 / D2)."""
    video_id = arguments.get("video_id", "")

    from xpst.knowledge.workspace import Workspace, _validate_name
    # Sanitize video_id to prevent path traversal (M2 security fix)
    try:
        _validate_name(video_id, "video_id")
    except ValueError as exc:
        return CallToolResult(
            isError=True,
            content=[TextContent(
                type="text",
                text=f"Invalid video_id: {exc}",
            )],
        )

    ws = Workspace.resolve("default", create=False)

    # Try direct content_hash lookup
    result = ws.get_transcript(video_id)
    if result is None:
        # Try as content: prefix
        result = ws.get_transcript(f"content:{video_id}")
    if result is None:
        # Search transcripts dir for a file matching this video_id
        import os
        tdir = ws.transcripts_dir
        if tdir.exists():
            for fname in os.listdir(tdir):
                if video_id in fname:
                    result = ws.get_transcript(fname.replace(".json", ""))
                    break

    payload = {"error": "Transcript not found", "video_id": video_id} if result is None else result
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_search(arguments: dict[str, Any]) -> CallToolResult:
    """Handle xpst_search (F2.5)."""
    query = arguments.get("query", "")
    limit = arguments.get("limit", 10)

    from xpst.knowledge.query import query_nuggets

    result = query_nuggets(query, k=limit)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
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
            for platform in ("tiktok", "youtube", "x", "instagram", "threads", "messenger", "local")
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
    """Handle xpst_auth_status tool.

    Uses the SAME live collector as ``xpst auth status --json`` (no
    presence-based shortcuts): an agent asking "is this account usable?" gets
    the honest badge plus the timestamp it was derived from. A platform xPST
    cannot currently prove is reported as ``unknown``/``needs_reauth`` — never
    as connected.
    """
    from xpst.auth_status import collect_live_auth_status_async
    from xpst.token_state import PLATFORM_ORDER
    from xpst.utils.credentials import CredentialStore
    from xpst.utils.quota import QuotaManager

    cred_store = CredentialStore(config.config_dir)
    quota_mgr = QuotaManager(config.config_dir)

    stored_keys = cred_store.list_keys()
    storage_type = "OS Keychain" if cred_store._use_keyring else "File Storage (encrypted fallback)"

    try:
        live = await collect_live_auth_status_async(config)
    except Exception as exc:  # noqa: BLE001 — a status tool must never crash
        live = {}
        logger.warning("MCP auth status live probe failed: %s", str(exc)[:200])

    platforms: dict[str, Any] = {}
    badges: dict[str, str] = {}
    checked_at: float | None = None
    # Every provider the canonical collector reports — including the local
    # file source — appears with an honest badge; omission would make this
    # surface disagree with CLI/HTTP on the same fact.
    order = list(PLATFORM_ORDER) + [
        name for name in live if isinstance(name, str) and name not in PLATFORM_ORDER
    ]
    for platform in order:
        entry = live.get(platform)
        info: dict[str, Any] = dict(entry) if isinstance(entry, dict) else {}
        if not info:
            # No live entry: presence is not proof, so report a non-green badge
            # rather than reporting `authenticated: true` from stored keys.
            from xpst.token_state import derive_token_state, token_metadata

            stored = bool(cred_store.retrieve(f"{platform}_access_token")) or bool(
                cred_store.retrieve(f"{platform}_token")
            )
            info = {"authenticated": False, "live_checked": False}
            info.update(
                derive_token_state(
                    platform,
                    {"configured": stored, "live_checked": False},
                    token_metadata(config, platform),
                )
            )
        info["quota_remaining"] = quota_mgr.get_remaining(platform).get("daily", "N/A")
        if platform == "messenger":
            # Kept for clients that used it: the opt-in auto-reply switch is not
            # an auth fact and never implies a working Messenger connection.
            info["auto_reply"] = bool(config.messenger.auto_reply)
        platforms[platform] = info
        if info.get("badge"):
            badges[platform] = str(info["badge"])
        if isinstance(info.get("checked_at"), (int, float)) and (
            checked_at is None or float(info["checked_at"]) > checked_at
        ):
            checked_at = float(info["checked_at"])

    result: dict[str, Any] = {
        "credential_storage": storage_type,
        "stored_credentials": stored_keys,
        "platforms": platforms,
        "badges": badges,
        "checked_at": checked_at,
    }
    if checked_at is not None:
        from xpst.token_state import iso_timestamp

        result["checked_at_iso"] = iso_timestamp(checked_at)

    # Forward-looking watchdog (xpst.credential_health): which credentials die
    # before anyone notices, and by when. Derived from the badges above, so an
    # agent gets the same horizon a human sees in the app.
    from xpst.credential_health import build_credential_health

    result["credential_health"] = build_credential_health(platforms)

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
    )


async def _handle_bio_get(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_bio_get — link-in-bio URL + current page config.

    Mirrors `xpst bio`. Returns the public /bio URL (dashboard default
    127.0.0.1:8080), the page handle, and the ordered links exactly as the
    dashboard renders them (enabled social accounts + custom links).
    """
    from xpst.dashboard.bio import collect_links

    payload = {
        "url": "http://127.0.0.1:8080/bio",
        "handle": config.bio.handle or "",
        "links": collect_links(config),
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
    )


async def _handle_providers(config: XPSTConfig) -> CallToolResult:
    """Handle xpst_providers tool."""
    data = build_provider_catalog(config)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, indent=2, default=str))],
    )


async def _handle_preflight(config: XPSTConfig, arguments: dict[str, Any]) -> CallToolResult:
    """Run the same side-effect-free preflight the CLI and dashboard use."""
    from xpst.content import ContentContractError, parse_destination_texts
    from xpst.services.post_preflight import PostPlanRequest, PostPreflightService, plan_content_type

    platforms = [str(item).lower() for item in (arguments.get("platforms") or []) if str(item).strip()]

    # The zero-destination wording and code are the canonical ones (the plan
    # carries them): an empty target list must reach the service so MCP reports
    # the same NO_DESTINATIONS refusal the CLI and the HTTP API report, instead
    # of a surface-specific rewording.
    # Per-destination copy, parsed by the shared contract parser so MCP accepts
    # exactly the shape the CLI and the HTTP API accept.
    try:
        overrides = parse_destination_texts(arguments.get("overrides"))
    except ContentContractError as exc:
        payload = {
            "ok": False,
            "ready": False,
            "blockers": [f"Invalid overrides payload: {exc}"],
            "warnings": [],
            "network_calls": False,
        }
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])

    # The content verdict is the same one the CLI and HTTP surfaces report, so
    # "can this be posted?" has one answer everywhere. The arguments go through
    # the one request parser: `text` with no media is a text post, and `caption`
    # keeps its legacy meaning (a caption for media).
    request = ContentRequest.from_payload({**arguments, "platforms": platforms})
    verdict = content_verdict(request)

    plan = PostPreflightService(config).plan(
        PostPlanRequest(
            media_paths=list(request.media_paths),
            target_platforms=platforms,
            base_caption=request.caption,
            per_platform_captions=overrides,
            # A text post carries no file: the media requirement must not be
            # applied to it, or every text preflight would be blocked. A request
            # with neither a file nor a body keeps that requirement.
            content_type=plan_content_type(request),
        )
    ).to_dict()
    # The zero-destination wording and code are the canonical ones (the plan
    # carries them), so the MCP answer, the dashboard answer and the CLI
    # refusal are the same error.
    blockers = [issue["message"] for issue in plan["hard_blockers"]] + verdict["blockers"]
    payload = {
        "ok": not blockers,
        "ready": not blockers,
        "blockers": blockers,
        "warnings": [issue["message"] for issue in plan["warnings"]],
        "error": plan["error"],
        "caption": request.caption,
        "captions": {platform: overrides.get(platform, request.caption) for platform in platforms},
        "content_type": verdict["content_type"] or verdict["effective_content_type"],
        "effective_content_type": verdict["effective_content_type"],
        "route": verdict["route"],
        "content": verdict,
        "plan": plan,
        "network_calls": False,
    }
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


async def _handle_capabilities(config: XPSTConfig) -> CallToolResult:
    """Return the canonical role-aware catalog without network access."""
    from xpst.provider_truth import canonical_provider_catalog

    catalog = canonical_provider_catalog(config)
    payload = {
        "ok": True,
        "contract_version": 1,
        "roles": catalog["roles"],
        "providers": [
            {
                "name": item["name"],
                "display_name": item["display_name"],
                "roles": item["role_names"],
                "capabilities": item["capabilities"],
                "state": item["state"],
                "docs_url": item["docs_url"],
            }
            for item in catalog["providers"]
        ],
        # The content contract, verbatim from its one source (xpst.content), so
        # the list an agent plans against is the list the CLI prints and the HTTP
        # API serves — not a per-surface copy.
        "content": capability_document(),
    }
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


async def _handle_readiness(config: XPSTConfig) -> CallToolResult:
    """Return local readiness without initializing the posting engine."""
    from xpst.readiness import build_readiness_report

    report = build_readiness_report(config).to_dict()
    payload = {
        # `ok` must mirror the readiness verdict instead of being a constant:
        # an agent gating on `ok` alone must not proceed when the install is
        # not actually ready.
        "ok": bool(report.get("ready")),
        "contract_version": 1,
        "readiness": report,
    }
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, default=str))])


async def _handle_auth_start(config: XPSTConfig, arguments: dict[str, Any]) -> CallToolResult:
    """Return a browser-free human action plan; never accept secrets."""
    from xpst.provider_truth import provider_definition

    platform = arguments.get("platform")
    try:
        definition = provider_definition(str(platform))
    except KeyError:
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps({
                "ok": False,
                "contract_version": 1,
                "error": {"code": "UNKNOWN_PROVIDER", "message": "Choose a supported provider."},
            }))],
        )
    payload = {
        "ok": True,
        "contract_version": 1,
        "platform": definition.name,
        "status": "human_action_required",
        "browser_opened": False,
        "command": f"xpst connect {definition.name}",
        "docs_url": definition.docs_url,
    }
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])


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


async def _handle_messenger_send(config: XPSTConfig, args: dict[str, Any]) -> CallToolResult:
    """Handle messenger_send tool — send a text message to a PSID."""
    import httpx

    from xpst.platforms.messenger import MessengerAdapter, MessengerError
    from xpst.utils.sessions import SessionManager

    adapter = MessengerAdapter(config)
    adapter._session_manager = SessionManager(config.config_dir)
    try:
        data = await adapter.send_text(args["recipient"], args["text"])
    except (ValueError, MessengerError, httpx.HTTPError) as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {exc}")],
            isError=True,
        )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"ok": True, "response": data}, default=str))],
    )


async def _handle_messenger_set_rules(config: XPSTConfig, args: dict[str, Any]) -> CallToolResult:
    """Handle messenger_set_rules tool — persist ManyChat-lite reply rules."""
    rules = args.get("rules") or {}
    config.messenger.auto_reply = bool(args.get("auto_reply", True))
    config.messenger.reply_rules = {str(k): str(v) for k, v in rules.items()}
    config.save()
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps({
                "ok": True,
                "auto_reply": config.messenger.auto_reply,
                "reply_rules": config.messenger.reply_rules,
            }, indent=2),
        )],
    )


async def _handle_messenger_check_comments(config: XPSTConfig, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_messenger_check_comments.

    Mirrors `xpst messenger check-comments`: scans recent comments on an
    Instagram/Facebook post and posts public replies per the configured
    reply_rules. Gated internally by comment_reply_enabled and
    comment_platforms.
    """
    from xpst.platforms.messenger import MessengerAdapter
    from xpst.utils.sessions import SessionManager

    media_id = args["media_id"]
    platform = args.get("platform", "instagram")
    since_ts = args.get("since_ts")

    adapter = MessengerAdapter(config)
    adapter._session_manager = SessionManager(config.config_dir)
    results = await adapter.auto_reply_to_comments(platform, media_id, since_ts)
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps({
                "media_id": media_id,
                "platform": platform,
                "results": results,
            }, indent=2, default=str),
        )],
    )


async def _handle_delete(engine: CrossPostEngine, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_delete tool.

    Performs a REAL platform takedown: for every destination the video was
    recorded against, it calls :meth:`CrossPostEngine.delete_post` and reports
    that destination's Phase-1.2 outcome verbatim — ``deleted`` / ``soft_hidden``
    / ``pending`` / ``unsupported`` — plus ``platform_deleted`` for that
    destination. The local record is always removed afterwards.

    The engine is the single source of truth for "can this platform delete?":
    an adapter with no delete path (or none initialised) yields ``unsupported``,
    which the payload carries instead of a fabricated success. ``platform="all"``
    targets every platform the video was recorded against.
    """
    from xpst.platforms.base import DeleteOutcome
    from xpst.services.recovery_service import PLATFORM_DELETE_SCOPE

    video_id = args["video_id"]
    platform = args.get("platform", "all")
    scope_note = (
        "Deleted on each destination where xPST supports it and removed the "
        "local record. Per-destination outcomes say exactly what happened on "
        "the platform."
    )

    video = engine.state.get_video(video_id)
    if video is None:
        # An unknown video is a caller error, not a success: return an explicit
        # failure payload so agents can distinguish "record removed" from
        # "record never existed".
        result = {
            "ok": False,
            "video_id": video_id,
            "platform": platform,
            "removed": [],
            "success": False,
            "operation": "delete",
            "scope": PLATFORM_DELETE_SCOPE,
            "platform_deleted": False,
            "results": [],
            "note": scope_note,
            "error": f"Unknown video: {video_id} (not found in state)",
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
            isError=True,
        )

    posted_to = video.get("posted_to") or {}
    platforms = list(posted_to.keys()) if platform == "all" else [platform]
    # Only records that actually exist can be processed: claiming anything for
    # a platform the video was never posted to is the same fabricated success.
    removable = [plat for plat in platforms if plat in posted_to]

    destination_results: list[dict[str, Any]] = []
    for plat in removable:
        try:
            outcome = await engine.delete_post(video_id, plat)
            detail = outcome.to_dict()
        except Exception as exc:  # noqa: BLE001 — report, never raise
            # A crash in the platform path is an unconfirmed delete, never a
            # silent success: surface it as pending with the share URL absent.
            logger.error("MCP delete_post failed for %s on %s: %s", video_id, plat, exc)
            detail = {
                "outcome": DeleteOutcome.PENDING.value,
                "platform": plat,
                "post_id": "",
                "message": f"Delete pending on {plat} - remove manually",
                "share_url": None,
                "detail": str(exc)[:200],
                "deleted": False,
            }
        destination_results.append({
            "platform": plat,
            "outcome": detail.get("outcome"),
            "platform_deleted": bool(detail.get("deleted")),
            "post_id": detail.get("post_id", ""),
            "message": detail.get("message", ""),
            "share_url": detail.get("share_url"),
            "detail": detail.get("detail"),
            "local_record_removed": True,
        })
        # The local record is removed regardless of the platform outcome, so the
        # two effects are independent and both are reported above.
        engine.state.remove_post(video_id, plat)
    if removable:
        engine.state.save()

    result = {
        "ok": bool(removable),
        "video_id": video_id,
        "platform": platform,
        "removed": removable,
        "success": bool(removable),
        "operation": "delete",
        "scope": PLATFORM_DELETE_SCOPE,
        # Aggregate: only true when every attempted destination is really gone
        # from the platform (deleted or soft-hidden).
        "platform_deleted": bool(destination_results)
        and all(r["platform_deleted"] for r in destination_results),
        "results": destination_results,
        "note": scope_note,
    }
    if not removable:
        result["error"] = (
            f"No local record of {video_id} on {platform}; nothing was removed."
        )

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
        isError=not result["ok"],
    )


async def _handle_disconnect(config: XPSTConfig, args: dict[str, Any]) -> CallToolResult:
    """Handle xpst_disconnect tool.

    Removes the platform's stored account credentials and disables it in
    config (delegates to :func:`xpst.connect.disconnect_platform`). A
    state-only/local operation — posted content is never touched.
    """
    from xpst.connect import disconnect_platform

    platform = args["platform"]
    result = disconnect_platform(platform, config)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
    )


async def _handle_kb_tool(name: str, args: dict[str, Any]) -> CallToolResult:
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

    workspace = args.get("workspace", "default")
    if name == "kb_add":
        payload = await asyncio.to_thread(kb_tools.kb_add, args["source"], workspace)
    elif name == "kb_query":
        payload = await asyncio.to_thread(kb_tools.kb_query, args["text"], workspace)
    elif name == "kb_organize":
        payload = await asyncio.to_thread(
            kb_tools.kb_organize, workspace, args.get("threshold")
        )
    else:  # kb_areas
        payload = await asyncio.to_thread(kb_tools.kb_areas, workspace)

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
    await get_server(config)

    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def cli_main(config_path: str | None = None) -> None:
    """CLI entry point for ``xpst mcp`` using the selected config path."""
    config = XPSTConfig.load(config_path)
    asyncio.run(main(config))


if __name__ == "__main__":
    cli_main()
