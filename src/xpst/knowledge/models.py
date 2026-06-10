"""Knowledge base data models."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace


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
               created_at: float = 0.0) -> "Nugget":
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

    def with_embedding(self, vec: Sequence[float]) -> "Nugget":
        """Return a new Nugget carrying ``vec``. The id is unchanged because it
        is a function of (source_video_id, point) only, so re-embedding never
        re-keys an existing nugget."""
        return replace(self, embedding=tuple(float(x) for x in vec))

    def with_area(self, area_id: str | None) -> "Nugget":
        """Return a new Nugget assigned to ``area_id`` (id unchanged)."""
        return replace(self, area_id=area_id)

    def with_difficulty(self, difficulty: str) -> "Nugget":
        """Return a new Nugget tagged with ``difficulty`` (id unchanged)."""
        return replace(self, difficulty=difficulty)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["prerequisites"] = list(self.prerequisites)
        d["embedding"] = list(self.embedding)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Nugget":
        d = dict(d)
        d["prerequisites"] = tuple(d.get("prerequisites", ()))
        # Phase 1 nuggets.json predates these keys — tolerate their absence.
        d["embedding"] = tuple(d.get("embedding", ()))
        d.setdefault("created_at", 0.0)
        return cls(**d)


@dataclass(frozen=True)
class Area:
    """A course module — discovered by clustering, not seeded. The id is a
    function of ``label`` only so re-labeling/re-centroiding is stable per label.
    """

    id: str
    label: str
    centroid: tuple[float, ...] = field(default_factory=tuple)
    nugget_ids: tuple[str, ...] = field(default_factory=tuple)
    order_index: int = 0

    @classmethod
    def create(cls, *, label: str, centroid: Sequence[float] = (),
               nugget_ids: Iterable[str] = (), order_index: int = 0) -> "Area":
        return cls(
            id=_hash("area", label),
            label=label,
            centroid=tuple(float(x) for x in centroid),
            nugget_ids=tuple(nugget_ids),
            order_index=order_index,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["centroid"] = list(self.centroid)
        d["nugget_ids"] = list(self.nugget_ids)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Area":
        d = dict(d)
        d["centroid"] = tuple(float(x) for x in d.get("centroid", ()))
        d["nugget_ids"] = tuple(d.get("nugget_ids", ()))
        d.setdefault("order_index", 0)
        return cls(**d)
