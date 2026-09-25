from __future__ import annotations

"""
Schedule Manager for xPST

Manages scheduled posts that should be published at a specific time.
Stores entries in ~/.xpst/schedule.json.

**Time contract.** Times are entered by the user in *local* time, persisted as
an explicit *UTC* instant (``scheduled_time`` is ISO-8601 with a ``+00:00``
offset), and rendered back in local time (:meth:`ScheduleManager.local_time`,
and the derived ``scheduled_time_local`` key that :meth:`ScheduleManager.list`
adds for every surface).  Due comparison is instant-based, so a job scheduled
across a DST transition fires at the wall-clock time the user picked — the
stored offset, not the machine's offset at check time, decides.
Legacy entries written by older builds (naive local strings) are still read as
local wall-clock time.

Each entry:
    {
        "id": "<uuid>",
        "video_path": "/path/to/video.mp4",
        "caption": "Post caption",
        "platforms": ["youtube", "instagram"],
        "scheduled_time": "2026-06-08T16:00:00+00:00",   # UTC instant
        "timezone": "America/Denver",                     # zone used to enter it
        "status": "pending" | "processing" | "completed" | "failed",
        "created_at": "2026-06-07T12:00:00",
        "completed_at": null,
        "error": null,
        "post_results": {}
    }
"""

import calendar
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from xpst.utils.atomic import replace_with_retry

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
# reject it at add() time with an actionable error instead. The limit is
# shared with the CLI (CAPTION_TOO_LONG): captions are re-read into memory
# by every scheduler tick, so bound them well above any platform limit but
# far below unbounded payload sizes (e.g. accidental 1 MB shell arguments).
MAX_CAPTION_LENGTH = 100_000

_UTC = timezone.utc

# Sentinel for edit(): distinguishes "leave this field alone" from "set it to
# None" (which is meaningful for repeat_rule).
_UNSET: Any = object()

# In-process abort signals, keyed by (config_dir, entry_id) so every
# ScheduleManager instance in the process — the app, the dashboard API, a
# worker thread — observes the same cancellation. Cross-process cancellation is
# covered by the store itself: cancel() removes the entry, so a worker in
# another process sees it vanish (see is_aborted()).
_ABORT_LOCK = threading.Lock()
_ABORT_EVENTS: dict[tuple[str, str], threading.Event] = {}


