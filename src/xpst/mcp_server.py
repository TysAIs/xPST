"""
MCP (Model Context Protocol) server for xPST.

Exposes xPST cross-posting capabilities as MCP tools and resources
via stdio transport. Designed for integration with AI assistants.

Usage:
    xpst-mcp                    # stdio mode (default)
    xpst mcp                    # via CLI
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ── MCP Server ───────────────────────────────────────────────────────────

mcp = FastMCP(
    "xPST",
    instructions="Cross-posting engine for short-form video (YouTube Shorts, X/Twitter, Instagram Reels)",
)


def _get_engine(config_path: str | None = None):
    """Lazy-initialize the CrossPostEngine."""
    from xpst.config import XPSTConfig
    config = XPSTConfig.load(config_path)
    from xpst.engine import CrossPostEngine
    return CrossPostEngine(config)


# ── Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def post_video(
    video_path: str,
    caption: str,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    """Post a video file to one or more social media platforms.

    Args:
        video_path: Path to the video file on disk.
        caption: Caption/title for the post.
        platforms: Target platform names (e.g. ["youtube", "x"]). None = all enabled.

    Returns:
        Dict with per-platform upload results.
    """
    from pathlib import Path

    path = Path(video_path).expanduser()
    if not path.exists():
        return {"success": False, "error": f"Video file not found: {video_path}"}

    engine = _get_engine()
    result = asyncio.run(engine.post_manual(path, caption, platforms))
    return _result_to_dict(result)


@mcp.tool()
def crosspost_new(
    bidirectional: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Check for new videos and cross-post them to all platforms.

    Args:
        bidirectional: Check ALL sources for bidirectional cross-posting.
        limit: Maximum number of videos to process.

    Returns:
        List of dicts with per-video cross-posting results.
    """
    engine = _get_engine()

    if bidirectional:
        results = asyncio.run(engine.check_and_post_bidirectional())
    else:
        results = asyncio.run(engine.check_and_post())

    return [_result_to_dict(r) for r in results[:limit]]


@mcp.tool()
def check_status() -> dict[str, Any]:
    """Check xPST health status including quotas and circuit breakers.

    Returns:
        Dict with statistics, platform health, quotas, and circuit breaker states.
    """
    engine = _get_engine()

    health = asyncio.run(engine.check_health())
    state_stats = engine.state.get_statistics()
    quota_status = engine.quota_manager.get_status()
    cb_status = engine.circuit_breakers.get_status()
    dlq = engine.state.get_dead_letter_queue()

    return {
        "health": _jsonify(health),
        "statistics": _jsonify(state_stats),
        "quotas": _jsonify(quota_status),
        "circuit_breakers": _jsonify(cb_status),
        "dead_letter_queue_count": len(dlq) if dlq else 0,
    }


@mcp.tool()
def list_platforms() -> dict[str, Any]:
    """List all configured platforms with auth status and capabilities.

    Returns:
        Dict mapping platform names to their status, auth info, and capabilities.
    """
    engine = _get_engine()

    platforms_info = {}
    for name, uploader in engine._platforms.items():
        platforms_info[name] = {
            "enabled": True,
            "class": type(uploader).__name__,
            "supports_delete": hasattr(uploader, "delete"),
            "supports_carousel": hasattr(uploader, "upload_carousel"),
        }

    # Show configured but disabled platforms
    config = engine.config
    all_platforms = {"youtube", "x", "instagram"}
    for plat in all_platforms:
        if plat not in platforms_info:
            platform_config = getattr(config, plat, None)
            if platform_config is not None:
                platforms_info[plat] = {
                    "enabled": getattr(platform_config, "enabled", False),
                    "class": None,
                    "supports_delete": False,
                    "supports_carousel": False,
                }

    return platforms_info


