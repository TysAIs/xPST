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

        provider_values = list(canonical.get("providers", {}).values())
        role_values = [
            role
            for provider in provider_values
            for role in provider.get("role_status", {}).values()
            if role.get("enabled")
        ]
        blockers = [
            {
                "platform": role.get("platform"),
                "role": role.get("role"),
                "state": role.get("state"),
                "error": role.get("error"),
            }
            for role in role_values
            if role.get("state") != "ready"
        ]
        destinations = [
            role for role in role_values if role.get("role") == "video_destination"
        ]
        destination = next(
            (role for role in destinations if role.get("state") == "ready"),
            destinations[0] if destinations else None,
        )
        destination_ready = bool(destination and destination.get("state") == "ready")
        next_action = (
            {
                "kind": "create_post",
                "label": "Create a post",
                "role": "video_destination",
            }
            if destination_ready
            else {
                "kind": "connect" if destination and destination.get("state") == "unconfigured" else "review",
                "label": "Connect a video destination" if destination and destination.get("state") == "unconfigured" else "Review readiness",
                "role": destination.get("role") if destination else "video_destination",
            }
        )
        ready = not blockers
        return {
            "status": status,
            "platforms": platforms,
            "total_processed": health.get("total_processed", 0),
            "auth": auth,
            "auth_error": auth_error,
            "canonical": canonical,
            "providers": canonical["providers"],
            "readiness": {"ready": ready, "blockers": blockers},
            "next_action": next_action,
            "can_create_post": destination_ready,
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

    @router.post("/preflight")
    def api_preflight(payload: dict[str, Any]) -> dict[str, Any]:
        """Run the canonical, side-effect-free post preflight.

        Delegates to ``PostPreflightService`` — the same service the CLI and the
        MCP tool use — so no surface can disagree about whether a post is ready.
        Only the request-shape preconditions (missing media or targets) are
        decided here; every media, caption, and destination verdict is canonical.
        """
        from xpst.config import XPSTConfig
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

        media_path = str(payload.get("media_path") or "").strip()
        caption = str(payload.get("caption") or "")
        platforms = [
            str(item).lower()
            for item in (payload.get("platforms") or [])
            if str(item).strip()
        ]

        request_blockers: list[str] = []
        if not platforms:
            # An empty target list would produce an empty plan that reports
            # "ready", so the request-shape precondition is decided here.
            request_blockers.append("Choose at least one destination platform.")

        plan: dict[str, Any] | None = None
        canonical_blockers: list[str] = []
        canonical_warnings: list[str] = []
        try:
            config = XPSTConfig.load(str(Path(config_dir).expanduser() / "config.yaml"))
        except Exception as exc:  # noqa: BLE001 - a missing config must not break preflight
            logger.debug("Preflight config load failed, using defaults: %s", exc)
            config = XPSTConfig()
        try:
            plan = PostPreflightService(config).plan(
                PostPlanRequest(
                    media_paths=[media_path] if media_path else [],
                    target_platforms=platforms,
                    base_caption=caption,
                )
            ).to_dict()
            canonical_blockers = [issue["message"] for issue in plan["hard_blockers"]]
            canonical_warnings = [issue["message"] for issue in plan["warnings"]]
        except Exception as exc:  # noqa: BLE001 - report truthfully instead of 500
            logger.warning("Preflight could not run: %s", exc)
            canonical_blockers = [f"Preflight could not run: {str(exc)[:200]}"]

        media = Path(media_path).expanduser() if media_path else None
        return {
            "ok": not request_blockers and not canonical_blockers,
            "ready": not request_blockers and not canonical_blockers,
            "media": {
                "path": media_path,
                "exists": bool(media and media.exists()),
                "is_file": bool(media and media.is_file()),
            },
            "caption": {
                "length": len(caption),
                "per_platform": {platform: caption for platform in platforms},
            },
            "platforms": platforms,
            "blockers": request_blockers + canonical_blockers,
            "warnings": canonical_warnings,
            "plan": plan,
            "network_calls": False,
        }

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
