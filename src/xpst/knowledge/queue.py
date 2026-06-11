r"""Durable ingestion queue for the knowledge base.

A persisted, resumable, idempotent queue of sources to ingest. It mirrors the
manifest's atomic-write discipline (tempfile + ``os.replace``, see
``src/xpst/state_store.py`` and ``knowledge/manifest.py``) so a crash mid-write
can never leave a half-written queue file, and it never imports a heavy KB
dependency -- it is pure-Python state behind the lazy-load wall.

Lifecycle of one item (``QueueItem.status``):

    enqueue -> pending --dequeue--> in_progress --mark_done---> done
                                              \--mark_failed-> failed

``dequeue`` atomically claims the oldest ``pending`` item by flipping it to
``in_progress`` and persisting before returning, so two workers never claim the
same item and a crash after the claim is recoverable. On load (and via
``requeue_stale``) any ``in_progress`` item left behind by a crashed worker is
reset to ``pending`` so work resumes rather than stalling. ``enqueue`` is
idempotent on the source content hash: re-adding a source already present (in
any non-terminal state) is a no-op, matching the manifest's source-level dedup.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from xpst.knowledge.models import (
    QUEUE_DONE,
    QUEUE_FAILED,
    QUEUE_IN_PROGRESS,
    QUEUE_PENDING,
    QueueItem,
)

_SCHEMA_VERSION = 1
# A non-terminal item can be re-dequeued; re-enqueueing a source that already
# has a live (pending/in_progress) item is a no-op.
_LIVE_STATES = frozenset({QUEUE_PENDING, QUEUE_IN_PROGRESS})


class IngestionQueue:
    """Durable, resumable ingestion queue persisted as JSON.

    Order is insertion order (``enqueued_at`` then id for stability). All
    mutating operations persist atomically before returning so the on-disk
    state always reflects the in-memory state.
    """

    def __init__(self, path: str | Path, *,
                 now: float | None = None) -> None:
        self.path = Path(path)
        # ``_items`` preserves enqueue order; dict keyed by id for O(1) lookup.
        self._items: dict[str, QueueItem] = {}
        self._load()
        # Recover from a crash: an item left ``in_progress`` had no worker to
        # finish it, so make it dequeueable again.
        self._requeue_stale(persist=True)

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Corrupt queue: start empty rather than crash the worker. The
            # manifest dedups so already-ingested sources are not re-run.
            return
        raw_items = data.get("items", []) if isinstance(data, dict) else []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = QueueItem.from_dict(raw)
            self._items[item.id] = item

    def _atomic_write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "items": [it.to_dict() for it in self._ordered()],
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            prefix=f"{self.path.name}.tmp.",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        try:
            os.replace(tmp_path, self.path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def _ordered(self) -> list[QueueItem]:
        return sorted(self._items.values(),
                      key=lambda it: (it.enqueued_at, it.id))

    def _requeue_stale(self, *, persist: bool) -> int:
        """Reset any ``in_progress`` item back to ``pending`` (crash recovery).
        Returns how many were reset. Persists only if something changed and
        ``persist`` is True."""
        changed = 0
        for item_id, item in list(self._items.items()):
            if item.status == QUEUE_IN_PROGRESS:
                self._items[item_id] = QueueItem.from_dict({
                    **item.to_dict(),
                    "status": QUEUE_PENDING,
                    "updated_at": self._now(),
                })
                changed += 1
        if changed and persist:
            self._atomic_write()
        return changed

    @staticmethod
    def _now() -> float:
        return time.time()

    # ── public API ───────────────────────────────────────────────────────--

    def enqueue(self, source: str, *, workspace: str = "default") -> QueueItem:
        """Add ``source`` to the queue. Idempotent: if a live (pending or
        in-progress) item for the same source already exists it is returned
        unchanged. A previously terminal (done/failed) source is re-opened as a
        fresh ``pending`` item so an explicit re-add can retry it."""
        item = QueueItem.create(source=source, workspace=workspace,
                                enqueued_at=self._now())
        existing = self._items.get(item.id)
        if existing is not None and existing.status in _LIVE_STATES:
            return existing
        # New, or re-opening a terminal item: (re)set to a clean pending entry.
        self._items[item.id] = item
        self._atomic_write()
        return item

    def dequeue(self) -> QueueItem | None:
        """Claim the oldest ``pending`` item, flip it to ``in_progress``,
        persist, and return it. Returns None when nothing is pending. The claim
        is durable so a crash after dequeue resumes the same item on reload."""
        for item in self._ordered():
            if item.status == QUEUE_PENDING:
                claimed = QueueItem.from_dict({
                    **item.to_dict(),
                    "status": QUEUE_IN_PROGRESS,
                    "attempts": item.attempts + 1,
                    "updated_at": self._now(),
                })
                self._items[item.id] = claimed
                self._atomic_write()
                return claimed
        return None

    def peek(self) -> QueueItem | None:
        """Return the oldest ``pending`` item WITHOUT claiming it. Read-only."""
        for item in self._ordered():
            if item.status == QUEUE_PENDING:
                return item
        return None

    def mark_done(self, item_id: str) -> None:
        """Move an item to the terminal ``done`` state. No-op if absent."""
        self._set_status(item_id, QUEUE_DONE, reason=None)

    def mark_failed(self, item_id: str, reason: str) -> None:
        """Move an item to the terminal ``failed`` state with ``reason``. A
        failed item is not retried automatically; ``kb doctor`` surfaces it and
        an explicit ``enqueue`` of the same source re-opens it. No-op if
        absent."""
        self._set_status(item_id, QUEUE_FAILED, reason=reason)

    def _set_status(self, item_id: str, status: str,
                    *, reason: str | None) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        self._items[item_id] = QueueItem.from_dict({
            **item.to_dict(),
            "status": status,
            "reason": reason,
            "updated_at": self._now(),
        })
        self._atomic_write()

    def get(self, item_id: str) -> QueueItem | None:
        return self._items.get(item_id)

    def items(self) -> list[QueueItem]:
        """All items in insertion order (read-only snapshot)."""
        return self._ordered()

    def counts(self) -> dict[str, int]:
        """Count of items per status, with every known state present (0 when
        empty) so callers/diagnostics get a stable shape."""
        out = {
            QUEUE_PENDING: 0,
            QUEUE_IN_PROGRESS: 0,
            QUEUE_DONE: 0,
            QUEUE_FAILED: 0,
        }
        for item in self._items.values():
            out[item.status] = out.get(item.status, 0) + 1
        return out

    def is_empty(self) -> bool:
        """True when no item is pending (the queue has no work to drain)."""
        return self.peek() is None
