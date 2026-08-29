"""Thin JSON API routers for the web UI (Phase 1 foundation).

Mounted at ``/api`` by :func:`xpst.dashboard.server._create_app`. Every
endpoint is a thin adapter over an EXISTING data method — no business
logic lives here:

=======================  ======================================================
Endpoint                 Data source (where it lives)
=======================  ======================================================
GET /api/summary         ``AnalyticsCollector.get_summary_stats``
                         (src/xpst/dashboard/analytics.py:925), reached through
                         ``cached_summary_stats`` (:mod:`~xpst.dashboard.analytics`,
                         line 119) so concurrent UI requests share one
                         fingerprint-memoized computation instead of stampeding.
GET /api/videos          ``AnalyticsCollector.get_video_lineup``
                         (src/xpst/dashboard/analytics.py:602) joined with
                         ``AnalyticsStore.platform_totals``
                         (src/xpst/analytics_store.py:137).
                         NOTE: ``AnalyticsStore.get_video_metrics_map`` does not
                         exist on this branch (grep-verified 2026-08-29), so the
                         rollup uses platform_totals; the lineup already carries
                         per-video/per-post latest metrics.
GET /api/videos/{id}     Lineup entries for that video_id plus per-post
                         snapshots via ``AnalyticsStore.latest_for_post``
                         (analytics_store.py:363) and ``AnalyticsStore.history``
                         (analytics_store.py:169). ``get_video_metrics`` also
                         does not exist on this branch — same reason as above.
GET /api/health-status   State-file health (``load_state``) plus live auth
                         liveness via ``collect_live_auth_status``
                         (src/xpst/auth_status.py:290; async core :237).
GET /api/settings        Sanitized config view reusing the MCP/CLI masker
                         ``xpst.cli._mask_sensitive_values`` (cli.py:2295),
                         the exact same masking ``xpst_config_show`` uses
                         (src/xpst/mcp/server.py:1354).
=======================  ======================================================

Auth: the router is mounted under the existing Basic auth middleware in
``server.py``. The middleware's exempt set is exactly
(/health, /metrics, /bio, /oauth/callback) — every /api/* path therefore
requires credentials. Do NOT add /api paths to the exempt set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)


def create_api_router(config_dir: str = "~/.xpst") -> APIRouter:
    """Build the /api router bound to a config directory.

    Args:
        config_dir: Path to the xPST config directory (same contract as
            :func:`xpst.dashboard.server._create_app`).

    Returns:
        An ``APIRouter`` with the Phase-1 read-only JSON endpoints.
    """
    router = APIRouter(prefix="/api", tags=["ui"])

    @router.get("/summary")
    def api_summary() -> dict[str, Any]:
        """Aggregate summary stats for the dashboard landing cards.

        Data: ``cached_summary_stats`` → ``AnalyticsCollector.get_summary_stats``
        (src/xpst/dashboard/analytics.py:925). Memoized on data-file
        fingerprints; never does network IO (G20).
        """
        from xpst.dashboard.analytics import cached_summary_stats

        return cached_summary_stats(config_dir)

    @router.get("/videos")
    def api_videos() -> dict[str, Any]:
        """Merged video lineup + platform totals rollup.

        Data: ``AnalyticsCollector.get_video_lineup``
        (src/xpst/dashboard/analytics.py:602) and
        ``AnalyticsStore.platform_totals`` (src/xpst/analytics_store.py:137)
        reached through the collector's cached store.
        """
        from xpst.dashboard.analytics import AnalyticsCollector

        collector = AnalyticsCollector(config_dir)
        lineup = collector.get_video_lineup()
        try:
            totals = collector._store().platform_totals()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("platform_totals read failed: %s", exc)
            totals = {}
        return {"videos": lineup, "count": len(lineup), "platform_totals": totals}

    @router.get("/videos/{video_id}")
    def api_video_detail(video_id: str) -> dict[str, Any]:
        """Per-video drill-down: all platform posts + snapshot history.

        Data: filtered ``get_video_lineup`` entries whose ``video_id``
        matches, joined with ``AnalyticsStore.latest_for_post`` (latest
        snapshot per platform post, analytics_store.py:363) and
        ``AnalyticsStore.history`` (snapshot time series,
        analytics_store.py:169). 404 when the video has no tracked posts.
        """
        from xpst.dashboard.analytics import AnalyticsCollector

        collector = AnalyticsCollector(config_dir)
        posts = [e for e in collector.get_video_lineup() if e.get("video_id") == video_id]
        if not posts:
            raise HTTPException(status_code=404, detail=f"Unknown video: {video_id}")

        history: dict[str, dict[str, Any]] = {}
        for post in posts:
            platform = str(post.get("platform") or "")
            post_id = str(post.get("post_id") or "")
            if not platform or not post_id:
                continue
            try:
                store = collector._store()
                latest = store.latest_for_post(platform, post_id)
                series = store.history(platform, post_id, limit=100)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Snapshot read failed for %s/%s: %s", platform, post_id, exc)
                latest, series = [], []
            history[f"{platform}:{post_id}"] = {
                "latest": latest,
                "series": series,
            }
        return {"video_id": video_id, "posts": posts, "metrics": history}

    @router.get("/health-status")
    def api_health_status() -> dict[str, Any]:
        """Engine health from state + live auth liveness per platform.

        Data: ``load_state`` health block (src/xpst/dashboard/analytics.py:70)
        and ``collect_live_auth_status`` (src/xpst/auth_status.py:290 —
        async core ``collect_live_auth_status_async`` :237). The live check
        has its own internal timeout and degrades to honest all-false
        entries instead of raising; any residual failure surfaces as
        ``auth_error`` with HTTP 200 (the UI renders a degraded state,
        it does not want a 5xx).
        """
        from xpst.dashboard.analytics import load_state

        state = load_state(config_dir)
        health = state.get("health", {})
        platforms = health.get("platforms", {})
        status = "healthy" if all(p.get("status") == "ok" for p in platforms.values()) else "degraded"

        auth: dict[str, Any] = {}
        auth_error: str | None = None
        try:
            from xpst.auth_status import collect_live_auth_status
            from xpst.config import XPSTConfig

            config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
            auth = collect_live_auth_status(config)
        except Exception as exc:
            auth_error = str(exc)[:200]
            logger.debug("Live auth status unavailable: %s", exc)

        return {
            "status": status,
            "platforms": platforms,
            "total_processed": health.get("total_processed", 0),
            "auth": auth,
            "auth_error": auth_error,
        }

    @router.get("/settings")
    def api_settings() -> dict[str, Any]:
        """Sanitized config view for the Settings page.

        Data: ``XPSTConfig.load`` masked with ``xpst.cli._mask_sensitive_values``
        (src/xpst/cli.py:2295) — the same recursive masker the MCP
        ``xpst_config_show`` tool reuses (src/xpst/mcp/server.py:1354),
        so no password hash / token / secret can leak through.
        """
        from xpst.cli import _mask_sensitive_values
        from xpst.config import XPSTConfig

        config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))

        def _section(obj: Any) -> dict[str, Any]:
            return dict(obj.__dict__) if hasattr(obj, "__dict__") else {}

        return _mask_sensitive_values(
            {
                "accounts": {
                    platform: _section(getattr(config, platform))
                    for platform in ("tiktok", "youtube", "x", "instagram", "threads", "messenger", "local")
                    if hasattr(config, platform)
                },
                "video": _section(config.video),
                "monitoring": _section(config.monitoring),
                "schedule": _section(config.schedule),
                "bio": _section(config.bio),
            }
        )

    return router
