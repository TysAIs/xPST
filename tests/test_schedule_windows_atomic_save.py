"""Windows-safe schedule persistence.

The cross-process claim is the single-winner latch for a scheduled post. On
Windows the loser of that race hit ``PermissionError: [WinError 5]`` inside
``ScheduleManager._save`` and crashed instead of returning False, which is how
``tests/test_scheduler_adversarial.py::TestDoubleFireRace`` failed on the
windows-latest / Python 3.10 lane. Persisting must retry a transient lock.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

import xpst.schedule_manager as schedule_module
from xpst.schedule_manager import ScheduleManager

if TYPE_CHECKING:
    from pathlib import Path


def test_save_retries_transient_windows_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single WinError 5 on os.replace must not crash schedule persistence."""
    manager = ScheduleManager(str(tmp_path))
    real_replace = schedule_module.os.replace
    attempts = {"count": 0}

    def flaky_replace(src: str, dst: str) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(schedule_module.os, "replace", flaky_replace)
    monkeypatch.setattr(schedule_module.time, "sleep", lambda _seconds: None)

    manager.add(
        video_path="clip.mp4",
        caption="retry me",
        scheduled_time=datetime(2030, 1, 1, 9, 0),
    )

    assert attempts["count"] >= 2, "persistence did not retry the transient lock"
    entries = ScheduleManager(str(tmp_path)).list()
    assert [entry["caption"] for entry in entries] == ["retry me"]


def test_save_gives_up_and_cleans_up_when_the_lock_is_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent denial still raises -- but never leaks a temp file."""
    manager = ScheduleManager(str(tmp_path))

    def always_denied(src: str, dst: str) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(schedule_module.os, "replace", always_denied)
    monkeypatch.setattr(schedule_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        manager.add(
            video_path="clip.mp4",
            caption="never lands",
            scheduled_time=datetime(2030, 1, 1, 9, 0),
        )

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".schedule_")]
    assert leftovers == [], f"temp files leaked: {leftovers}"
