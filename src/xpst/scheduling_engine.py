"""In-app scheduling engine: run due scheduled posts.

This is the engine-side half of in-app scheduling (the UI half lives in the
desktop/web scheduler view). It owns one job: take what
:class:`xpst.schedule_manager.ScheduleManager` has persisted and publish it at
the right time.

Behaviour:

- :meth:`SchedulingEngine.run_due` runs exactly one pass: claim the due entries
  atomically (``pending`` -> ``processing`` under the cross-process file lock,
  so cron and a running app can never double-post), post each one, and record
  the verified per-platform result.
- :meth:`SchedulingEngine.start` fires a pass immediately (engine start) and
  then keeps firing on an interval while the process lives, so a job added
  after startup is picked up without a restart.
- :meth:`SchedulingEngine.stop` stops the loop promptly (the wait is
  interruptible) and joins the worker.
- A cancelled entry never fires: the pass checks the abort signal before
  touching the network, and again after a post returns, because a cancel that
  lands while a plan is in flight wins — nothing is recorded as a completed
  plan for an entry the user cancelled.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.schedule_manager import ScheduleManager, stored_per_platform_captions
from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from xpst.config import XPSTConfig

logger = get_logger(__name__)

# Used when no config interval is available. The config's schedule interval
# (15 minutes by default) is the normal source.
DEFAULT_DUE_INTERVAL_SECONDS = 900.0

# A pass may not be run by a tighter loop than this; guards against a
# misconfigured interval turning the daemon into a busy loop.
MIN_DUE_INTERVAL_SECONDS = 0.01

_COUNT_KEYS: tuple[str, ...] = ("due", "posted", "failed", "aborted", "deferred")


def _config_interval(config: XPSTConfig | None) -> float:
    """Read the configured scheduler interval, falling back to the default."""
    raw = getattr(getattr(config, "schedule", None), "check_interval", None)
    if raw is None:
        return DEFAULT_DUE_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DUE_INTERVAL_SECONDS
    if value <= 0:
        return DEFAULT_DUE_INTERVAL_SECONDS
    return max(MIN_DUE_INTERVAL_SECONDS, value)


class SchedulingEngine:
    """Runs due scheduled posts on demand and on an interval.

    Args:
        engine: The cross-posting engine (only ``post_manual`` is used).
        config: Optional XPSTConfig — supplies the default interval and the
            config directory.
        manager: Optional pre-built ScheduleManager (tests inject one; the
            default is built for ``config_dir``).
        config_dir: Config directory for the schedule store.
        interval: Seconds between automatic passes (default: the configured
            scheduler interval).
        tz: Local zone the manager should display/interpret times in.
    """

    def __init__(
        self,
        engine: Any,
        *,
        config: XPSTConfig | None = None,
        manager: ScheduleManager | None = None,
        config_dir: str | Path | None = None,
        interval: float | None = None,
        tz: Any = None,
    ) -> None:
        self.engine = engine
        self.config = config
        resolved_dir = config_dir or getattr(config, "config_dir", None) or "~/.xpst"
        self.manager = manager or ScheduleManager(str(resolved_dir), tz=tz)
        raw_interval = interval if interval is not None else _config_interval(config)
        self.interval = max(MIN_DUE_INTERVAL_SECONDS, float(raw_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # One pass at a time per process: a long post must not let the next
        # tick start a second pass over the same store.
        self._pass_lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True while the interval loop is alive and not stopping."""
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        """Run a due pass now, then one per interval until :meth:`stop`."""
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="xpst-run-due", daemon=True
        )
        self._thread.start()
        logger.info("Scheduling engine started (interval=%.2fs)", self.interval)

    def stop(self, timeout: float | None = 5.0) -> None:
        """Stop the interval loop and join the worker (idempotent)."""
        self._stop.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)
        self._thread = None
        logger.info("Scheduling engine stopped")

    def _loop(self) -> None:
        """Interval loop body: pass immediately, then every ``interval``."""
        while not self._stop.is_set():
            try:
                counts = self.run_due()
                if counts["due"]:
                    logger.info(
                        "scheduling: due=%d posted=%d failed=%d aborted=%d",
                        counts["due"], counts["posted"], counts["failed"], counts["aborted"],
                    )
            except Exception as exc:  # noqa: BLE001 - never kill the daemon
                logger.error("scheduling: run-due pass raised %s", exc)
            if self._stop.wait(self.interval):
                break

    # ── the pass ──────────────────────────────────────────────────────

    def run_due(self, *, dry_run: bool = False) -> dict[str, int]:
        """Publish every due entry once.

        Args:
            dry_run: Report what is due without claiming or posting anything.

        Returns:
            Per-status counts: ``due``, ``posted``, ``failed``, ``aborted``.
        """
        counts = dict.fromkeys(_COUNT_KEYS, 0)

        if dry_run:
            counts["due"] = len(self.manager.get_due())
            return counts

        with self._pass_lock:
            due = self.manager.claim_due()
            if not due:
                return counts
            counts["due"] = len(due)

            for entry in due:
                entry_id = str(entry.get("id") or "")
                # A plan cancelled before it reached the network is aborted
                # here: no upload, no completion, no retry noise.
                if self.manager.is_aborted(entry_id):
                    counts["aborted"] += 1
                    logger.info("scheduling: %s was cancelled before posting", entry_id)
                    continue

                video_path = Path(str(entry.get("video_path") or ""))
                if not video_path.exists():
                    counts["failed"] += 1
                    self._record(
                        entry_id,
                        success=False,
                        error=f"File not found: {video_path}",
                    )
                    logger.warning(
                        "scheduling: %s skipped — file missing: %s", entry_id, video_path
                    )
                    continue

                caption = entry.get("caption") or ""
                platforms = entry.get("platforms") or None
                # Per-destination copy persisted at add time (same semantics
                # as `xpst post --caption-for`); {} = shared caption everywhere.
                per_platform_captions = stored_per_platform_captions(entry)
                # Route by the modality stored at add time (D1's schedule
                # twin: an entry added for an IMAGE must fire through the
                # image route, not the video encoder). Entries predating the
                # field — and entries naming a file that is actually an image
                # — stay on the video path only if the contract agrees.
                entry_route = str(entry.get("content_type") or "").strip().lower()
                entry_media = [
                    Path(p) for p in (entry.get("media_paths") or []) if str(p).strip()
                ]
                try:
                    if entry_route == "carousel" and len(entry_media) >= 2:
                        result = asyncio.run(
                            self.engine.post_manual_carousel(
                                entry_media,
                                caption,
                                platforms,
                                per_platform_captions or None,
                            )
                        )
                    elif entry_route == "image":
                        result = asyncio.run(
                            self.engine.post_manual_image(
                                video_path,
                                caption,
                                platforms,
                            )
                        )
                    else:
                        result = asyncio.run(
                            self.engine.post_manual(
                                video_path,
                                caption,
                                platforms,
                                per_platform_captions=per_platform_captions or None,
                            )
                        )
                    success = bool(getattr(result, "all_success", False))
                    error_msg = None
                    if not success:
                        error_msg = "; ".join(
                            f"{platform}: {upload.error}"
                            for platform, upload in getattr(result, "results", {}).items()
                            if not upload.success
                        )
                    post_results = {
                        platform: upload.to_dict()
                        for platform, upload in getattr(result, "results", {}).items()
                    }
                    # G11 on the fire path (D2): when EVERY destination came
                    # back deferred (anti-bot window), the entry is not a
                    # failure — it goes back to pending until the window
                    # opens. Marking it failed buried a healthy queue entry in
                    # the DLQ and told the user their post had failed.
                    rows = getattr(result, "results", {})
                    deferred_rows = [
                        upload for upload in rows.values()
                        if not upload.success
                        and getattr(upload, "metadata", None)
                        and (upload.metadata or {}).get("deferred")
                    ]
                    if not success and rows and len(deferred_rows) == len(rows):
                        resume_iso = next(
                            (
                                (upload.metadata or {}).get("resume_after")
                                for upload in deferred_rows
                                if (upload.metadata or {}).get("resume_after")
                            ),
                            None,
                        )
                        resume_at = None
                        if resume_iso:
                            try:
                                resume_at = datetime.fromisoformat(str(resume_iso))
                            except ValueError:
                                resume_at = None
                        counts["deferred"] = counts.get("deferred", 0) + 1
                        try:
                            self.manager.requeue(
                                entry_id,
                                next_time=resume_at,
                                error="deferred: outside posting window",
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.error("scheduling: requeue of %s failed: %s", entry_id, exc)
                        logger.info(
                            "scheduling: %s deferred (outside posting window), requeued",
                            entry_id,
                        )
                        continue
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    counts["failed"] += 1
                    self._record(entry_id, success=False, error=str(exc))
                    logger.error("scheduling: %s raised %s", entry_id, exc)
                    continue

                # A cancel that landed while the post was in flight wins: the
                # entry is already gone from the store, so recording a
                # completion for it would resurrect a plan the user cancelled.
                if self.manager.is_aborted(entry_id):
                    counts["aborted"] += 1
                    logger.info(
                        "scheduling: %s cancelled in flight — result discarded", entry_id
                    )
                    continue

                self._record(
                    entry_id,
                    success=success,
                    error=error_msg,
                    post_results=post_results,
                )
                if success:
                    counts["posted"] += 1
                    logger.info("scheduling: %s published", entry_id)
                else:
                    counts["failed"] += 1
                    logger.warning("scheduling: %s failed: %s", entry_id, error_msg)

            return counts

    def _record(
        self,
        entry_id: str,
        *,
        success: bool,
        error: str | None = None,
        post_results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Persist a result without letting a store error kill the loop."""
        if not entry_id:
            return
        try:
            self.manager.mark_complete(
                entry_id, success=success, error=error, post_results=post_results
            )
        except Exception as exc:  # noqa: BLE001 - a store hiccup is not fatal
            logger.error("scheduling: could not record %s: %s", entry_id, exc)


def run_due(
    engine: Any,
    config: XPSTConfig | None = None,
    *,
    manager: ScheduleManager | None = None,
    config_dir: str | Path | None = None,
    dry_run: bool = False,
    tz: Any = None,
) -> dict[str, int]:
    """Run a single due-post pass with a throwaway engine (scripting/CLI use).

    Returns the same per-status counts as :meth:`SchedulingEngine.run_due`.
    """
    scheduler = SchedulingEngine(
        engine, config=config, manager=manager, config_dir=config_dir, tz=tz
    )
    return scheduler.run_due(dry_run=dry_run)


__all__ = [
    "DEFAULT_DUE_INTERVAL_SECONDS",
    "MIN_DUE_INTERVAL_SECONDS",
    "SchedulingEngine",
    "run_due",
]
