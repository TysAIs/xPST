"""Durable in-app scheduling engine (kanban t_19c77c53).

Contract pinned here:

- create / edit / cancel of scheduled posts are persisted durably (a fresh
  ``ScheduleManager`` — i.e. an app restart — sees them),
- times are entered in local time, stored as UTC, and displayed back in local
  time, including across a DST boundary (2026-11-01 America/Denver),
- ``run_due`` picks up due jobs when the engine starts and on every interval
  while it keeps running,
- cancelling removes the entry from the store *and* aborts a plan that is
  already in flight, and a cancelled entry never fires.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from xpst.schedule_manager import ScheduleManager
from xpst.scheduling_engine import SchedulingEngine

try:
    DENVER = ZoneInfo("America/Denver")
except Exception:  # pragma: no cover - platform without the IANA tz database
    DENVER = None

if DENVER is None:  # pragma: no cover - platform without the IANA tz database
    pytestmark = pytest.mark.skip(
        reason="no IANA timezone database on this platform (install tzdata)"
    )

# America/Denver DST ends 2026-11-01 at 02:00 local (MDT -> MST), so a naive
# local noon is UTC-6 on 2026-10-31 and UTC-7 on 2026-11-01.
BEFORE_DST_LOCAL = datetime(2026, 10, 31, 12, 0)
AFTER_DST_LOCAL = datetime(2026, 11, 1, 12, 0)
BEFORE_DST_UTC = "2026-10-31T18:00:00+00:00"
AFTER_DST_UTC = "2026-11-01T19:00:00+00:00"


@pytest.fixture
def config_dir(tmp_path: Path) -> str:
    return str(tmp_path / ".xpst")


@pytest.fixture
def manager(config_dir: str) -> ScheduleManager:
    return ScheduleManager(config_dir, tz=DENVER)


# ── fake engine ────────────────────────────────────────────────────────


class _FakeUploadResult:
    def __init__(self, success: bool = True, error: str | None = None) -> None:
        self.success = success
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {"status": "published" if self.success else "failed", "error": self.error}


class _FakePostResult:
    def __init__(self, all_success: bool = True, error: str | None = None) -> None:
        self.all_success = all_success
        self.results = {"youtube": _FakeUploadResult(all_success, error)}


class _FakeEngine:
    """Fake CrossPostEngine. Can block inside a post to model an in-flight plan."""

    def __init__(
        self,
        *,
        ok: bool = True,
        raise_exc: Exception | None = None,
        block_captions: set[str] | None = None,
    ) -> None:
        self.ok = ok
        self.raise_exc = raise_exc
        self.block_captions = block_captions or set()
        self.posts: list[tuple[str, str, list[str] | None]] = []
        self.in_flight = threading.Event()
        self.release = threading.Event()

    async def post_manual(self, video_path, caption, platforms=None, per_platform_captions=None):  # type: ignore[no-untyped-def]
        self.posts.append((str(video_path), caption, platforms))
        if caption in self.block_captions:
            self.in_flight.set()
            self.release.wait(10)
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakePostResult(self.ok, None if self.ok else "platform rejected")


def _now_local_naive() -> datetime:
    """Local wall-clock "now" in the zone under test.

    Schedule times are entered in local time, so a test that builds a *due*
    time must build it from the same zone — not from the runner's clock (CI
    runs in UTC, where a naive ``datetime.now()`` is six hours behind Denver
    local and an entry meant to be due comes out six hours in the future).
    """
    return datetime.now(DENVER).replace(tzinfo=None)


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00")
    return path


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ── 1. create / edit / cancel durability ───────────────────────────────


def test_create_is_durable_across_restart(manager, config_dir, tmp_path):
    entry = manager.add(
        video_path=str(_video(tmp_path)),
        caption="survives restart",
        scheduled_time=BEFORE_DST_LOCAL,
        platforms=["youtube"],
    )

    restarted = ScheduleManager(config_dir, tz=DENVER)
    entries = restarted.list()
    assert [e["id"] for e in entries] == [entry["id"]]
    assert entries[0]["caption"] == "survives restart"
    assert entries[0]["platforms"] == ["youtube"]
    assert entries[0]["status"] == "pending"
    assert entries[0]["operation_id"] == entry["operation_id"]


def test_edit_is_durable_across_restart(manager, config_dir, tmp_path):
    entry = manager.add(
        video_path=str(_video(tmp_path)),
        caption="before",
        scheduled_time=BEFORE_DST_LOCAL,
        platforms=["youtube"],
    )

    updated = manager.edit(
        entry["id"],
        caption="after",
        scheduled_time=AFTER_DST_LOCAL,
        platforms=["x"],
    )
    assert updated is not None
    assert updated["caption"] == "after"
    # Editing never re-identifies the job: recovery/audit keys stay intact.
    assert updated["operation_id"] == entry["operation_id"]
    assert updated["idempotency_key"] == entry["idempotency_key"]

    restarted = ScheduleManager(config_dir, tz=DENVER)
    stored = restarted.list()[0]
    assert stored["caption"] == "after"
    assert stored["platforms"] == ["x"]
    # Stored as UTC — the edit is the post-DST local noon (UTC-7).
    assert stored["scheduled_time"] == AFTER_DST_UTC
    assert restarted.local_time(stored).isoformat() == AFTER_DST_LOCAL.replace(
        tzinfo=DENVER
    ).isoformat()


def test_edit_unknown_entry_is_reported_not_silent(manager):
    assert manager.edit("deadbeef", caption="nope") is None


def test_edit_rejects_oversized_caption(manager, tmp_path):
    entry = manager.add("/tmp/v.mp4", "c", BEFORE_DST_LOCAL)
    with pytest.raises(ValueError, match="caption"):
        manager.edit(entry["id"], caption="x" * 200_000)


def test_edit_rearms_a_failed_entry(manager, tmp_path):
    entry = manager.add("/tmp/v.mp4", "c", BEFORE_DST_LOCAL)
    manager.claim(entry["id"])
    manager.mark_complete(entry["id"], success=False, error="boom")
    assert manager.list()[0]["status"] == "failed"

    manager.edit(entry["id"], caption="retry me")
    stored = manager.list()[0]
    assert stored["status"] == "pending"
    assert stored["error"] is None


def test_cancel_removes_entry_from_store_and_disk(manager, config_dir, tmp_path):
    entry = manager.add("/tmp/v.mp4", "cancel me", BEFORE_DST_LOCAL)

    assert manager.cancel(entry["id"]) is True
    assert ScheduleManager(config_dir, tz=DENVER).list() == []
    raw = json.loads(Path(config_dir, "schedule.json").read_text(encoding="utf-8"))
    assert raw == []
    # A second cancel (or an unknown id) is a reported failure, not a success.
    assert manager.cancel(entry["id"]) is False
    assert manager.cancel("unknown-id") is False


# ── 2. timezone correctness across a DST boundary ──────────────────────


def test_store_is_utc_and_display_is_local_across_dst(manager, tmp_path):
    before = manager.add("/tmp/v.mp4", "before dst", BEFORE_DST_LOCAL)
    after = manager.add("/tmp/v.mp4", "after dst", AFTER_DST_LOCAL)

    # Stored: explicit UTC instants, one wall-clock hour apart in UTC terms
    # because the local offset changed by an hour.
    assert before["scheduled_time"] == BEFORE_DST_UTC
    assert after["scheduled_time"] == AFTER_DST_UTC
    assert datetime.fromisoformat(before["scheduled_time"]).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(after["scheduled_time"]).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(after["scheduled_time"]) - datetime.fromisoformat(
        before["scheduled_time"]
    ) == timedelta(hours=25)

    # Displayed: the same local wall-clock time, with the offset that applied
    # on that side of the transition.
    local_before = manager.local_time(before)
    local_after = manager.local_time(after)
    assert (local_before.hour, local_before.minute) == (12, 0)
    assert (local_after.hour, local_after.minute) == (12, 0)
    assert local_before.utcoffset() == timedelta(hours=-6)  # MDT
    assert local_after.utcoffset() == timedelta(hours=-7)  # MST

    # Every listed entry carries its local rendering for the UI/CLI.
    listed = {entry["caption"]: entry for entry in manager.list()}
    assert listed["before dst"]["scheduled_time_local"] == local_before.isoformat()
    assert listed["after dst"]["scheduled_time_local"] == local_after.isoformat()


def test_aware_input_is_converted_not_reinterpreted(manager):
    aware = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    entry = manager.add("/tmp/v.mp4", "utc input", aware)
    assert entry["scheduled_time"] == "2026-11-01T12:00:00+00:00"
    # 12:00 UTC is 05:00 local (MST) — never a blind wall-clock copy.
    assert manager.local_time(entry).hour == 5


def test_due_comparison_uses_instants_across_the_dst_fold(manager):
    """2026-11-01 01:30 local happens twice; the stored instant decides."""
    first_pass = manager.add("/tmp/v.mp4", "first pass", datetime(2026, 11, 1, 0, 30))
    second_pass = manager.add("/tmp/v.mp4", "second pass", datetime(2026, 11, 1, 2, 30))

    assert first_pass["scheduled_time"] == "2026-11-01T06:30:00+00:00"  # MDT
    assert second_pass["scheduled_time"] == "2026-11-01T09:30:00+00:00"  # MST

    # 08:00 UTC on 2026-11-01 is after the first local pass, before the second.
    now = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    due = manager.get_due(now=now)
    assert [e["caption"] for e in due] == ["first pass"]

    # And one hour later the second pass is due too.
    due_later = manager.get_due(now=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))
    assert sorted(e["caption"] for e in due_later) == ["first pass", "second pass"]


def test_legacy_naive_entries_still_evaluate_as_local_wall_clock(manager, config_dir):
    """Entries written by older builds (naive local strings) keep working."""
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    Path(config_dir, "schedule.json").write_text(
        json.dumps(
            [
                {
                    "id": "legacy1",
                    "video_path": "/tmp/v.mp4",
                    "caption": "legacy",
                    "platforms": [],
                    "scheduled_time": "2026-11-01T09:00:00",
                    "status": "pending",
                }
            ]
        ),
        encoding="utf-8",
    )
    restarted = ScheduleManager(config_dir, tz=DENVER)
    assert restarted.get_due() == []  # 09:00 local is still in the future
    assert len(restarted.get_due(now=datetime(2026, 11, 1, 9, 30))) == 1


# ── 3. run-due: on start and while running ─────────────────────────────


def test_run_due_fires_due_job_on_engine_start(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)
    manager.add(str(video), "due on start", _now_local_naive() - timedelta(minutes=1))

    engine = _FakeEngine()
    scheduler = SchedulingEngine(engine, manager=manager, interval=30.0)
    try:
        scheduler.start()
        assert _wait_for(lambda: bool(engine.posts)), "due job never fired on start"
    finally:
        scheduler.stop()

    assert engine.posts[0][1] == "due on start"
    assert ScheduleManager(config_dir, tz=DENVER).list()[0]["status"] == "completed"


def test_run_due_picks_up_job_added_while_running(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)

    engine = _FakeEngine()
    scheduler = SchedulingEngine(engine, manager=manager, interval=0.05)
    try:
        scheduler.start()
        assert _wait_for(lambda: scheduler.is_running)
        # Nothing due yet: the loop is alive and has not posted anything.
        manager.add(str(video), "added while running", _now_local_naive() - timedelta(minutes=1))
        assert _wait_for(lambda: bool(engine.posts)), "interval loop never picked up the job"
    finally:
        scheduler.stop()

    assert engine.posts[0][1] == "added while running"
    fresh = ScheduleManager(config_dir, tz=DENVER)
    assert [e["status"] for e in fresh.list()] == ["completed"]


def test_run_due_reports_counts_and_failures(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)
    manager.add(str(video), "will fail", _now_local_naive() - timedelta(minutes=1))

    counts = SchedulingEngine(_FakeEngine(ok=False), manager=manager).run_due()
    assert counts == {"due": 1, "posted": 0, "failed": 1, "aborted": 0}
    stored = ScheduleManager(config_dir, tz=DENVER).list()[0]
    assert stored["status"] == "failed"
    assert stored["error"]


def test_run_due_survives_a_raising_engine(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)
    manager.add(str(video), "boom", _now_local_naive() - timedelta(minutes=1))

    counts = SchedulingEngine(
        _FakeEngine(raise_exc=RuntimeError("provider down")), manager=manager
    ).run_due()
    assert counts == {"due": 1, "posted": 0, "failed": 1, "aborted": 0}
    assert ScheduleManager(config_dir, tz=DENVER).list()[0]["status"] == "failed"


def test_run_due_ignores_future_entries(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)
    manager.add(str(video), "later", _now_local_naive() + timedelta(hours=2))

    engine = _FakeEngine()
    counts = SchedulingEngine(engine, manager=manager).run_due()
    assert counts == {"due": 0, "posted": 0, "failed": 0, "aborted": 0}
    assert engine.posts == []


# ── 4. cancel aborts the plan ──────────────────────────────────────────


def test_cancel_before_due_never_fires(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)
    entry = manager.add(str(video), "cancelled", _now_local_naive() + timedelta(minutes=30))
    assert manager.cancel(entry["id"]) is True

    engine = _FakeEngine()
    counts = SchedulingEngine(engine, manager=manager).run_due()
    assert engine.posts == []
    assert counts["posted"] == 0

    # The cancelled job stays gone in a restarted app, too.
    assert ScheduleManager(config_dir, tz=DENVER).list() == []


def test_cancel_in_flight_aborts_completion(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)
    entry = manager.add(str(video), "in flight", _now_local_naive() - timedelta(minutes=1))

    engine = _FakeEngine(block_captions={"in flight"})
    scheduler = SchedulingEngine(engine, manager=manager)
    counts: list[dict[str, int]] = []

    def _run() -> None:
        counts.append(scheduler.run_due())

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        assert engine.in_flight.wait(10), "post never started"
        # Cancel while the plan is mid-flight: the entry leaves the store...
        assert manager.cancel(entry["id"]) is True
    finally:
        engine.release.set()
    worker.join(10)

    assert counts, "run_due never returned"
    assert counts[0]["aborted"] == 1
    assert counts[0]["posted"] == 0
    # ...and the in-flight plan is not recorded as a completion afterwards.
    assert ScheduleManager(config_dir, tz=DENVER).list() == []


def test_cancel_of_queued_entry_skips_the_post(config_dir, tmp_path):
    video = _video(tmp_path)
    manager = ScheduleManager(config_dir, tz=DENVER)
    past = _now_local_naive() - timedelta(minutes=1)
    first = manager.add(str(video), "blocking", past)
    second = manager.add(str(video), "queued", past)

    engine = _FakeEngine(block_captions={"blocking"})
    scheduler = SchedulingEngine(engine, manager=manager)
    counts: list[dict[str, int]] = []

    def _run() -> None:
        counts.append(scheduler.run_due())

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        assert engine.in_flight.wait(10), "post never started"
        assert manager.cancel(second["id"]) is True
    finally:
        engine.release.set()
    worker.join(10)

    captions = [p[1] for p in engine.posts]
    assert captions == ["blocking"], f"a cancelled entry was posted: {captions}"
    assert counts[0]["aborted"] == 1
    stored_ids = [e["id"] for e in ScheduleManager(config_dir, tz=DENVER).list()]
    assert stored_ids == [first["id"]]
