"""Thin JSON API routers for the web UI (Phase 1 foundation)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)


def create_api_router(config_dir: str = "~/.xpst") -> APIRouter:
    """Build the /api router bound to a config directory."""
    router = APIRouter(prefix="/api", tags=["ui"])

    @router.get("/summary")
    def api_summary() -> dict[str, Any]:
        from xpst.dashboard.analytics import cached_summary_stats

        return cached_summary_stats(config_dir)

    @router.get("/videos")
    def api_videos() -> dict[str, Any]:
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
            history[f"{platform}:{post_id}"] = {"latest": latest, "series": series}
        return {"video_id": video_id, "posts": posts, "metrics": history}

    @router.get("/health-status")
    def api_health_status() -> dict[str, Any]:
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
            from xpst.provider_truth import canonical_status_report

            config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
            auth = collect_live_auth_status(config)
            canonical = canonical_status_report(config, auth)
            role_states = [
                role.get("state")
                for provider in canonical["providers"].values()
                for role in provider.get("role_status", {}).values()
                if role.get("enabled")
            ]
            if any(state in {"unconfigured", "degraded", "blocked_external_review"} for state in role_states):
                status = "degraded"
        except Exception as exc:
            auth_error = str(exc)[:200]
            logger.debug("Live auth status unavailable: %s", exc)
            canonical = {"providers": {}, "platforms": {}, "roles": []}

        return {
            "status": status,
            "platforms": platforms,
            "total_processed": health.get("total_processed", 0),
            "auth": auth,
            "auth_error": auth_error,
            "canonical": canonical,
            "providers": canonical["providers"],
        }

    @router.get("/providers")
    def api_providers() -> dict[str, Any]:
        from xpst.config import XPSTConfig
        from xpst.provider_truth import canonical_provider_catalog

        config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
        return canonical_provider_catalog(config)

    @router.get("/schedules")
    def api_schedules() -> dict[str, Any]:
        """Return persisted schedule entries without starting the scheduler."""
        from xpst.schedule_manager import ScheduleManager

        schedules = ScheduleManager(config_dir).list()
        return {"schedules": schedules, "count": len(schedules)}

    @router.get("/activity")
    def api_activity() -> dict[str, Any]:
        """Return recorded failures with targeted, truthful recovery metadata."""
        from xpst.state_store import StateStore

        state = StateStore(config_dir).get()
        failures: list[dict[str, Any]] = []
        for video_id, video in (state.get("posted_videos") or {}).items():
            errors = video.get("errors") or {}
            for platform, result in errors.items():
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
        return {"failures": failures, "count": len(failures)}

    @router.get("/library")
    def api_library() -> dict[str, Any]:
        """Return local library items with only verified platform results."""
        from xpst.state_store import StateStore

        state = StateStore(config_dir).get()
        items: list[dict[str, Any]] = []
        for video_id, video in (state.get("posted_videos") or {}).items():
            posts = []
            for platform, result in (video.get("posted_to") or {}).items():
                if not isinstance(result, dict):
                    continue
                post_id = result.get("id") or result.get("post_id")
                post_url = result.get("url") or result.get("post_url")
                if post_id or post_url:
                    posts.append({"platform": str(platform), "post_id": post_id, "post_url": post_url})
            items.append({
                "video_id": str(video_id),
                "source_url": video.get("source_url"),
                "source_platform": video.get("source_platform"),
                "caption": video.get("caption", ""),
                "last_attempt": video.get("last_attempt"),
                "posts": posts,
            })
        items.sort(key=lambda item: item.get("last_attempt") or "", reverse=True)
        return {"items": items, "count": len(items)}

    @router.get("/settings")
    def api_settings() -> dict[str, Any]:
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
