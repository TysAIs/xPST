"""Comprehensive tests for ScheduleManager (item 29)."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xpst.schedule_manager import ScheduleManager


@pytest.fixture
def tmp_schedule_dir(tmp_path):
    """Provide a temporary config directory for ScheduleManager."""
    return str(tmp_path / ".xpst")


@pytest.fixture
def manager(tmp_schedule_dir):
    """Create a ScheduleManager with an isolated temp directory."""
    return ScheduleManager(config_dir=tmp_schedule_dir)


class TestAddEntry:
    """test_add_entry: add a scheduled post, verify it appears in list()."""

    def test_add_entry(self, manager):
        """Adding an entry makes it appear in list()."""
        entry = manager.add(
            video_path="/tmp/video.mp4",
            caption="Test caption",
            scheduled_time=datetime(2026, 12, 1, 10, 0),
            platforms=["youtube"],
        )
        assert entry["id"] is not None
        assert entry["caption"] == "Test caption"
        assert entry["video_path"] == "/tmp/video.mp4"
        assert entry["status"] == "pending"
        assert entry["platforms"] == ["youtube"]

        entries = manager.list()
        assert len(entries) == 1
        assert entries[0]["id"] == entry["id"]


class TestListSorted:
    """test_list_sorted: add multiple entries, verify list() returns them sorted."""

    def test_list_sorted(self, manager):
        """Entries are returned sorted by scheduled_time."""
        t3 = datetime(2026, 12, 3, 10, 0)
        t1 = datetime(2026, 12, 1, 10, 0)
        t2 = datetime(2026, 12, 2, 10, 0)

        e3 = manager.add("/tmp/v3.mp4", "third", t3)
        e1 = manager.add("/tmp/v1.mp4", "first", t1)
        e2 = manager.add("/tmp/v2.mp4", "second", t2)

        entries = manager.list()
        assert len(entries) == 3
        assert entries[0]["caption"] == "first"
        assert entries[1]["caption"] == "second"
        assert entries[2]["caption"] == "third"


class TestRemove:
    """test_remove: add then remove, verify removal."""

    def test_remove(self, manager):
        """Removing an existing entry returns True and removes it."""
        entry = manager.add("/tmp/v.mp4", "test", datetime(2026, 12, 1))
        assert manager.remove(entry["id"]) is True
        assert len(manager.list()) == 0


class TestRemoveNonexistent:
    """test_remove_nonexistent: remove non-existent ID returns False."""

    def test_remove_nonexistent(self, manager):
        """Removing a non-existent ID returns False."""
        assert manager.remove("nonexistent-id") is False


class TestGetDue:
    """test_get_due: add entries with past/future times, verify only past ones returned."""

    def test_get_due(self, manager):
        """Only pending entries with scheduled_time <= now are returned."""
        past = datetime.now() - timedelta(hours=1)
        future = datetime.now() + timedelta(hours=24)
        also_past = datetime.now() - timedelta(minutes=30)

        e_past = manager.add("/tmp/v1.mp4", "due", past)
        e_future = manager.add("/tmp/v2.mp4", "not yet", future)
        e_also_past = manager.add("/tmp/v3.mp4", "also due", also_past)

        due = manager.get_due()
        due_ids = [e["id"] for e in due]

        assert e_past["id"] in due_ids
        assert e_also_past["id"] in due_ids
        assert e_future["id"] not in due_ids

    def test_get_due_excludes_completed(self, manager):
        """Entries marked completed are not returned by get_due()."""
        past = datetime.now() - timedelta(hours=1)
        entry = manager.add("/tmp/v.mp4", "done", past)
        manager.mark_complete(entry["id"], success=True)

        due = manager.get_due()
        assert len(due) == 0


class TestMarkComplete:
    """test_mark_complete: mark entry complete, verify status changes."""

    def test_mark_complete(self, manager):
        """Marking an entry complete sets status to 'completed'."""
        entry = manager.add("/tmp/v.mp4", "test", datetime(2026, 12, 1))
        manager.mark_complete(entry["id"], success=True)

        entries = manager.list()
        assert len(entries) == 1
        assert entries[0]["status"] == "completed"
        assert entries[0]["completed_at"] is not None


class TestMarkFailed:
    """test_mark_failed: mark entry failed with error, verify error stored."""

    def test_mark_failed(self, manager):
        """Marking an entry failed sets status to 'failed' with error message."""
        entry = manager.add("/tmp/v.mp4", "test", datetime(2026, 12, 1))
        manager.mark_complete(entry["id"], success=False, error="Upload timeout")

        entries = manager.list()
        assert len(entries) == 1
        assert entries[0]["status"] == "failed"
        assert entries[0]["error"] == "Upload timeout"


class TestPersistence:
    """test_persistence: save, create new instance, verify data persists."""

    def test_persistence(self, tmp_schedule_dir):
        """Data survives across ScheduleManager instances."""
        m1 = ScheduleManager(config_dir=tmp_schedule_dir)
        m1.add("/tmp/v.mp4", "persisted", datetime(2026, 12, 1))
        assert len(m1.list()) == 1

        # Create new instance pointing to same directory
        m2 = ScheduleManager(config_dir=tmp_schedule_dir)
        assert len(m2.list()) == 1
        assert m2.list()[0]["caption"] == "persisted"


class TestEmptySchedule:
    """test_empty_schedule: verify empty schedule works."""

    def test_empty_schedule(self, manager):
        """Empty schedule returns empty lists and handles operations gracefully."""
        assert manager.list() == []
        assert manager.get_due() == []
        assert manager.remove("anything") is False


class TestIdUniqueness:
    """test_id_uniqueness: add multiple entries, verify all IDs different."""

    def test_id_uniqueness(self, manager):
        """All generated IDs are unique."""
        ids = set()
        for i in range(50):
            entry = manager.add(f"/tmp/v{i}.mp4", f"caption {i}", datetime(2026, 12, 1))
            ids.add(entry["id"])

        assert len(ids) == 50
