"""Canonical recovery operations shared by every xPST surface.

Three recovery operations used to live on one surface only, or to mean
different things per surface:

* cancelling a scheduled post (CLI ``schedule remove`` had no MCP equivalent),
* retrying one recorded failure (CLI ``failures retry`` had no MCP equivalent),
* deleting a post (CLI ``delete`` and MCP ``xpst_delete`` both delete on the
  platform via the engine's Phase-1.2 delete contract, then drop the local
  record — the MCP tool used to drop only the record and still report success).

An agent that believes it cancelled the right entry, retried the item it named,
or deleted a live post is a fabricated-success machine. So the business logic
lives here exactly once, and both the CLI and the MCP server render the verdict
from the same payload.

Every payload carries two honesty fields:

* ``scope`` — where the effect actually lands (``local_schedule_store``,
  ``platform_upload``, ...), never where a reader might assume it lands;
* ``attempted`` / ``cancelled`` / ``posted`` / ``platform_deleted`` — booleans
  that are only true when that specific thing really happened.

The plan functions are side-effect free and return the same verdict the
executing function will act on, so a dry run cannot drift from the real path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from xpst.config import XPSTConfig
    from xpst.engine import CrossPostEngine
    from xpst.state import StateManager

logger = get_logger(__name__)

# Scope tokens. These are part of the agent-facing contract — changing one is a
# breaking change for any client that branches on it.
SCHEDULE_CANCEL_SCOPE = "local_schedule_store"
FAILURE_RETRY_SCOPE = "platform_upload"
DELETE_RECORD_SCOPE = "local_state_only"
# xpst_delete performs a REAL platform takedown (engine.delete_post) AND removes
# the local record, so its scope names both effects. DELETE_RECORD_SCOPE remains
# for callers that still branch on the legacy local-only semantics.
PLATFORM_DELETE_SCOPE = "platform_and_local_state"

# Error codes shared by both surfaces (the CLI already ships these strings).
SCHEDULE_ENTRY_NOT_FOUND = "POST_NOT_FOUND"
RETRY_VIDEO_NOT_FOUND = "VIDEO_NOT_FOUND"
RETRY_NO_RECORDED_FAILURE = "NO_RECORDED_FAILURE"
RETRY_NO_LOCAL_FILE = "NO_LOCAL_FILE"
RETRY_FAILED = "RETRY_FAILED"

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


def _schedule_manager(config_dir: str | Path | None) -> Any:
    """Build a ScheduleManager for ``config_dir`` (default ``~/.xpst``)."""
    from xpst.schedule_manager import ScheduleManager

    if config_dir is None:
        return ScheduleManager()
    return ScheduleManager(str(config_dir))


@dataclass(frozen=True)
class ScheduleCancelPlan:
    """Side-effect-free verdict for cancelling one scheduled post."""

    entry_id: str
    entry: dict[str, Any] | None

    @property
    def found(self) -> bool:
        return self.entry is not None

    def to_payload(self, *, dry_run: bool, cancelled: bool) -> dict[str, Any]:
        """Canonical payload. ``cancelled`` is only ever true after a real removal."""
        payload: dict[str, Any] = {
            "ok": self.found,
            "operation": "schedule_cancel",
            "scope": SCHEDULE_CANCEL_SCOPE,
            "dry_run": dry_run,
            "entry_id": self.entry_id,
            "found": self.found,
            "cancelled": bool(cancelled),
            "entry": self.entry,
            "error": None,
        }
        if not self.found:
            payload["error"] = {
                "code": SCHEDULE_ENTRY_NOT_FOUND,
                "message": f"Scheduled post not found: {self.entry_id}",
            }
        return payload


def plan_schedule_cancel(
    config_dir: str | Path | None,
    entry_id: str,
) -> ScheduleCancelPlan:
    """Look up the entry a cancel would remove — no writes, no side effects."""
    manager = _schedule_manager(config_dir)
    entry = next((e for e in manager.list() if e.get("id") == entry_id), None)
    return ScheduleCancelPlan(entry_id=entry_id, entry=entry)


def cancel_scheduled_post(
    config_dir: str | Path | None,
    entry_id: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Cancel (remove) a scheduled post from the local schedule store.

    Semantics are identical to ``xpst schedule remove``: an unknown id is a
    failure (``POST_NOT_FOUND``), never a silent no-op success. The operation
    only touches the local schedule store — it never un-posts live content.
    """
    manager = _schedule_manager(config_dir)
    entry = next((e for e in manager.list() if e.get("id") == entry_id), None)
    plan = ScheduleCancelPlan(entry_id=entry_id, entry=entry)

    if dry_run:
        return plan.to_payload(dry_run=True, cancelled=False)
    if not plan.found:
        return plan.to_payload(dry_run=False, cancelled=False)

    if not manager.remove(entry_id):
        # Lost a race with another process between the read and the write:
        # report the truth (nothing was cancelled) instead of a success.
        logger.info("schedule cancel: %s vanished before removal", entry_id)
        return ScheduleCancelPlan(entry_id=entry_id, entry=None).to_payload(
            dry_run=False, cancelled=False
        )
    return plan.to_payload(dry_run=False, cancelled=True)


