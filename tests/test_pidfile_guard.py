"""Pidfile guard tests: acquire / release / stale-steal / verify semantics.

Covers the shared single-instance helper used by ``run`` / ``watch`` /
``post`` / ``serve``. The OS-level lock (fcntl.flock) is the real
double-post guard; the recorded PID is metadata used to detect and safely
steal stale lock files left by crashed processes.
"""

from __future__ import annotations

import json
import os

import pytest

from xpst.utils.pidfile import PidfileLock, PidfileLockError, _pid_exists

DEAD_PID = 999_999_99  # not a real running process


def test_pidfile_acquire_writes_own_pid(tmp_path):
    """acquire() records our PID in the pidfile."""
    lock = PidfileLock(str(tmp_path))
    lock.acquire()
    try:
        data = json.loads((tmp_path / "xpst.pid").read_text())
        assert data["pid"] == os.getpid()
        assert "started_at" in data
    finally:
        lock.release()


def test_pidfile_release_removes_file(tmp_path):
    """release() removes the pidfile we own."""
    lock = PidfileLock(str(tmp_path))
    lock.acquire()
    assert (tmp_path / "xpst.pid").exists()
    lock.release()
    assert not (tmp_path / "xpst.pid").exists()


def test_pidfile_release_idempotent(tmp_path):
    """Double release is a no-op and never raises."""
    lock = PidfileLock(str(tmp_path))
    lock.acquire()
    lock.release()
    lock.release()


def test_double_acquire_raises(tmp_path):
    """A second exclusive acquire while the first is held raises."""
    first = PidfileLock(str(tmp_path))
    first.acquire()
    try:
        second = PidfileLock(str(tmp_path))
        with pytest.raises(PidfileLockError):
            second.acquire()
    finally:
        first.release()


def test_acquire_steals_stale_pidfile(tmp_path):
    """A pidfile recording a dead PID is stolen (lock succeeds, pid replaced).

    The OS-level lock is the source of truth and is auto-released on process
    death, so a stale pidfile never blocks a fresh acquire — the pid content
    is simply overwritten with our own.
    """
    stale = {"pid": DEAD_PID, "started_at": "2026-08-25T05:16:27"}
    (tmp_path / "xpst.pid").write_text(json.dumps(stale))

    assert PidfileLock(str(tmp_path)).is_stale() is True

    lock = PidfileLock(str(tmp_path))
    lock.acquire()
    try:
        data = json.loads((tmp_path / "xpst.pid").read_text())
        assert data["pid"] == os.getpid()
        assert data["pid"] != DEAD_PID
    finally:
        lock.release()


def test_is_stale_false_for_live_or_missing_pidfile(tmp_path):
    """is_stale() is True only for a recorded-but-dead pid."""
    lock = PidfileLock(str(tmp_path))
    assert lock.is_stale() is False  # no file yet

    lock.acquire()
    try:
        assert lock.is_stale() is False  # recorded pid is ours (live)
    finally:
        lock.release()

    (tmp_path / "xpst.pid").write_text("not json")
    assert lock.is_stale() is False  # unreadable -> not stale


def test_acquire_raises_when_live_holder_and_pid_dead(monkeypatch, tmp_path):
    """Contention with a live flock holder raises even if the pid is stale.

    flock is the source of truth: if the lock cannot be taken, another live
    instance is active (flock is released on process death), so we must not
    steal the file out from under it.
    """
    stale = {"pid": DEAD_PID, "started_at": "2026-08-25T05:16:27"}
    (tmp_path / "xpst.pid").write_text(json.dumps(stale))

    lock = PidfileLock(str(tmp_path))

    def always_busy(fd: int) -> None:  # noqa: ARG001
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(lock, "_lock", always_busy)
    with pytest.raises(PidfileLockError):
        lock.acquire()
    # File must survive so the live holder's inode is untouched.
    assert (tmp_path / "xpst.pid").exists()


def test_verify_true_while_held_after_release_false(tmp_path):
    """verify() reflects whether a live holder owns the pidfile."""
    lock = PidfileLock(str(tmp_path))
    lock.acquire()
    try:
        assert lock.verify() is True
    finally:
        lock.release()
    assert lock.verify() is False


def test_verify_false_for_stale_pidfile(tmp_path):
    """verify() returns False when the recorded pid is dead."""
    (tmp_path / "xpst.pid").write_text(json.dumps({"pid": DEAD_PID, "started_at": "x"}))
    lock = PidfileLock(str(tmp_path))
    assert lock.verify() is False


def test_release_does_not_clobber_replacement_lock(tmp_path):
    """release() must not unlink a pidfile another process has taken over."""
    lock = PidfileLock(str(tmp_path))
    lock.acquire()
    # Simulate a successor acquiring the lock and rewriting the file.
    successor = {"pid": DEAD_PID, "started_at": "later"}  # different owner
    (tmp_path / "xpst.pid").write_text(json.dumps(successor))
    lock.release()
    # File must survive because the recorded pid is no longer ours.
    assert (tmp_path / "xpst.pid").exists()


def test_pid_exists_helper():
    """_pid_exists distinguishes live vs dead pids (self vs sentinel)."""
    assert _pid_exists(os.getpid()) is True
    assert _pid_exists(DEAD_PID) is False
