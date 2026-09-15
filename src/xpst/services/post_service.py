"""Canonical manual-post service for the HTTP API (first-run compose -> post).

One place decides three things that must never disagree between the CLI, the
MCP server, and the web UI:

1. **Whether a post may run at all** — delegates to
   :class:`xpst.services.post_preflight.PostPreflightService`, the same
   side-effect-free planner the CLI/MCP surfaces use.
2. **What actually happened** — delegates the upload to
   :meth:`xpst.engine.CrossPostEngine.post_manual` (the real engine path; no
   bespoke uploader, no skipped state updates).
3. **How the outcome is reported** — :func:`serialize_post_attempt` builds a
   per-destination envelope where *every requested destination appears*, and a
   destination that produced no uploader or no result is reported as a failure
   with an explicit error.

Rule 3 is the fix for the shipped defect where a failed Instagram upload was
still logged as "100% complete": a missing/skipped destination is never
silently dropped from the response and never counted as success.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.platforms.base import UploadOutcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from xpst.config import XPSTConfig
    from xpst.engine import CrossPostResult

logger = logging.getLogger(__name__)

# Upper bound on a single manual post's media list (the CLI accepts a repeatable
# --video for carousels; the web UI sends one).
MAX_MEDIA_ITEMS = 10

# A destination that was requested but produced no upload result at all. This is
# the truthful text the UI renders instead of a fabricated success row.
NO_RESULT_ERROR = (
    "No uploader was available for {platform}: nothing was uploaded. "
    "Connect and enable the destination, then try again."
)


@dataclass(frozen=True)
class PostRequest:
    """A manual post request as received from an API caller."""

    media_paths: list[str] = field(default_factory=list)
    caption: str = ""
    platforms: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> PostRequest:
        """Build a request from a JSON payload, normalizing types.

        Unknown keys are ignored; platform names are lower-cased and deduped in
        request order so the response rows match what the caller asked for.
        """
        data = payload or {}
        raw_media = data.get("media_paths")
        if raw_media is None:
            single = data.get("media_path")
            raw_media = [single] if single else []
        if isinstance(raw_media, str):
            raw_media = [raw_media]
        media_paths = [str(item).strip() for item in (raw_media or []) if str(item).strip()]

        seen: dict[str, None] = {}
        for item in data.get("platforms") or []:
            name = str(item).strip().lower()
            if name:
                seen.setdefault(name, None)

        return cls(
            media_paths=media_paths[:MAX_MEDIA_ITEMS],
            caption=str(data.get("caption") or ""),
            platforms=list(seen),
        )


def _upload_to_dict(upload: Any) -> dict[str, Any]:
    """Serialize one upload result without upgrading a failure to success."""
    success = bool(getattr(upload, "success", False))
    outcome = getattr(upload, "outcome", None)
    outcome_value = outcome.value if isinstance(outcome, UploadOutcome) else (str(outcome) if outcome else None)
    return {
        "success": success,
        "published": success,
        "post_id": getattr(upload, "post_id", None),
        "post_url": getattr(upload, "post_url", None),
        "error": None if success else (getattr(upload, "error", None) or "Upload failed"),
        "outcome": outcome_value,
        "retryable": getattr(upload, "retryable", None),
    }


def serialize_post_attempt(
    *,
    requested: list[str],
    results: dict[str, Any] | None = None,
    video_id: str = "",
    caption: str = "",
    dry_run: bool = False,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Build the truthful result envelope for a posted (or dry-run) attempt.

    Args:
        requested: destination platforms the caller asked for, in order.
        results: ``{platform: UploadResult}`` from the engine, if the post ran.
        video_id: engine-assigned id (empty for a dry run).
        caption: the caption that was sent.
        dry_run: True when nothing was uploaded. A dry run reports per
            destination ``success: None`` ("not attempted") rather than a
            failure, because no upload was attempted at all.
        blockers: preflight blockers, when the attempt was refused.

    Returns:
        JSON-serializable dict. ``ok`` is True only when the post actually ran
        (or the dry-run plan is ready), at least one destination was requested,
        and every requested destination published.
    """
    results = results or {}
    destinations: list[dict[str, Any]] = []

    for platform in requested:
        upload = results.get(platform)
        if upload is None:
            if dry_run:
                # Nothing was attempted: report "not attempted", never failure.
                destinations.append(
                    {
                        "platform": platform,
                        "attempted": False,
                        "success": None,
                        "published": None,
                        "post_id": None,
                        "post_url": None,
                        "error": None,
                        "outcome": None,
                        "retryable": None,
                    }
                )
                continue
            destinations.append(
                {
                    "platform": platform,
                    "attempted": True,
                    "success": False,
                    "published": False,
                    "post_id": None,
                    "post_url": None,
                    "error": NO_RESULT_ERROR.format(platform=platform),
                    "outcome": None,
                    "retryable": None,
                }
            )
            continue
        row = _upload_to_dict(upload)
        row["platform"] = platform
        row["attempted"] = True
        destinations.append(row)

    # A platform the engine reported that the caller did not ask for is still
    # surfaced, so the response can never hide an unexpected upload.
    for platform, upload in results.items():
        if platform in requested:
            continue
        row = _upload_to_dict(upload)
        row["platform"] = platform
        row["attempted"] = True
        row["unrequested"] = True
        destinations.append(row)

    uploaded = [row for row in destinations if row["success"] is True]
    failed = [row for row in destinations if row["success"] is False]
    attempted = bool(requested)
    blocked = bool(blockers)

    # A dry run is "ok" when the plan is ready; a real post is "ok" only when
    # every requested destination actually published.
    ok = (attempted and not blocked) if dry_run else (attempted and not blocked and not failed and bool(uploaded))

    return {
        "ok": ok,
        "dry_run": dry_run,
        "uploaded": bool(uploaded) and not dry_run,
        "video_id": video_id,
        "caption": caption,
        "requested": list(requested),
        "destinations": destinations,
        "uploaded_count": len(uploaded),
        "failed_count": len(failed),
        "blockers": list(blockers or []),
        # ``all_success`` keeps the engine vocabulary for CLI/MCP parity; it is
        # never True for a dry run or a refused attempt.
        "all_success": ok and not dry_run,
        "partial_success": bool(uploaded) and bool(failed) and not dry_run,
    }


