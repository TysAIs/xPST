from __future__ import annotations

"""
Schedule Manager for xPST

Manages scheduled posts that should be published at a specific time.
Stores entries in ~/.xpst/schedule.json.

Each entry:
    {
        "id": "<uuid>",
        "video_path": "/path/to/video.mp4",
        "caption": "Post caption",
        "platforms": ["youtube", "instagram"],
        "scheduled_time": "2026-06-08T10:00:00",
        "status": "pending" | "completed" | "failed",
        "created_at": "2026-06-07T12:00:00",
        "completed_at": null,
        "error": null
    }
"""

import calendar
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX advisory locking
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt  # Windows file locking
except ImportError:
    msvcrt = None

from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Hard cap on caption size. A 10 MB caption would be persisted to
# schedule.json on every save and passed to every platform uploader;
# reject it at add() time with an actionable error instead.
MAX_CAPTION_LENGTH = 100_000


def _clamp_day(day: int, year: int, month: int) -> int:
    """Clamp a day-of-month to the maximum valid day for the given month/year.

    Args:
        day: Desired day (e.g. 31).
        year: Full year (e.g. 2026).
        month: Month number 1-12.

    Returns:
        The clamped day that is valid for the given month/year.
    """
    max_day = calendar.monthrange(year, month)[1]
    return min(day, max_day)


