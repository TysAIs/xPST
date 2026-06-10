"""Knowledge base data models."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field


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

    @classmethod
    def create(cls, *, point: str, source_video_id: str,
               timestamp_start: float, timestamp_end: float,
               source_url: str | None = None) -> "Nugget":
        return cls(
            id=_hash(source_video_id, point),
            point=point,
            source_video_id=source_video_id,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            source_url=source_url,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["prerequisites"] = list(self.prerequisites)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Nugget":
        d = dict(d)
        d["prerequisites"] = tuple(d.get("prerequisites", ()))
        return cls(**d)
