"""Schedule operation identity contracts."""

from datetime import datetime, timedelta

from xpst.schedule_manager import ScheduleManager


def test_schedule_entry_has_stable_operation_id_and_idempotency_key(tmp_path):
    manager = ScheduleManager(str(tmp_path))
    entry = manager.add("clip.mp4", "caption", datetime.now() + timedelta(hours=1), ["youtube"])

    assert entry["operation_id"]
    assert entry["idempotency_key"] == entry["operation_id"]
    assert manager.list()[0]["operation_id"] == entry["operation_id"]


def test_schedule_operation_identity_survives_completion_and_recurrence(tmp_path):
    manager = ScheduleManager(str(tmp_path))
    entry = manager.add(
        "clip.mp4", "caption", datetime.now() - timedelta(minutes=1), ["youtube"], repeat_rule="daily"
    )
    manager.mark_complete(entry["id"], success=True)

    entries = manager.list()
    completed = next(item for item in entries if item["id"] == entry["id"])
    recurring = next(item for item in entries if item["id"] != entry["id"])
    assert completed["operation_id"] == entry["operation_id"]
    assert recurring["operation_id"] != entry["operation_id"]
    assert recurring["idempotency_key"] == recurring["operation_id"]
