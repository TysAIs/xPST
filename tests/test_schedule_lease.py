"""Lease/reaper contract for claimed schedule entries.

A worker that dies between ``claim`` and ``mark_complete`` leaves its entry in
``processing``. Nothing reads ``claimed_at``, so before this contract existed the
entry was invisible to ``get_due`` (pending only) forever: a crashed daemon
silently lost the post.

Every instant here is built as an **aware** datetime in the manager's own zone
(``_LOCAL``). A bare ``datetime.now()`` is host wall-clock time, and the manager
reads a naive value as wall-clock time *in its configured zone* — so the same
test would be due on a machine in Denver and seven hours in the future on a UTC
CI runner. Pinning these assertions to explicit instants is what makes the
contract mean the same thing on every host.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from xpst.schedule_manager import ScheduleManager

_LOCAL = ZoneInfo("America/Denver")


def _recently() -> datetime:
    """An instant one minute ago, expressed in the manager's zone."""
    return datetime.now(_LOCAL) - timedelta(minutes=1)


def _manager(tmp_path: Path) -> ScheduleManager:
    return ScheduleManager(str(tmp_path), tz=_LOCAL)


def _age_claim(tmp_path: Path, entry_id: str, age: timedelta) -> None:
    """Simulate a worker that claimed the entry and then died ``age`` ago."""
    store = tmp_path / "schedule.json"
    rows = json.loads(store.read_text(encoding="utf-8"))
    claimed_at = datetime.now(_LOCAL) - age
    for row in rows:
        if row["id"] == entry_id:
            row["status"] = "processing"
            row["claimed_at"] = claimed_at.isoformat()
    store.write_text(json.dumps(rows), encoding="utf-8")


def test_stale_processing_entry_is_reclaimed_to_pending(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    entry = manager.add(
        video_path="clip.mp4",
        caption="lease",
        scheduled_time=_recently(),
        platforms=["x"],
    )
    assert [e["id"] for e in manager.claim_due()] == [entry["id"]]
    _age_claim(tmp_path, entry["id"], timedelta(hours=2))

    reclaimed = _manager(tmp_path).reclaim_stale(lease_seconds=300)

    assert reclaimed == [entry["id"]]
    stored = next(e for e in _manager(tmp_path).list() if e["id"] == entry["id"])
    assert stored["status"] == "pending"
    assert "claimed_at" not in stored
    assert "stale lease" in (stored["error"] or "")


def test_fresh_claim_is_left_alone(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    entry = manager.add(
        video_path="clip.mp4",
        caption="still running",
        scheduled_time=_recently(),
        platforms=["x"],
    )
    assert manager.claim_due()

    assert manager.reclaim_stale(lease_seconds=300) == []
    stored = next(e for e in manager.list() if e["id"] == entry["id"])
    assert stored["status"] == "processing"


def test_reclaimed_entry_is_due_again(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    entry = manager.add(
        video_path="clip.mp4",
        caption="retry me",
        scheduled_time=_recently(),
        platforms=["x"],
    )
    manager.claim_due()
    _age_claim(tmp_path, entry["id"], timedelta(hours=2))

    retry = _manager(tmp_path)
    retry.reclaim_stale(lease_seconds=300)

    assert [e["id"] for e in retry.get_due()] == [entry["id"]]