def local_zone() -> tzinfo:
    """Return the machine's local timezone as a DST-aware zone.

    Prefers the ``TZ`` environment variable, then the ``/etc/localtime``
    symlink (macOS/Linux), and finally falls back to the current fixed offset.
    A DST-aware zone matters: a naive local wall-clock time must be converted
    to UTC with the offset that applies *on that date*, not today's.
    """
    name = os.environ.get("TZ", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - unknown TZ value, keep looking
            pass
    try:
        parts = Path("/etc/localtime").resolve().parts
        if "zoneinfo" in parts:
            zone = "/".join(parts[parts.index("zoneinfo") + 1:])
            return ZoneInfo(zone)
    except Exception:  # noqa: BLE001 - no usable system zone
        pass
    fixed = datetime.now().astimezone().tzinfo
    return fixed or _UTC


def zone_name(tz: tzinfo | None) -> str | None:
    """Return the IANA name of ``tz`` when it has one (ZoneInfo), else None."""
    key = getattr(tz, "key", None)
    return key if isinstance(key, str) and key else None


def to_utc(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Interpret ``dt`` as an instant and return it in UTC.

    Naive values are local wall-clock time in ``tz`` (default: the machine's
    local zone); aware values keep their instant. DST is handled by the zone,
    so 12:00 on 2026-10-31 (MDT) and 12:00 on 2026-11-01 (MST) map to instants
    one hour further apart than their wall-clock difference.
    """
    zone = tz or local_zone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(_UTC)


def to_local(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Return ``dt`` in local time (``tz`` defaulting to the machine's zone)."""
    zone = tz or local_zone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def _abort_key(config_dir: str | Path, entry_id: str) -> tuple[str, str]:
    return (str(Path(config_dir).expanduser()), str(entry_id))


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
    methods to add, edit, list, cancel (remove), and process due posts.
    """

    def __init__(self, config_dir: str = "~/.xpst", *, tz: tzinfo | None = None):
        """Initialize the schedule manager.

        Args:
            config_dir: Path to the xPST config directory.
            tz: Local zone used to interpret naive times and to render local
                displays. Defaults to the machine's local zone.
        """
        self.config_dir = Path(config_dir).expanduser()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.schedule_file = self.config_dir / "schedule.json"
        # Cross-process lock (cron + daemon may run concurrently) and
        # in-process lock (threaded callers, e.g. dashboard backend).
        self._lockfile = self.config_dir / ".schedule.lock"
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._tz: tzinfo = tz or local_zone()
        self._load()

    # ── locking helpers ───────────────────────────────────────────────

    @contextmanager
    def _process_lock(self):
        """Acquire an exclusive advisory file lock shared by all processes
        (cron + daemon may run concurrently). fcntl on POSIX, msvcrt on
        Windows; degrades to in-process locking only if neither exists.

        msvcrt.LK_LOCK retries the lock for ~10 seconds and then raises
        OSError; that must NOT be swallowed — proceeding unlocked lets two
        writers load-append-save concurrently and the last save silently
        drops the other writer's entry (observed on Windows CI: 10 threaded
        adds → 9 entries). Retry until the bounded deadline, then fail
        loudly instead of corrupting the schedule.
        """
        with open(self._lockfile, "a+") as f:
            locked = False
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                locked = True
            elif msvcrt is not None:
                deadline = time.monotonic() + 30.0
                while True:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                        locked = True
                        break
                    except (AttributeError, OSError):
                        if time.monotonic() >= deadline:
                            raise  # fail loudly: an unlocked write loses data
                        time.sleep(0.05)
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

    # ── timezone helpers ──────────────────────────────────────────────

    @staticmethod
    def _normalize_to_naive_local(dt: datetime) -> datetime:
        """Convert a tz-aware datetime to naive local time.

        Legacy helper for entries written before UTC storage. DST semantics:
        wall-clock time is preserved across the conversion.
        """
        if dt.tzinfo is None:
            return dt
        return dt.astimezone().replace(tzinfo=None)

    def _zone_for_entry(self, entry: dict[str, Any]) -> tzinfo:
        """The zone an entry was entered in (falls back to the manager zone)."""
        name = entry.get("timezone") if isinstance(entry, dict) else None
        if isinstance(name, str) and name:
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001 - tzdata may not know the name
                pass
        return self._tz

    def _entry_instant(self, raw: Any) -> datetime | None:
        """Parse a stored ``scheduled_time`` into a UTC instant.

        Naive values (legacy entries) are read as local wall-clock time.
        Returns None for anything unparseable so a corrupt entry can never
        crash a scheduler tick.
        """
        try:
            dt = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None
        return to_utc(dt, self._tz)

    def _now_utc(self, now: datetime | None = None) -> datetime:
        """Resolve a comparison clock to UTC (naive input = local wall clock)."""
        if now is None:
            return datetime.now(_UTC)
        if not isinstance(now, datetime):
            raise TypeError(f"now must be a datetime, got {type(now).__name__}")
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        return now.astimezone(_UTC)

    def local_time(self, entry: Any, tz: tzinfo | None = None) -> datetime:
        """Return an entry's scheduled instant in local time (display helper).

        Args:
            entry: A schedule entry dict (or a raw stored/ISO time string).
            tz: Override the display zone (default: the entry's own zone, so a
                job entered in another timezone still shows the time the user
                typed).

        Returns:
            A timezone-aware datetime in local time.
        """
        if isinstance(entry, dict):
            raw = entry.get("scheduled_time")
            zone = tz or self._zone_for_entry(entry)
        else:
            raw, zone = entry, tz or self._tz
        if not isinstance(raw, str):
            raise ValueError("schedule entry has no scheduled_time to display")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"invalid scheduled_time: {raw!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        return dt.astimezone(zone)

    def _with_local_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Copy of ``entry`` plus its local rendering for display surfaces."""
        view = dict(entry)
        try:
            view["scheduled_time_local"] = self.local_time(entry).isoformat()
        except (ValueError, TypeError):
            # Corrupt/legacy entry: expose it verbatim rather than failing the
            # whole listing (the dashboard, CLI and MCP all share list()).
            view["scheduled_time_local"] = None
        return view

    # ── abort / cancel signalling ─────────────────────────────────────

    def abort_event(self, entry_id: str) -> threading.Event:
        """Return the (process-wide) abort signal for one schedule entry."""
        key = _abort_key(self.config_dir, entry_id)
        with _ABORT_LOCK:
            return _ABORT_EVENTS.setdefault(key, threading.Event())

    def _clear_abort(self, entry_id: str) -> None:
        """Clear any pending abort signal for ``entry_id`` (re-armed job)."""
        with _ABORT_LOCK:
            event = _ABORT_EVENTS.get(_abort_key(self.config_dir, entry_id))
            if event is not None:
                event.clear()

    def is_aborted(self, entry_id: str) -> bool:
        """True when a claimed plan must not proceed.

        Two independent reasons count as abort:

        - an in-process cancel signalled this id (instant), or
        - the entry is no longer in the store (a cancel from another process,
          which only has the file to talk through).

        Call this only for an id that was claimed: an id that never existed is
        indistinguishable from a cancelled one by design.
        """
        if self.abort_event(entry_id).is_set():
            return True
        with self._process_lock():
            with self._lock:
                self._reload_locked()
                return all(e.get("id") != entry_id for e in self._entries)

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
                        "quarantined.",
                        self.schedule_file, e,
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
            # Windows can briefly lock the destination (WinError 5) when another
            # process is reading or replacing the same file; the claim path races
            # on purpose, so retry instead of crashing the loser.
            replace_with_retry(tmp_path, self.schedule_file, sleep=time.sleep)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _reload_locked(self) -> None:
        """Re-read the schedule from disk. Caller must hold the process lock."""
        self._load()

    # ── create ────────────────────────────────────────────────────────

    def add(
        self,
        video_path: str,
        caption: str,
        scheduled_time: datetime,
        platforms: list[str] | None = None,
        repeat_rule: str | None = None,
        *,
        tz: tzinfo | None = None,
    ) -> dict[str, Any]:
        """Add a new scheduled post.

        Args:
            video_path: Path to the video file.
            caption: Post caption text (max 100,000 characters).
            scheduled_time: When to publish. Naive datetimes are the local
                wall-clock time the user typed (in ``tz``); aware datetimes
                keep their instant. Either way it is stored as UTC.
            platforms: Target platforms (None = all enabled).
            repeat_rule: Repeat rule - 'daily', 'weekly', 'monthly', or None.
            tz: Zone that naive ``scheduled_time`` values are entered in.

        Returns:
            The created schedule entry (with its local rendering).

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

        zone = tz or self._tz
        scheduled_utc = to_utc(scheduled_time, zone)

        entry_id = str(uuid.uuid4())[:8]
        entry: dict[str, Any] = {
            "id": entry_id,
            "operation_id": (operation_id := str(uuid.uuid4())),
            "idempotency_key": operation_id,
            "video_path": str(video_path),
            "caption": caption,
            "platforms": clean_platforms,
            "scheduled_time": scheduled_utc.isoformat(),
            "timezone": zone_name(zone) or zone_name(self._tz),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "error": None,
            "post_results": {},
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
        self._clear_abort(entry_id)
        logger.info(f"Scheduled post {entry['id']} for {scheduled_utc} (UTC)")
        return self._with_local_view(entry)

    # ── edit ──────────────────────────────────────────────────────────

    def edit(
        self,
        entry_id: str,
        *,
        video_path: str | None = None,
        caption: str | None = None,
        scheduled_time: datetime | None = None,
        platforms: list[str] | None = None,
        repeat_rule: Any = _UNSET,
        tz: tzinfo | None = None,
    ) -> dict[str, Any] | None:
        """Edit a pending scheduled post in place.

        Only the fields passed are changed. ``id``, ``operation_id`` and
        ``idempotency_key`` are never re-issued, so a retry or a recovery
        lookup keyed on the original identity keeps working. A ``failed``
        entry is re-armed to ``pending`` (its error is cleared) because the
        user is fixing it; anything else keeps its status.

        Args:
            entry_id: The id of the entry to edit.
            video_path: New media path.
            caption: New caption (validated like :meth:`add`).
            scheduled_time: New publish time (naive = local wall clock in
                ``tz``); stored as UTC.
            platforms: New platform list (empty list = all enabled).
            repeat_rule: 'daily' | 'weekly' | 'monthly' | None. Omitted =
                unchanged.
            tz: Zone that a naive ``scheduled_time`` is entered in.

        Returns:
            The updated entry, or None when no entry has that id.

        Raises:
            ValueError: On an invalid repeat_rule, oversized caption, or a
                non-datetime scheduled_time.
        """
        if repeat_rule is not _UNSET and repeat_rule not in (None, "daily", "weekly", "monthly"):
            raise ValueError(
                f"Invalid repeat_rule: {repeat_rule!r}. "
                f"Must be one of: None, 'daily', 'weekly', 'monthly'"
            )
        if scheduled_time is not None and not isinstance(scheduled_time, datetime):
            raise ValueError(
                f"scheduled_time must be a datetime, got {type(scheduled_time).__name__}."
            )
        if caption is not None:
            caption = caption if isinstance(caption, str) else str(caption)
            if len(caption) > MAX_CAPTION_LENGTH:
                raise ValueError(
                    f"Caption is {len(caption):,} characters; the maximum is "
                    f"{MAX_CAPTION_LENGTH:,}. Shorten the caption before scheduling."
                )
        clean_platforms = (
            [p.strip() for p in (platforms or []) if p and p.strip()]
            if platforms is not None
            else None
        )
        zone = tz or self._tz

        with self._process_lock():
            with self._lock:
                # Reload so an edit applies on top of the latest on-disk state
                # (another process may have added or claimed entries).
                self._reload_locked()
                entry = next((e for e in self._entries if e.get("id") == entry_id), None)
                if entry is None:
                    return None

                if video_path is not None:
                    entry["video_path"] = str(video_path)
                if caption is not None:
                    entry["caption"] = caption
                if clean_platforms is not None:
                    entry["platforms"] = clean_platforms
                if repeat_rule is not _UNSET:
                    entry["repeat_rule"] = repeat_rule
                if scheduled_time is not None:
                    entry["scheduled_time"] = to_utc(scheduled_time, zone).isoformat()
                    entry["timezone"] = zone_name(zone) or entry.get("timezone")
                entry["updated_at"] = datetime.now().isoformat()
                if entry.get("status") == "failed":
                    entry["status"] = "pending"
                    entry["error"] = None
                    entry["completed_at"] = None
                self._save()
                updated = self._with_local_view(entry)

        # A re-armed entry is allowed to run again.
        self._clear_abort(entry_id)
        logger.info("Edited scheduled post %s", entry_id)
        return updated

    # ── read ──────────────────────────────────────────────────────────

    def list(self) -> list[dict[str, Any]]:
        """List all scheduled posts, sorted by scheduled_time.

        Every entry carries a ``scheduled_time_local`` display value so the
        CLI, dashboard and UI all show local time while the store stays UTC.

        Returns:
            List of schedule entries.
        """
        with self._lock:
            ordered = sorted(self._entries, key=lambda e: e.get("scheduled_time", ""))
            return [self._with_local_view(e) for e in ordered]

    # ── cancel / remove ───────────────────────────────────────────────

    def cancel(self, entry_id: str) -> bool:
        """Cancel a scheduled post: remove it *and* abort any in-flight plan.

        The abort signal is raised before the removal, so a worker that already
        claimed the entry sees the cancellation even if it is mid-post, and a
        worker about to post it skips it. Removal from the store is what makes
        the cancellation durable (and visible to other processes).

        Returns:
            True if the entry was removed, False if it was already gone.
        """
        self.abort_event(entry_id).set()
        removed = self.remove(entry_id)
        if removed:
            logger.info("Cancelled scheduled post %s (abort signalled)", entry_id)
        return removed

    def remove(self, entry_id: str) -> bool:
        """Remove a scheduled post by ID (no abort signal).

        Prefer :meth:`cancel` for user-facing cancellation: it also aborts a
        plan that is already in flight.

        Args:
            entry_id: The ID of the entry to remove.

        Returns:
            True if removed, False if not found.
        """
        with self._process_lock():
            with self._lock:
                # Reload under the process lock so the removal applies on
                # top of the latest on-disk state (other processes may have
                # added entries since this instance last loaded).
                self._reload_locked()
                original_count = len(self._entries)
                self._entries = [e for e in self._entries if e.get("id") != entry_id]
                removed = len(self._entries) < original_count
                if removed:
                    self._save()
        if removed:
            logger.info(f"Removed scheduled post {entry_id}")
        return removed

    # ── due processing ────────────────────────────────────────────────

    def get_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Get posts that are due for publishing.

        Returns entries where the stored instant <= now and status ==
        "pending". Comparison is instant-based: stored UTC times and legacy
        naive local times are both resolved to an instant, so a job scheduled
        across a DST transition fires when the user's wall clock reaches it.

        Note: this is a read-only view. Workers that post must use
        :meth:`claim_due` / :meth:`claim` to guarantee exactly-once
        processing under concurrent cron + daemon invocations.

        Args:
            now: Comparison clock (defaults to the real clock). Naive values
                are read as local wall-clock time.

        Returns:
            List of due schedule entries.
        """
        with self._lock:
            return [self._with_local_view(e) for e in self._due_entries(now)]

    def _due_entries(self, now: datetime | None) -> list[dict[str, Any]]:
        """Due entries as the live records (callers that mutate must hold the lock)."""
        cutoff = self._now_utc(now)
        due = []
        for entry in self._entries:
            if entry.get("status") != "pending":
                continue
            instant = self._entry_instant(entry.get("scheduled_time"))
            if instant is not None and instant <= cutoff:
                due.append(entry)
        return due

    def claim_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Atomically claim all due entries for this worker.

        Transitions each due entry from ``pending`` to ``processing`` and
        persists the change under a cross-process file lock, so when cron
        and the serve daemon fire simultaneously only one of them posts.

        Args:
            now: Comparison clock (defaults to the real clock).

        Returns:
            The list of entries claimed by this worker (now status
            ``processing``).
        """
        with self._process_lock():
            with self._lock:
                self._reload_locked()
                due = self._due_entries(now)
                for entry in due:
                    entry["status"] = "processing"
                    entry["claimed_at"] = datetime.now().isoformat()
                if due:
                    self._save()
                return [self._with_local_view(e) for e in due]

    def claim(self, entry_id: str, *, now: datetime | None = None) -> bool:
        """Atomically claim a single entry by ID.

        Re-reads the store from disk first, so it is safe across both
        threads and OS processes.

        Args:
            entry_id: The id to claim.
            now: Comparison clock (defaults to the real clock).

        Returns:
            True if this worker claimed the entry, False if another worker
            already claimed it, it is not pending, or it is not yet due.
        """
        cutoff = self._now_utc(now)
        with self._process_lock():
            with self._lock:
                self._reload_locked()
                for entry in self._entries:
                    if entry.get("id") != entry_id:
                        continue
                    if entry.get("status") != "pending":
                        return False
                    instant = self._entry_instant(entry.get("scheduled_time"))
                    if instant is None or instant > cutoff:
                        return False
                    entry["status"] = "processing"
                    entry["claimed_at"] = datetime.now().isoformat()
                    self._save()
                    return True
                return False

    def mark_complete(
        self,
        entry_id: str,
        success: bool = True,
        error: str | None = None,
        post_results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Mark a scheduled post as completed or failed.

        ``post_results`` preserves each platform's verified result, including
        confirmed IDs/URLs and retryable errors, so a partial run can be
        targeted for recovery without losing source identity.
        """
        with self._process_lock():
            with self._lock:
                self._reload_locked()
                for entry in self._entries:
                    if entry.get("id") == entry_id:
                        entry["status"] = "completed" if success else "failed"
                        entry["completed_at"] = datetime.now().isoformat()
                        if error:
                            entry["error"] = error
                        if post_results is not None:
                            entry["post_results"] = post_results
                        # Auto-create next occurrence for recurring entries
                        if success and entry.get("repeat_rule"):
                            self._create_next_occurrence(entry)
                        break
                self._save()

    def _create_next_occurrence(self, entry: dict[str, Any]) -> None:
        """Create the next occurrence of a recurring schedule entry.

        The next occurrence advances by *local wall clock* in the entry's own
        zone and is then stored as UTC, so a 09:00 daily/weekly/monthly job
        keeps firing at 09:00 local across a DST transition instead of
        drifting by an hour.

        Args:
            entry: The completed schedule entry to base the next occurrence on.
        """
        repeat_rule = entry.get("repeat_rule")
        if not repeat_rule:
            return

        instant = self._entry_instant(entry.get("scheduled_time"))
        if instant is None:
            logger.warning(
                "Cannot create next occurrence: invalid scheduled_time in entry %s",
                entry.get("id"),
            )
            return
        zone = self._zone_for_entry(entry)
        current_time = instant.astimezone(zone)

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

        operation_id = str(uuid.uuid4())
        new_entry: dict[str, Any] = {
            "id": str(uuid.uuid4())[:8],
            "operation_id": operation_id,
            "idempotency_key": operation_id,
            "video_path": entry["video_path"],
            "caption": entry["caption"],
            "platforms": entry.get("platforms", []),
            "scheduled_time": next_time.astimezone(_UTC).isoformat(),
            "timezone": zone_name(zone) or entry.get("timezone"),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "error": None,
            "post_results": {},
            "repeat_rule": repeat_rule,
        }
        self._entries.append(new_entry)
        logger.info(
            "Created next %s occurrence %s for %s",
            repeat_rule, new_entry["id"], next_time,
        )


__all__ = [
    "MAX_CAPTION_LENGTH",
    "ScheduleManager",
    "local_zone",
    "to_local",
    "to_utc",
    "zone_name",
]