@mcp.tool()
def get_analytics(
    platforms: list[str] | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Get engagement analytics across platforms.

    Args:
        platforms: Filter to specific platforms. None = all.
        top_n: Number of top posts to return.

    Returns:
        Dict with per-platform analytics and top posts.
    """
    from xpst.analytics import AnalyticsCollector

    engine = _get_engine()
    collector = AnalyticsCollector(engine.config.config_dir)

    post_ids = collector._discover_post_ids()
    if platforms:
        post_ids = {k: v for k, v in post_ids.items() if k in platforms}

    data = asyncio.run(collector.collect_all(post_ids))
    totals = collector.get_total_metrics(data)
    platform_totals = collector.get_platform_totals(data)

    # Top posts
    all_posts = []
    for platform, posts_data in data.items():
        for post_id, metrics in posts_data.items():
            metrics["platform"] = platform
            all_posts.append(metrics)
    all_posts.sort(key=lambda p: p.get("views", 0), reverse=True)

    return {
        "totals": totals,
        "platform_totals": platform_totals,
        "top_posts": all_posts[:top_n],
        "post_count": len(all_posts),
    }


@mcp.tool()
def delete_post(post_id: str, platform: str) -> dict[str, Any]:
    """Delete a previously posted video from a specific platform.

    Args:
        post_id: The video/post identifier.
        platform: Platform name (e.g. "youtube", "x", "instagram").

    Returns:
        Dict with deletion result.
    """
    engine = _get_engine()
    result = asyncio.run(engine.delete_post(post_id, platform))
    return {"success": result, "post_id": post_id, "platform": platform}


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Perform a connectivity health check on all sources and platforms.

    Tests authentication and connectivity without performing any uploads.

    Returns:
        Dict with source and platform health status.
    """
    engine = _get_engine()
    health = asyncio.run(engine.check_health())
    return _jsonify(health)


@mcp.tool()
def get_logs(lines: int = 50, level: str = "INFO") -> list[str]:
    """Retrieve recent log entries.

    Args:
        lines: Number of log lines to return.
        level: Minimum log level filter (DEBUG, INFO, WARNING, ERROR).

    Returns:
        List of log line strings.
    """
    from xpst.config import XPSTConfig

    config = XPSTConfig.load()
    log_path = Path(config.monitoring.log_file).expanduser()

    if not log_path.exists():
        return ["No log file found"]

    all_lines = log_path.read_text().splitlines()
    level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    min_level = level_order.get(level.upper(), 1)

    filtered = []
    for line in all_lines:
        for lvl, _ in level_order.items():
            if f"[{lvl}]" in line or f"{lvl}:" in line:
                if level_order[lvl] >= min_level:
                    filtered.append(line)
                break
        else:
            filtered.append(line)  # include lines without level markers

    return filtered[-lines:]


# ── Resources ────────────────────────────────────────────────────────────

@mcp.resource("xpst://config")
def get_config() -> str:
    """Return the current xPST configuration (sanitized — no secrets)."""
    from xpst.config import XPSTConfig

    config = XPSTConfig.load()
    return json.dumps({
        "config_dir": config.config_dir,
        "youtube_enabled": config.youtube.enabled,
        "x_enabled": config.x.enabled,
        "instagram_enabled": config.instagram.enabled,
        "download_dir": config.video.download_dir,
        "check_interval": config.schedule.check_interval,
        "notifications_enabled": config.notifications.enabled,
        "rate_limits": {
            "youtube": config.rate_limits.youtube,
            "instagram": config.rate_limits.instagram,
            "x": config.rate_limits.x,
            "tiktok": config.rate_limits.tiktok,
        },
    }, indent=2)


@mcp.resource("xpst://state")
def get_state() -> str:
    """Return the current cross-posting state summary."""
    from xpst.config import XPSTConfig
    from xpst.state import StateManager

    config = XPSTConfig.load()
    state = StateManager(config.config_dir)
    stats = state.get_statistics()
    return json.dumps(_jsonify(stats), indent=2)


@mcp.resource("xpst://health")
def get_health_resource() -> str:
    """Return current system health status."""
    from xpst.config import XPSTConfig
    from xpst.state import StateManager
    from xpst.utils.circuit_breaker import CircuitBreakerManager
    from xpst.utils.quota import QuotaManager

    config = XPSTConfig.load()
    state = StateManager(config.config_dir)
    quota = QuotaManager(config.config_dir)

    health = {
        "statistics": _jsonify(state.get_statistics()),
        "quotas": _jsonify(quota.get_status()),
    }
    return json.dumps(health, indent=2)


# ── Helpers ──────────────────────────────────────────────────────────────

def _result_to_dict(result) -> dict[str, Any]:
    """Convert a CrossPostResult to a plain dict."""
    out: dict[str, Any] = {
        "video_id": result.video_id,
        "caption": result.caption[:80],
        "all_success": result.all_success,
        "partial_success": result.partial_success,
        "platforms": {},
    }
    for platform, ur in result.results.items():
        out["platforms"][platform] = {
            "success": ur.success,
            "post_url": ur.post_url,
            "post_id": ur.post_id,
            "error": ur.error,
            "platform": ur.platform,
        }
    return out


def _jsonify(obj: Any) -> Any:
    """Recursively convert an object to JSON-safe types."""
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    return str(obj)


# ── Entry point ──────────────────────────────────────────────────────────

def main() -> None:
    """Start the MCP server over stdio transport."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