class PostService:
    """Plan, run, and report a manual post for one config directory."""

    def __init__(
        self,
        config: XPSTConfig,
        config_dir: str | None = None,
        engine_factory: Callable[[XPSTConfig], Any] | None = None,
    ) -> None:
        """Bind the service to a config.

        Args:
            config: loaded :class:`~xpst.config.XPSTConfig`.
            config_dir: overrides ``config.config_dir`` when set.
            engine_factory: test seam — a callable returning an engine-like
                object exposing ``post_manual`` / ``post_manual_carousel``.
                Production callers leave this ``None`` and get the real
                :class:`~xpst.engine.CrossPostEngine`.
        """
        self.config = config
        self.config_dir = str(config_dir or getattr(config, "config_dir", "") or "")
        self._engine_factory = engine_factory

    # ── Planning ────────────────────────────────────────────────────────

    def preflight(self, request: PostRequest) -> dict[str, Any]:
        """Run the canonical, side-effect-free preflight for a request."""
        from xpst.services.post_preflight import PostPlanRequest, PostPreflightService

        blockers: list[str] = []
        if not request.platforms:
            # An empty target list would produce an empty plan that reports
            # "ready", so the request-shape precondition is decided here.
            blockers.append("Choose at least one destination platform.")
        if not request.media_paths:
            blockers.append("Choose a video file before posting.")

        plan: dict[str, Any] | None = None
        try:
            plan = PostPreflightService(self.config).plan(
                PostPlanRequest(
                    media_paths=list(request.media_paths),
                    target_platforms=list(request.platforms),
                    base_caption=request.caption,
                )
            ).to_dict()
            blockers.extend(issue["message"] for issue in plan["hard_blockers"])
        except Exception as exc:  # noqa: BLE001 - report truthfully instead of raising
            logger.warning("Post preflight could not run: %s", exc)
            blockers.append(f"Preflight could not run: {str(exc)[:200]}")

        return {
            "ready": not blockers,
            "blockers": blockers,
            "plan": plan,
            "network_calls": False,
        }

    def dry_run(self, request: PostRequest) -> dict[str, Any]:
        """Return the plan envelope. Never instantiates the engine."""
        verdict = self.preflight(request)
        return serialize_post_attempt(
            requested=request.platforms,
            results={},
            caption=request.caption,
            dry_run=True,
            blockers=verdict["blockers"],
        ) | {"plan": verdict["plan"], "network_calls": False, "ready": verdict["ready"]}

    # ── Execution ───────────────────────────────────────────────────────

    def _build_engine(self) -> Any:
        if self._engine_factory is not None:
            return self._engine_factory(self.config)
        from xpst.engine import CrossPostEngine

        return CrossPostEngine(self.config)

    async def execute_async(self, request: PostRequest) -> dict[str, Any]:
        """Run the real upload path and return the truthful envelope."""
        verdict = self.preflight(request)
        if not verdict["ready"]:
            return serialize_post_attempt(
                requested=request.platforms,
                results={},
                caption=request.caption,
                dry_run=False,
                blockers=verdict["blockers"],
            ) | {"plan": verdict["plan"], "ready": False, "blocked": True}

        try:
            engine = self._build_engine()
        except Exception as exc:  # noqa: BLE001 - a dead engine is a failed post
            logger.error("Could not start the posting engine: %s", exc)
            return serialize_post_attempt(
                requested=request.platforms,
                results={},
                caption=request.caption,
                dry_run=False,
                blockers=[f"Posting engine unavailable: {str(exc)[:200]}"],
            ) | {"ready": True, "blocked": True}

        paths = [Path(item).expanduser() for item in request.media_paths]
        try:
            if len(paths) > 1:
                result: CrossPostResult = await engine.post_manual_carousel(
                    paths, request.caption, request.platforms
                )
            else:
                result = await engine.post_manual(paths[0], request.caption, request.platforms)
        except Exception as exc:  # noqa: BLE001 - the caller must see the failure
            logger.error("Manual post failed before producing results: %s", exc)
            return serialize_post_attempt(
                requested=request.platforms,
                results={},
                caption=request.caption,
                dry_run=False,
                blockers=[f"Post failed: {str(exc)[:200]}"],
            ) | {"ready": True, "blocked": False}

        envelope = serialize_post_attempt(
            requested=request.platforms,
            results=dict(getattr(result, "results", {}) or {}),
            video_id=str(getattr(result, "video_id", "") or ""),
            caption=str(getattr(result, "caption", request.caption) or ""),
            dry_run=False,
        )
        envelope["ready"] = True
        envelope["blocked"] = False
        return envelope

    def execute(self, request: PostRequest) -> dict[str, Any]:
        """Synchronous wrapper around :meth:`execute_async`.

        FastAPI runs sync ``def`` endpoints in a worker thread, where
        ``asyncio.run`` is safe. When a caller already owns a running loop the
        work is pushed to a private thread with its own loop rather than
        failing with "asyncio.run() cannot be called from a running event loop".
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.execute_async(request))

        box: dict[str, Any] = {}

        def _run() -> None:
            box["value"] = asyncio.run(self.execute_async(request))

        thread = threading.Thread(target=_run, name="xpst-api-post", daemon=True)
        thread.start()
        thread.join()
        return box["value"]
