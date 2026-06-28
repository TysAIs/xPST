"""Durable ingestion queue: enqueue/dequeue/peek/mark-done/mark-failed,
idempotency, atomic persistence, and crash-resume."""
from __future__ import annotations

import json

from xpst.knowledge.models import (
    QUEUE_DONE,
    QUEUE_FAILED,
    QUEUE_IN_PROGRESS,
    QUEUE_PENDING,
)
from xpst.knowledge.queue import IngestionQueue


def test_enqueue_then_peek_and_dequeue(tmp_path):
    q = IngestionQueue(tmp_path / "queue.json")
    assert q.peek() is None
    item = q.enqueue("https://x/v1")
    assert item.status == QUEUE_PENDING

    peeked = q.peek()
    assert peeked is not None
    assert peeked.source == "https://x/v1"
    # peek does not claim
    assert peeked.status == QUEUE_PENDING

    claimed = q.dequeue()
    assert claimed is not None
    assert claimed.id == item.id
    assert claimed.status == QUEUE_IN_PROGRESS
    assert claimed.attempts == 1
    # nothing pending now
    assert q.peek() is None
    assert q.dequeue() is None


def test_fifo_order_by_enqueue_time(tmp_path):
    q = IngestionQueue(tmp_path / "queue.json")
    # Force a strictly-increasing clock so enqueue order is deterministic; an
    # inexhaustible counter survives the extra _now() calls dequeue makes.
    clock = {"t": 0.0}

    def _tick() -> float:
        clock["t"] += 1.0
        return clock["t"]

    q._now = _tick  # type: ignore[method-assign]
    a = q.enqueue("a")
    b = q.enqueue("b")
    c = q.enqueue("c")
    assert [i.source for i in q.items()] == ["a", "b", "c"]
    assert q.dequeue().id == a.id
    assert q.dequeue().id == b.id
    assert q.dequeue().id == c.id


def test_enqueue_idempotent_on_live_item(tmp_path):
    q = IngestionQueue(tmp_path / "queue.json")
    first = q.enqueue("dup")
    second = q.enqueue("dup")
    assert first.id == second.id
    assert len(q.items()) == 1


def test_mark_done_and_failed_are_terminal(tmp_path):
    q = IngestionQueue(tmp_path / "queue.json")
    a = q.enqueue("a")
    b = q.enqueue("b")
    q.dequeue()  # claim a
    q.mark_done(a.id)
    assert q.get(a.id).status == QUEUE_DONE

    claimed_b = q.dequeue()  # claim b
    assert claimed_b.id == b.id
    q.mark_failed(b.id, "boom")
    failed = q.get(b.id)
    assert failed.status == QUEUE_FAILED
    assert failed.reason == "boom"

    counts = q.counts()
    assert counts[QUEUE_DONE] == 1
    assert counts[QUEUE_FAILED] == 1
    assert counts[QUEUE_PENDING] == 0


def test_re_enqueue_reopens_failed_item(tmp_path):
    q = IngestionQueue(tmp_path / "queue.json")
    a = q.enqueue("a")
    q.dequeue()
    q.mark_failed(a.id, "boom")
    assert q.get(a.id).status == QUEUE_FAILED
    # Explicit re-add reopens it as pending for a retry.
    reopened = q.enqueue("a")
    assert reopened.id == a.id
    assert q.get(a.id).status == QUEUE_PENDING
    assert q.get(a.id).reason is None


def test_persists_across_instances(tmp_path):
    path = tmp_path / "queue.json"
    q = IngestionQueue(path)
    q.enqueue("a")
    q.enqueue("b")
    reloaded = IngestionQueue(path)
    assert [i.source for i in reloaded.items()] == ["a", "b"]
    assert reloaded.peek().source == "a"


def test_in_progress_resumes_after_crash(tmp_path):
    """An item claimed (in_progress) then never finished — simulating a crashed
    worker — is reset to pending on reload so work resumes."""
    path = tmp_path / "queue.json"
    q = IngestionQueue(path)
    item = q.enqueue("a")
    claimed = q.dequeue()
    assert claimed.status == QUEUE_IN_PROGRESS
    # Simulate a crash: a brand-new queue loads the on-disk in_progress item.
    reloaded = IngestionQueue(path)
    resumed = reloaded.get(item.id)
    assert resumed.status == QUEUE_PENDING
    # And it is dequeueable again.
    assert reloaded.peek().id == item.id
    again = reloaded.dequeue()
    assert again.status == QUEUE_IN_PROGRESS
    # attempts incremented across the two claims.
    assert again.attempts == 2


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "queue.json"
    q = IngestionQueue(path)
    q.enqueue("a")
    q.dequeue()
    assert list(tmp_path.glob("*.tmp.*")) == []
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert len(data["items"]) == 1


def test_corrupt_queue_recovers_to_empty(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text("{ not json", encoding="utf-8")
    q = IngestionQueue(path)  # must not raise
    assert q.items() == []
    item = q.enqueue("a")
    assert q.get(item.id).status == QUEUE_PENDING


def test_is_empty_reflects_pending_work(tmp_path):
    q = IngestionQueue(tmp_path / "queue.json")
    assert q.is_empty()
    a = q.enqueue("a")
    assert not q.is_empty()
    q.dequeue()
    q.mark_done(a.id)
    # done is terminal -> no pending work left
    assert q.is_empty()


def test_mark_missing_item_is_noop(tmp_path):
    q = IngestionQueue(tmp_path / "queue.json")
    q.mark_done("nope")  # must not raise
    q.mark_failed("nope", "reason")  # must not raise
    assert q.items() == []


def test_fifo_survives_identical_timestamps(tmp_path):
    """Windows clock granularity (~15.6ms) makes same-tick enqueues common;
    the insertion sequence must keep FIFO order across persistence even when
    every enqueued_at is identical."""
    path = tmp_path / "queue.json"
    q = IngestionQueue(path, now=1000.0)
    for name in ("a", "b", "c", "d"):
        q.enqueue(name)
    reloaded = IngestionQueue(path)
    assert [i.source for i in reloaded.items()] == ["a", "b", "c", "d"]
    assert reloaded.peek().source == "a"
