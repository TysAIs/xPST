"""Regression: the schedule store must use explicit utf-8 encoding.

Non-ASCII captions (emoji, CJK) are normal for social posting. Without an
explicit ``encoding="utf-8"`` the open() calls fall back to the locale codepage
(cp1252 on Windows) and ``json.dump(..., ensure_ascii=False)`` raises
``UnicodeEncodeError``. See docs/AUDIT-2026-06-10.md item 1.
"""

from __future__ import annotations

import builtins
from datetime import datetime

from xpst.schedule_manager import ScheduleManager


def test_schedule_file_opened_with_utf8(tmp_path, monkeypatch):
    encodings: list[str | None] = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if str(file).endswith("schedule.json"):
            encodings.append(kwargs.get("encoding"))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)

    mgr = ScheduleManager(config_dir=str(tmp_path))
    mgr.add(
        video_path="/tmp/v.mp4",
        caption="release 🎉 日本語 café",
        scheduled_time=datetime(2026, 6, 10, 10, 0, 0),
    )
    # Force a fresh read from disk (exercises _load on an existing file).
    mgr2 = ScheduleManager(config_dir=str(tmp_path))

    assert encodings, "schedule.json was never opened"
    assert all(enc == "utf-8" for enc in encodings), (
        f"schedule.json must be opened with encoding='utf-8', saw {encodings}"
    )

    entries = mgr2.list()
    assert entries[0]["caption"] == "release 🎉 日本語 café"