@dataclass(frozen=True)
class FailureRetryPlan:
    """Side-effect-free verdict for retrying one recorded upload failure."""

    video_id: str
    platform: str
    failure: dict[str, Any] | None = None
    media_path: Path | None = None
    caption: str = ""
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None

    def to_payload(
        self,
        *,
        dry_run: bool = False,
        attempted: bool = False,
        posted: bool = False,
        post_url: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Canonical payload. ``attempted``/``posted`` only true when they happened."""
        code = error_code or self.error_code
        message = error_message or self.error_message
        return {
            "ok": code is None,
            "operation": "failure_retry",
            "scope": FAILURE_RETRY_SCOPE,
            "dry_run": dry_run,
            "video_id": self.video_id,
            "platform": self.platform,
            "failure": self.failure,
            "media_path": str(self.media_path) if self.media_path is not None else None,
            "attempted": bool(attempted),
            "posted": bool(posted),
            "post_url": post_url,
            "error": None if code is None else {"code": code, "message": message},
        }


def plan_failure_retry(
    config: XPSTConfig,
    state: StateManager,
    video_id: str,
    platform: str,
) -> FailureRetryPlan:
    """Resolve the exact target of a targeted retry without uploading.

    The verdict (ok / error code) is what the executing path will act on, so
    callers can report it as a dry run without a second contract to drift.
    """
    video = state.get_video(video_id)
    if video is None:
        return FailureRetryPlan(
            video_id=video_id,
            platform=platform,
            error_code=RETRY_VIDEO_NOT_FOUND,
            error_message=f"Unknown video id: {video_id}",
        )

    failure = (video.get("errors") or {}).get(platform)
    if not failure:
        return FailureRetryPlan(
            video_id=video_id,
            platform=platform,
            error_code=RETRY_NO_RECORDED_FAILURE,
            error_message=f"{video_id} has no recorded failure on {platform}.",
        )

    download_dir = Path(config.video.download_dir).expanduser()
    candidates = sorted(download_dir.glob(f"*{video_id.split(':')[-1]}*"))
    candidates = [p for p in candidates if p.suffix.lower() in _VIDEO_SUFFIXES]
    if not candidates:
        return FailureRetryPlan(
            video_id=video_id,
            platform=platform,
            failure=failure if isinstance(failure, dict) else {"error": str(failure)},
            error_code=RETRY_NO_LOCAL_FILE,
            error_message=(
                f"No local file for {video_id} in {download_dir}. "
                "Re-run the source cycle (`xpst run`) or post manually with `xpst post`."
            ),
        )

    return FailureRetryPlan(
        video_id=video_id,
        platform=platform,
        failure=failure if isinstance(failure, dict) else {"error": str(failure)},
        media_path=candidates[0],
        caption=video.get("caption") or "",
    )


async def execute_failure_retry(
    engine: CrossPostEngine,
    plan: FailureRetryPlan,
) -> dict[str, Any]:
    """Perform a planned targeted retry — one real upload to ``plan.platform``.

    ``ok`` is true only when the destination actually accepted the post;
    ``attempted`` records that a platform call was made at all.
    """
    if not plan.ok:
        # Never "retry" something the plan already refused — that is exactly the
        # fabricated success this service exists to prevent.
        return plan.to_payload()

    assert plan.media_path is not None  # guaranteed by plan.ok
    state = engine.state
    result = await engine.post_manual(plan.media_path, plan.caption, [plan.platform])
    upload = result.results.get(plan.platform)

    if upload is not None and upload.success:
        try:
            state.clear_dead_letter_queue(plan.video_id)
            state.save()
        except Exception:  # noqa: BLE001 - the post landed; DLQ cleanup is bookkeeping
            logger.warning("retry succeeded but DLQ cleanup failed for %s", plan.video_id)
        return plan.to_payload(
            attempted=True,
            posted=True,
            post_url=upload.post_url or None,
        )

    error = upload.error if upload is not None else "no result"
    return plan.to_payload(
        attempted=True,
        posted=False,
        error_code=RETRY_FAILED,
        error_message=str(error or "retry failed"),
    )


async def retry_failed_post(
    engine: CrossPostEngine,
    video_id: str,
    platform: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Plan and retry one recorded failure — the one-shot entry point.

    A real upload to ``platform`` only. Every failure mode returns a distinct
    error code (``VIDEO_NOT_FOUND`` / ``NO_RECORDED_FAILURE`` / ``NO_LOCAL_FILE``
    / ``RETRY_FAILED``) and ``posted: false``; ``ok`` is true only when the
    destination actually accepted the post.
    """
    plan = plan_failure_retry(engine.config, engine.state, video_id, platform)

    if dry_run:
        return plan.to_payload(dry_run=True)
    if not plan.ok:
        logger.info(
            "failure retry(%s, %s) not attempted: %s", video_id, platform, plan.error_code
        )
        return plan.to_payload()
    return await execute_failure_retry(engine, plan)


__all__ = [
    "DELETE_RECORD_SCOPE",
    "FAILURE_RETRY_SCOPE",
    "PLATFORM_DELETE_SCOPE",
    "SCHEDULE_CANCEL_SCOPE",
    "FailureRetryPlan",
    "ScheduleCancelPlan",
    "cancel_scheduled_post",
    "execute_failure_retry",
    "plan_failure_retry",
    "plan_schedule_cancel",
    "retry_failed_post",
]