class ScheduleManager:
    """Manages scheduled posts for xPST.

    Stores scheduled posts in ~/.xpst/schedule.json and provides
    methods to add, list, remove, and process due posts.
    """

    def __init__(self, config_dir: str = "~/.xpst"):
        """Initialize the schedule manager.

        Args:
            config_dir: Path to the xPST config directory.
        """
        self.config_dir = Path(config_dir).expanduser()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.schedule_file = self.config_dir / "schedule.json"
        # Cross-process lock (cron + daemon may run concurrently) and
        # in-process lock (threaded callers, e.g. dashboard backend).
        self._lockfile = self.config_dir / ".schedule.lock"
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._load()

    # ── locking helpers ───────────────────────────────────────────────

    @contextmanager
    def _process_lock(self):
        """Acquire an exclusive advisory file lock shared by all processes
        (cron + daemon may run concurrently). fcntl on POSIX, msvcrt on
        Windows; degrades to in-process locking only if neither exists."""
        with open(self._lockfile, "a+") as f:
            locked = False
            try:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    locked = True
                elif msvcrt is not None:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
            except (AttributeError, OSError):  # pragma: no cover
                pass
            try:
                yield
            finally:
                if locked:
                    try:
                        if fcntl is not None:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        else:
                            f.seek(0)
                            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    except (AttributeError, OSError):  # pragma: no cover
                        pass

    @staticmethod
    def _normalize_to_naive_local(dt: datetime) -> datetime:
        """Convert a tz-aware datetime to naive local time.

        The schedule store compares against datetime.now() (naive local),
        so aware values must be normalized or comparisons raise TypeError.
        DST semantics: wall-clock time is preserved across the conversion.
        """
        if dt.tzinfo is None:
            return dt
        return dt.astimezone().replace(tzinfo=None)

    def _load(self) -> None:
        """Load schedule entries from disk.

        On corruption the broken file is quarantined (renamed with a
        .corrupt-<timestamp> suffix) so the user's data is preserved for
        manual recovery instead of being silently overwritten later.
        """
        if self.schedule_file.exists():
            try:
                with open(self.schedule_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    dropped = [e for e in data if not isinstance(e, dict)]
                    if dropped:
                        logger.error(
                            "Schedule file %s contained %d non-object entries; "
                            "they have been dropped (types: %s)",
                            self.schedule_file, len(dropped),
                            [type(e).__name__ for e in dropped],
                        )
                    self._entries = [e for e in data if isinstance(e, dict)]
                else:
                    logger.error(
                        "Schedule file %s is not a JSON array (got %s); "
                        "starting with an empty schedule",
                        self.schedule_file, type(data).__name__,
                    )
                    self._entries = []
            except (json.JSONDecodeError, OSError) as e:
                quarantine = self.schedule_file.with_name(
                    f"{self.schedule_file.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                )
                try:
                    os.replace(self.schedule_file, quarantine)
                    logger.error(
                        "Schedule file %s is corrupt (%s). It has been "
                        "quarantined to %s — pending scheduled posts in it "
                        "were NOT deleted; inspect the quarantined file to "
                        "recover them.",
                        self.schedule_file, e, quarantine,
                    )
                except OSError:
                    logger.error(
                        "Schedule file %s is corrupt (%s) and could not be "
                        "quarantined.", self.schedule_file, e,
                    )
                self._entries = []
        else:
            self._entries = []

    def _save(self) -> None:
        """Persist schedule entries to disk atomically."""
        import tempfile

        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp file then atomic rename (same pattern as state_store)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.schedule_file.parent, suffix=".tmp", prefix=".schedule_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, indent=2, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.schedule_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _reload_locked(self) -> None:
        """Re-read the schedule from disk. Caller must hold the process lock."""
        self._load()

    def add(
        self,
        video_path: str,
        caption: str,
        scheduled_time: datetime,
        platforms: list[str] | None = None,
        repeat_rule: str | None = None,
    ) -> dict[str, Any]:
        """Add a new scheduled post.

        Args:
            video_path: Path to the video file.
            caption: Post caption text (max 100,000 characters).
            scheduled_time: When to publish. Naive datetimes are taken as
                local wall-clock time; aware datetimes are converted to
                local wall-clock time.
            platforms: Target platforms (None = all enabled).
            repeat_rule: Repeat rule - 'daily', 'weekly', 'monthly', or None.

        Returns:
            The created schedule entry.

        Raises:
            ValueError: If repeat_rule is invalid, caption exceeds
                MAX_CAPTION_LENGTH, or scheduled_time is not a datetime.
        """
        valid_rules = (None, "daily", "weekly", "monthly")
        if repeat_rule not in valid_rules:
            raise ValueError(
                f"Invalid repeat_rule: {repeat_rule!r}. "
                f"Must be one of: None, 'daily', 'weekly', 'monthly'"
            )
        if not isinstance(scheduled_time, datetime):
            raise ValueError(
                f"scheduled_time must be a datetime, got {type(scheduled_time).__name__}. "
                "Parse strings with datetime.strptime or datetime.fromisoformat first."
            )
        caption = caption if isinstance(caption, str) else str(caption)
        if len(caption) > MAX_CAPTION_LENGTH:
            raise ValueError(
                f"Caption is {len(caption):,} characters; the maximum is "
                f"{MAX_CAPTION_LENGTH:,}. Shorten the caption before scheduling."
            )
        clean_platforms = [p.strip() for p in (platforms or []) if p and p.strip()]

        scheduled_time = self._normalize_to_naive_local(scheduled_time)

        entry: dict[str, Any] = {
            "id": str(uuid.uuid4())[:8],
            "video_path": str(video_path),
            "caption": caption,
            "platforms": clean_platforms,
            "scheduled_time": scheduled_time.isoformat(),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "error": None,
            "repeat_rule": repeat_rule,
        }
        with self._process_lock():
            with self._lock:
                # Reload under the process lock: other instances (threads
                # or OS processes) may have persisted entries since this
                # instance was constructed; appending blindly would drop
                # them on the next save.
                self._reload_locked()
                self._entries.append(entry)
                self._save()
        logger.info(f"Scheduled post {entry['id']} for {scheduled_time}")
        return entry

    def list(self) -> list[dict[str, Any]]:
        """List all scheduled posts, sorted by scheduled_time.

        Returns:
            List of schedule entries.
        """
        with self._lock:
            return sorted(self._entries, key=lambda e: e.get("scheduled_time", ""))

    def remove(self, entry_id: str) -> bool:
        """Remove a scheduled post by ID.

        Args:
            entry_id: The ID of the entry to remove.

        Returns:
            True if removed, False if not found.
        """
        with self._lock:
            original_count = len(self._entries)
            self._entries = [e for e in self._entries if e.get("id") != entry_id]
            removed = len(self._entries) < original_count
            if removed:
                self._save()
        if removed:
            logger.info(f"Removed scheduled post {entry_id}")
        return removed

    def get_due(self) -> list[dict[str, Any]]:
        """Get posts that are due for publishing.

        Returns entries where scheduled_time <= now and status == "pending".
        Timezone-aware stored times are normalized to local wall-clock time
        before comparison (legacy entries written by other tools).

        Note: this is a read-only view. Workers that post must use
        :meth:`claim_due` / :meth:`claim` to guarantee exactly-once
        processing under concurrent cron + daemon invocations.

        Returns:
            List of due schedule entries.
        """
        now = datetime.now()
        with self._lock:
            due = []
            for entry in self._entries:
                if entry.get("status") != "pending":
                    continue
                try:
                    scheduled = datetime.fromisoformat(entry["scheduled_time"])
                    scheduled = self._normalize_to_naive_local(scheduled)
                    if scheduled <= now:
                        due.append(entry)
                except (ValueError, KeyError, TypeError):
                    continue
            return due

    def claim_due(self) -> list[dict[str, Any]]:
        """Atomically claim all due entries for this worker.

        Transitions each due entry from ``pending`` to ``processing`` and
        persists the change under a cross-process file lock, so when cron
        and the serve daemon fire simultaneously only one of them posts.

        Returns:
            The list of entries claimed by this worker (now status
            ``processing``).
        """
        with self._process_lock():
            with self._lock:
                self._reload_locked()
                due = [e for e in self.get_due()]
                for entry in due:
                    entry["status"] = "processing"
                    entry["claimed_at"] = datetime.now().isoformat()
                if due:
                    self._save()
                return due

    def claim(self, entry_id: str) -> bool:
        """Atomically claim a single entry by ID.

        Re-reads the store from disk first, so it is safe across both
        threads and OS processes.

        Returns:
            True if this worker claimed the entry, False if another worker
            already claimed it, it is not pending, or it is not yet due.
        """
        with self._process_lock():
            with self._lock:
                self._reload_locked()
                now = datetime.now()
                for entry in self._entries:
                    if entry.get("id") != entry_id:
                        continue
                    if entry.get("status") != "pending":
                        return False
                    try:
                        scheduled = datetime.fromisoformat(entry["scheduled_time"])
                        scheduled = self._normalize_to_naive_local(scheduled)
                    except (ValueError, KeyError, TypeError):
                        return False
                    if scheduled > now:
                        return False
                    entry["status"] = "processing"
                    entry["claimed_at"] = now.isoformat()
                    self._save()
                    return True
                return False

    def mark_complete(self, entry_id: str, success: bool = True, error: str | None = None) -> None:
        """Mark a scheduled post as completed or failed.

        Args:
            entry_id: The ID of the entry.
            success: Whether the post succeeded.
            error: Error message if failed.
        """
        with self._lock:
            for entry in self._entries:
                if entry.get("id") == entry_id:
                    entry["status"] = "completed" if success else "failed"
                    entry["completed_at"] = datetime.now().isoformat()
                    if error:
                        entry["error"] = error
                    # Auto-create next occurrence for recurring entries
                    if success and entry.get("repeat_rule"):
                        self._create_next_occurrence(entry)
                    break
            self._save()

    def _create_next_occurrence(self, entry: dict[str, Any]) -> None:
        """Create the next occurrence of a recurring schedule entry.

        Args:
            entry: The completed schedule entry to base the next occurrence on.
        """
        repeat_rule = entry.get("repeat_rule")
        if not repeat_rule:
            return

        try:
            current_time = datetime.fromisoformat(entry["scheduled_time"])
        except (ValueError, KeyError):
            logger.warning("Cannot create next occurrence: invalid scheduled_time in entry %s", entry.get("id"))
            return

        if repeat_rule == "daily":
            next_time = current_time + timedelta(days=1)
        elif repeat_rule == "weekly":
            next_time = current_time + timedelta(weeks=1)
        elif repeat_rule == "monthly":
            # Advance by calendar month with day clamping
            next_month = current_time.month + 1
            next_year = current_time.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            clamped_day = _clamp_day(current_time.day, next_year, next_month)
            try:
                next_time = current_time.replace(year=next_year, month=next_month, day=clamped_day)
            except ValueError:
                # Fallback: should not happen with clamping, but be safe
                next_time = current_time + timedelta(days=30)
        else:
            return

        new_entry: dict[str, Any] = {
            "id": str(uuid.uuid4())[:8],
            "video_path": entry["video_path"],
            "caption": entry["caption"],
            "platforms": entry.get("platforms", []),
            "scheduled_time": next_time.isoformat(),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "error": None,
            "repeat_rule": repeat_rule,
        }
        self._entries.append(new_entry)
        logger.info(
            "Created next %s occurrence %s for %s",
            repeat_rule, new_entry["id"], next_time,
        )
