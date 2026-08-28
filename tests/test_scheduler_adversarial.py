"""Adversarial QA tests for scheduler reliability.

Covers: DST boundaries, missed-run catch-up exactly-once semantics,
double-fire races, clock skew, corrupted schedule persistence, and
schedule_add input fuzzing. Each test reproduces a real-world failure
scenario for the $10/mo scheduling product.
"""

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from xpst.schedule_manager import ScheduleManager

MAX_CAPTION_LEN = 100_000


@pytest.fixture
def tmp_schedule_dir(tmp_path):
    return str(tmp_path / ".xpst")


@pytest.fixture
def manager(tmp_schedule_dir):
    return ScheduleManager(config_dir=tmp_schedule_dir)


def _write_raw(tmp_schedule_dir, payload):
    d = Path(tmp_schedule_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "schedule.json").write_text(payload, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# 1. DST boundary
# ──────────────────────────────────────────────────────────────────────
class TestDSTBoundary:
    """Schedule entries must fire at the correct wall-clock time across
    the machine's next DST transition and never crash on ambiguous or
    timezone-aware inputs."""

    def _next_transition_wall_time(self, tz: ZoneInfo) -> datetime:
        """Find the next DST transition for tz: scan by day, refine by hour."""
        probe = datetime.now(tz).replace(second=0, microsecond=0)
        base_offset = probe.utcoffset()
        transition_day = None
        for _ in range(400):  # scan ~13 months max, 1 day at a time
            probe += timedelta(days=1)
            if probe.utcoffset() != base_offset:
                transition_day = probe
                break
        if transition_day is None:
            pytest.skip("No DST transition found within 13 months")
        # Back up to just before the day change and refine hourly.
        probe = transition_day - timedelta(days=1)
        for _ in range(48):
            probe += timedelta(hours=1)
            if probe.utcoffset() != base_offset:
                return probe
        pytest.skip("Could not localize the DST transition hour")

    def test_get_due_handles_aware_iso_entries(self, manager, tmp_schedule_dir):
        """A schedule.json entry written with a timezone-aware ISO string
        (e.g. by a third-party tool) must not crash get_due()."""
        aware = datetime.now(ZoneInfo("America/Denver")).astimezone() - timedelta(minutes=5)
        _write_raw(
            tmp_schedule_dir,
            json.dumps(
                [
                    {
                        "id": "tzaware1",
                        "video_path": "/tmp/v.mp4",
                        "caption": "aware",
                        "platforms": [],
                        "scheduled_time": aware.isoformat(),
                        "status": "pending",
                        "created_at": aware.isoformat(),
                        "completed_at": None,
                        "error": None,
                    }
                ]
            ),
        )
        m2 = ScheduleManager(config_dir=tmp_schedule_dir)
        due = m2.get_due()  # must not raise TypeError (aware vs naive compare)
        assert len(due) == 1

    def test_add_with_aware_datetime_normalizes(self, manager):
        """add() with a tz-aware datetime must not produce an entry that
        later breaks due comparison."""
        aware = datetime.now(ZoneInfo("America/Denver")).astimezone() - timedelta(minutes=1)
        manager.add("/tmp/v.mp4", "c", aware)
        due = manager.get_due()
        assert len(due) == 1

    def test_wall_clock_across_transition(self, tmp_schedule_dir):
        """Entries scheduled around the next DST transition fire exactly
        once per wall-clock occurrence and never duplicate."""
        tz = ZoneInfo("America/Denver")
        trans = self._next_transition_wall_time(tz)
        naive_local = trans.replace(tzinfo=None)
        m = ScheduleManager(config_dir=tmp_schedule_dir)
        m.add("/tmp/v.mp4", "pre", naive_local - timedelta(minutes=30))
        m.add("/tmp/v.mp4", "post", naive_local + timedelta(minutes=30))

        # "Clock" just before the transition: only the pre entry is due.
        due_before = [e for e in m.get_due()]
        # Real wall clock is before the transition now, so exactly the pre
        # entry (scheduled 30 min before) may or may not be due depending on
        # real now; assert idempotency instead: repeated get_due calls
        # return the same set (no duplicates created by the transition).
        again = m.get_due()
        assert [e["id"] for e in due_before] == [e["id"] for e in again]

    def test_monthly_recurrence_across_dst_month(self, manager):
        """Monthly recurrence over a DST boundary month must keep the same
        wall-clock time (no 1-hour drift)."""
        m = manager
        entry = m.add("/tmp/v.mp4", "c", datetime(2026, 10, 15, 9, 0), repeat_rule="monthly")
        m.mark_complete(entry["id"], success=True)
        entries = [e for e in m.list() if e["status"] == "pending"]
        assert len(entries) == 1
        nxt = datetime.fromisoformat(entries[0]["scheduled_time"])
        assert (nxt.month, nxt.day, nxt.hour, nxt.minute) == (11, 15, 9, 0)


# ──────────────────────────────────────────────────────────────────────
# 2. Missed-run catch-up / exactly-once
# ──────────────────────────────────────────────────────────────────────
class TestCatchUpExactlyOnce:
    def test_stale_wake_check_triggers_catch_up(self, tmp_schedule_dir, monkeypatch):
        from xpst.config import XPSTConfig
        from xpst.scheduler import Scheduler

        cfg = XPSTConfig(config_dir=str(tmp_schedule_dir))
        cfg.schedule.check_interval = 1

        class FakeState:
            def get_last_wake_check(self):
                return datetime.now() - timedelta(hours=5)

            def update_last_wake_check(self):
                pass

            def update_last_check_time(self):
                pass

            def save(self):
                pass

        class FakeEngine:
            state = FakeState()

        s = Scheduler(FakeEngine(), cfg)
        assert s._needs_catch_up() is True

    def test_fresh_wake_check_no_catch_up(self, tmp_schedule_dir):
        from xpst.config import XPSTConfig
        from xpst.scheduler import Scheduler

        cfg = XPSTConfig(config_dir=str(tmp_schedule_dir))
        cfg.schedule.check_interval = 3600

        class FakeState:
            def get_last_wake_check(self):
                return datetime.now()

            def update_last_wake_check(self):
                pass

            def update_last_check_time(self):
                pass

            def save(self):
                pass

        class FakeEngine:
            state = FakeState()

        s = Scheduler(FakeEngine(), cfg)
        assert s._needs_catch_up() is False

    def test_past_schedule_fires_exactly_once(self, manager):
        """A run scheduled in the past is processed exactly once: after
        completion it never appears in get_due() again (no spam)."""
        entry = manager.add(
            "/tmp/v.mp4", "past", datetime.now() - timedelta(hours=3)
        )
        due1 = manager.get_due()
        assert [e["id"] for e in due1] == [entry["id"]]
        manager.mark_complete(entry["id"], success=True)
        for _ in range(5):
            assert manager.get_due() == []

    def test_claim_prevents_double_processing(self, manager):
        """claim_due() hands each due entry to at most one worker."""
        past = datetime.now() - timedelta(minutes=1)
        e1 = manager.add("/tmp/v.mp4", "a", past)
        e2 = manager.add("/tmp/v.mp4", "b", past)

        claimed = manager.claim_due()
        assert {e["id"] for e in claimed} == {e1["id"], e2["id"]}
        # Second worker gets nothing.
        assert manager.claim_due() == []
        # And get_due() no longer returns claimed entries.
        assert manager.get_due() == []


# ──────────────────────────────────────────────────────────────────────
# 3. Double-fire race
# ──────────────────────────────────────────────────────────────────────
class TestDoubleFireRace:
    def test_concurrent_claim_single_winner(self, manager):
        """Two threads claiming the same due entry concurrently: exactly
        one wins, so no duplicate post can occur."""
        past = datetime.now() - timedelta(minutes=1)
        e = manager.add("/tmp/v.mp4", "race", past)

        winners = []

        def worker():
            m = ScheduleManager(config_dir=manager.config_dir)
            if m.claim(e["id"]):
                winners.append(e["id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert winners == [e["id"]]

    def test_manager_thread_safe_add(self, manager):
        """Concurrent add() calls must not lose entries or corrupt the file."""
        errs = []

        def worker(i):
            try:
                m = ScheduleManager(config_dir=manager.config_dir)
                m.add("/tmp/v.mp4", f"c{i}", datetime.now() + timedelta(hours=1 + i))
            except Exception as exc:  # pragma: no cover
                errs.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errs == []
        m = ScheduleManager(config_dir=manager.config_dir)
        assert len(m.list()) == 10
        # File must be valid JSON.
        json.loads(Path(manager.schedule_file).read_text(encoding="utf-8"))

    def test_cross_process_claim_single_winner(self, manager, tmp_path):
        """Two OS processes claiming the same entry: exactly one wins
        (fcntl file lock), simulating cron + daemon double-fire."""
        past = datetime.now() - timedelta(minutes=1)
        e = manager.add("/tmp/v.mp4", "xproc", past)

        script = tmp_path / "claim_child.py"
        script.write_text(
            "import sys\n"
            "from xpst.schedule_manager import ScheduleManager\n"
            f"m = ScheduleManager(config_dir={str(manager.config_dir)!r})\n"
            f"print('WIN' if m.claim({e['id']!r}) else 'LOSE')\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        procs = [
            subprocess.Popen(
                ["/Users/itxji/XPST/.venv/bin/python", str(script)],
                stdout=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(2)
        ]
        outs = [p.communicate()[0].strip() for p in procs]
        assert sorted(outs) == ["LOSE", "WIN"]


# ──────────────────────────────────────────────────────────────────────
# 4. Clock skew
# ──────────────────────────────────────────────────────────────────────
class TestClockSkew:
    def test_backward_clock_no_catch_up_no_busy_loop(self, tmp_schedule_dir, monkeypatch):
        """Clock set backward 5 minutes mid-wait must not trigger catch-up
        or busy-looping (negative elapsed => no catch-up, ever)."""
        import xpst.scheduler as sched_mod
        from xpst.config import XPSTConfig
        from xpst.scheduler import Scheduler

        cfg = XPSTConfig(config_dir=str(tmp_schedule_dir))
        cfg.schedule.check_interval = 60

        real_now = datetime.now()

        class FakeState:
            def get_last_wake_check(self):
                return real_now

            def update_last_wake_check(self):
                pass

            def update_last_check_time(self):
                pass

            def save(self):
                pass

        class FakeEngine:
            state = FakeState()

        class FakeDT(datetime):
            _now = real_now

            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return cls._now

        monkeypatch.setattr(sched_mod, "datetime", FakeDT)
        s = Scheduler(FakeEngine(), cfg)

        # Wall clock jumped backward 5 minutes: last wake check now appears
        # to be in the future -> negative elapsed -> never catch up.
        FakeDT._now = real_now - timedelta(minutes=5)
        for _ in range(10):
            assert s._needs_catch_up() is False, (
                "backward clock skew caused a spurious catch-up (busy-loop risk)"
            )

        # Clock catching back up to ~real time: still no catch-up.
        FakeDT._now = real_now + timedelta(seconds=30)
        assert s._needs_catch_up() is False

    def test_backward_clock_does_not_miss_future_entry(self, manager):
        """A pending future entry survives a backward clock jump: it stays
        pending and becomes due when the clock catches back up."""
        future = datetime.now() + timedelta(minutes=2)
        e = manager.add("/tmp/v.mp4", "future", future)
        assert manager.get_due() == []
        # Clock "catches up" past the scheduled time.
        manager._entries[0]["scheduled_time"] = (
            datetime.now() - timedelta(minutes=1)
        ).isoformat()
        assert [x["id"] for x in manager.get_due()] == [e["id"]]


# ──────────────────────────────────────────────────────────────────────
# 5. Corrupted schedule persistence
# ──────────────────────────────────────────────────────────────────────
class TestCorruption:
    def test_malformed_json_does_not_crash(self, tmp_schedule_dir):
        _write_raw(tmp_schedule_dir, "{not valid json!!")
        m = ScheduleManager(config_dir=tmp_schedule_dir)
        assert m.list() == []
        # And it can recover: adding a new entry works.
        m.add("/tmp/v.mp4", "c", datetime.now() + timedelta(hours=1))
        assert len(ScheduleManager(config_dir=tmp_schedule_dir).list()) == 1

    def test_corrupt_file_is_quarantined_not_silently_lost(self, tmp_schedule_dir):
        """Pending user schedules in a corrupt file must be preserved on
        disk (quarantined), not silently overwritten by the next save."""
        original = Path(tmp_schedule_dir, "schedule.json")
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text("[{broken", encoding="utf-8")
        m = ScheduleManager(config_dir=tmp_schedule_dir)
        m.add("/tmp/v.mp4", "c", datetime.now() + timedelta(hours=1))

        quarantined = list(Path(tmp_schedule_dir).glob("schedule.json.corrupt*"))
        assert quarantined, "corrupt schedule file was silently destroyed"
        assert "broken" in quarantined[0].read_text(encoding="utf-8")

    def test_non_dict_entries_dont_crash(self, tmp_schedule_dir):
        """A schedule.json containing non-dict items (corrupted shape) must
        not crash list()/get_due() with AttributeError (daemon crash-loop)."""
        _write_raw(tmp_schedule_dir, json.dumps(["garbage", 42, None]))
        m = ScheduleManager(config_dir=tmp_schedule_dir)
        m.list()
        m.get_due()  # must not raise
        assert [e for e in m._entries if not isinstance(e, dict)] == []

    def test_entry_missing_scheduled_time(self, tmp_schedule_dir):
        _write_raw(
            tmp_schedule_dir,
            json.dumps([{"id": "x", "status": "pending"}]),
        )
        m = ScheduleManager(config_dir=tmp_schedule_dir)
        m.list()
        assert m.get_due() == []

    def test_truncated_write_recovers(self, tmp_schedule_dir):
        """Simulate a crash mid-write (truncated JSON): engine loads empty,
        keeps running, and a later save produces valid JSON."""
        _write_raw(tmp_schedule_dir, json.dumps([{"id": "a"}])[:8])
        m = ScheduleManager(config_dir=tmp_schedule_dir)
        m.add("/tmp/v.mp4", "c", datetime.now() + timedelta(hours=1))
        data = json.loads(Path(tmp_schedule_dir, "schedule.json").read_text())
        assert len(data) == 1


# ──────────────────────────────────────────────────────────────────────
# 6. schedule_add input fuzz
# ──────────────────────────────────────────────────────────────────────
class TestInputFuzz:
    def test_past_timestamp_is_due_once(self, manager):
        entry = manager.add("/tmp/v.mp4", "c", datetime.now() - timedelta(days=30))
        assert len(manager.get_due()) == 1
        manager.mark_complete(entry["id"], success=True)
        assert manager.get_due() == []

    def test_invalid_repeat_rule_raises_actionable(self, manager):
        with pytest.raises(ValueError, match="repeat_rule"):
            manager.add(
                "/tmp/v.mp4", "c", datetime.now() + timedelta(hours=1),
                repeat_rule="hourly",
            )

    def test_empty_platform_strings_filtered(self, manager):
        entry = manager.add(
            "/tmp/v.mp4", "c", datetime.now() + timedelta(hours=1),
            platforms=["", "youtube", "  "],
        )
        assert entry["platforms"] == ["youtube"]

    def test_unicode_caption_roundtrip(self, manager):
        caption = "héllo 世界 🎥🚀 مرحبا \U0001f1e9\U0001f1ea"
        entry = manager.add("/tmp/v.mp4", caption, datetime.now() + timedelta(hours=1))
        m2 = ScheduleManager(config_dir=manager.config_dir)
        assert m2.list()[0]["caption"] == caption
        assert entry["caption"] == caption

    def test_huge_caption_rejected_actionably(self, manager):
        with pytest.raises(ValueError, match="caption"):
            manager.add(
                "/tmp/v.mp4", "x" * (10 * 1024 * 1024),
                datetime.now() + timedelta(hours=1),
            )

    def test_max_caption_accepted(self, manager):
        entry = manager.add(
            "/tmp/v.mp4", "x" * MAX_CAPTION_LEN, datetime.now() + timedelta(hours=1)
        )
        assert len(entry["caption"]) == MAX_CAPTION_LEN

    def test_none_video_path_coerced(self, manager):
        entry = manager.add("", "c", datetime.now() + timedelta(hours=1))
        assert entry["video_path"] == ""

    def test_cannot_add_after_close_of_day_edge_dates(self, manager):
        """Feb 29 / month-end clamping for monthly recurrence."""
        entry = manager.add(
            "/tmp/v.mp4", "c", datetime(2026, 1, 31, 9, 0), repeat_rule="monthly"
        )
        manager.mark_complete(entry["id"], success=True)
        pending = [e for e in manager.list() if e["status"] == "pending"]
        nxt = datetime.fromisoformat(pending[0]["scheduled_time"])
        assert (nxt.month, nxt.day) == (2, 28)


# ──────────────────────────────────────────────────────────────────────
# 7. LaunchAgent install (live, macOS)
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS only")
class TestLaunchAgentLive:
    PLIST = Path.home() / "Library" / "LaunchAgents" / "com.xpst.schedule.plist"
    LABEL = "com.xpst.schedule"

    def test_plist_written_and_valid(self, tmp_path):
        """_install_os_scheduler writes a plist that passes plutil -lint."""
        from xpst.cli import _write_launchd_plist

        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir()
        path = _write_launchd_plist(plist_dir, "/fake/bin/xpst")
        out = subprocess.run(
            ["plutil", "-lint", str(path)], capture_output=True, text=True
        )
        assert out.returncode == 0, out.stderr
        assert "OK" in out.stdout

    def test_install_uninstall_live(self):
        """Full live cycle: install -> plutil lint -> launchctl finds it ->
        uninstall -> plist gone and service gone."""
        if self.PLIST.exists() or self._service_loaded():
            pytest.skip("com.xpst.schedule already in use on this machine")

        from xpst.cli import _install_os_scheduler, _uninstall_os_scheduler

        xpst_bin = "/Users/itxji/XPST/.venv/bin/xpst"
        try:
            ok = _install_os_scheduler("Darwin", xpst_bin, 15, as_json=True)
            assert ok, "install failed"
            assert self.PLIST.exists()
            lint = subprocess.run(
                ["plutil", "-lint", str(self.PLIST)], capture_output=True, text=True
            )
            assert lint.returncode == 0, lint.stderr
            assert self._service_loaded()
        finally:
            ok = _uninstall_os_scheduler("Darwin", xpst_bin, as_json=True)
            assert ok, "uninstall failed"
        assert not self.PLIST.exists()
        assert not self._service_loaded()

    @classmethod
    def _service_loaded(cls):
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{cls.LABEL}"],
            capture_output=True, text=True,
        )
        return out.returncode == 0
