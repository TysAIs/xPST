"""Knowledge base data models."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    h.update("\n".join(parts).encode("utf-8"))
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class Nugget:
    id: str
    point: str
    source_video_id: str
    timestamp_start: float
    timestamp_end: float
    source_url: str | None = None
    area_id: str | None = None
    difficulty: str = "beginner"
    prerequisites: tuple[str, ...] = field(default_factory=tuple)
    embedding: tuple[float, ...] = field(default_factory=tuple)
    created_at: float = 0.0

    @classmethod
    def create(cls, *, point: str, source_video_id: str,
               timestamp_start: float, timestamp_end: float,
               source_url: str | None = None,
               difficulty: str = "beginner",
               prerequisites: Iterable[str] = (),
               created_at: float = 0.0) -> Nugget:
        return cls(
            id=_hash(source_video_id, point),
            point=point,
            source_video_id=source_video_id,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            source_url=source_url,
            difficulty=difficulty,
            prerequisites=tuple(prerequisites),
            created_at=created_at,
        )

    def with_embedding(self, vec: Sequence[float]) -> Nugget:
        """Return a new Nugget carrying ``vec``. The id is unchanged because it
        is a function of (source_video_id, point) only, so re-embedding never
        re-keys an existing nugget."""
        return replace(self, embedding=tuple(float(x) for x in vec))

    def with_area(self, area_id: str | None) -> Nugget:
        """Return a new Nugget assigned to ``area_id`` (id unchanged)."""
        return replace(self, area_id=area_id)

    def with_difficulty(self, difficulty: str) -> Nugget:
        """Return a new Nugget tagged with ``difficulty`` (id unchanged)."""
        return replace(self, difficulty=difficulty)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["prerequisites"] = list(self.prerequisites)
        d["embedding"] = list(self.embedding)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Nugget:
        d = dict(d)
        d["prerequisites"] = tuple(d.get("prerequisites", ()))
        # Phase 1 nuggets.json predates these keys — tolerate their absence.
        d["embedding"] = tuple(d.get("embedding", ()))
        d.setdefault("created_at", 0.0)
        return cls(**d)


@dataclass(frozen=True)
class Area:
    """A course module — discovered by clustering, not seeded. The id is a
    function of cluster MEMBERSHIP (sorted ``nugget_ids``) when members are
    known, falling back to ``label`` only for degenerate empty-membership areas
    so they do not all collide on one constant id. Keying on membership means a
    non-deterministic LLM that re-phrases the label for the same cluster re-keys
    to the same area instead of accumulating ghost areas. ``label`` is a mutable
    display field replaced in place via ``upsert_area``.
    """

    id: str
    label: str
    centroid: tuple[float, ...] = field(default_factory=tuple)
    nugget_ids: tuple[str, ...] = field(default_factory=tuple)
    order_index: int = 0

    @classmethod
    def create(cls, *, label: str, centroid: Sequence[float] = (),
               nugget_ids: Iterable[str] = (), order_index: int = 0) -> Area:
        ids = tuple(nugget_ids)
        # Key on membership for real (clustered) areas; fall back to the label
        # for degenerate empty-membership areas so they keep distinct ids.
        key_parts = sorted(ids) if ids else [label]
        return cls(
            id=_hash("area", *key_parts),
            label=label,
            centroid=tuple(float(x) for x in centroid),
            nugget_ids=ids,
            order_index=order_index,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["centroid"] = list(self.centroid)
        d["nugget_ids"] = list(self.nugget_ids)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Area:
        d = dict(d)
        d["centroid"] = tuple(float(x) for x in d.get("centroid", ()))
        d["nugget_ids"] = tuple(d.get("nugget_ids", ()))
        d.setdefault("order_index", 0)
        return cls(**d)


# Durable ingestion-queue states. ``pending`` items are dequeueable;
# ``in_progress`` marks an item a worker has claimed but not yet finished, so a
# crash mid-ingest is recoverable (it is reset to ``pending`` on the next
# ``requeue_stale`` / load reconciliation); terminal states are ``done`` and
# ``failed``.
QUEUE_PENDING = "pending"
QUEUE_IN_PROGRESS = "in_progress"
QUEUE_DONE = "done"
QUEUE_FAILED = "failed"
_QUEUE_STATES = frozenset(
    {QUEUE_PENDING, QUEUE_IN_PROGRESS, QUEUE_DONE, QUEUE_FAILED}
)


@dataclass(frozen=True)
class QueueItem:
    """One durable ingestion-queue entry. The ``id`` is a content hash of the
    source so re-enqueueing the same source is idempotent (dedup key), matching
    the manifest's source-level dedup. ``attempts`` survives restarts so retry
    policy is durable, and ``reason`` records why a terminal ``failed`` item
    failed for ``kb doctor`` to surface."""

    id: str
    source: str
    status: str = QUEUE_PENDING
    workspace: str = "default"
    attempts: int = 0
    reason: str | None = None
    enqueued_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def create(cls, *, source: str, workspace: str = "default",
               enqueued_at: float = 0.0) -> QueueItem:
        return cls(
            id=_hash("queue", source),
            source=source,
            workspace=workspace,
            enqueued_at=enqueued_at,
            updated_at=enqueued_at,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> QueueItem:
        d = dict(d)
        d.setdefault("status", QUEUE_PENDING)
        d.setdefault("workspace", "default")
        d.setdefault("attempts", 0)
        d.setdefault("reason", None)
        d.setdefault("enqueued_at", 0.0)
        d.setdefault("updated_at", 0.0)
        # Tolerate an unknown status by normalizing to pending rather than
        # crashing the worker on a forward-incompatible queue file.
        if d.get("status") not in _QUEUE_STATES:
            d["status"] = QUEUE_PENDING
        return cls(**d)
