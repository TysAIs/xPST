"""Dependency-free JSON-file KnowledgeStore. Kept as the always-available
fallback so the KB works without LanceDB. Nuggets persist to ``nuggets.json``
and areas to ``areas.json`` beside it. Vector search is brute-force cosine."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.store.base import KnowledgeStore

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return dot / (na * nb)


class JsonKnowledgeStore(KnowledgeStore):
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._areas_path = self._path.parent / "areas.json"
        self._nuggets: dict[str, Nugget] = {}
        self._areas: dict[str, Area] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._nuggets = {k: Nugget.from_dict(v) for k, v in raw.items()}
        if self._areas_path.exists():
            raw = json.loads(self._areas_path.read_text(encoding="utf-8"))
            self._areas = {k: Area.from_dict(v) for k, v in raw.items()}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.to_dict() for k, v in self._nuggets.items()}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _flush_areas(self) -> None:
        self._areas_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.to_dict() for k, v in self._areas.items()}
        self._areas_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add_nugget(self, nugget: Nugget) -> None:
        if nugget.id in self._nuggets:
            return
        self._nuggets[nugget.id] = nugget
        self._flush()

    def get_nugget(self, nugget_id: str) -> Nugget | None:
        return self._nuggets.get(nugget_id)

    def has_nugget(self, nugget_id: str) -> bool:
        return nugget_id in self._nuggets

    def all_nuggets(self) -> Iterable[Nugget]:
        return list(self._nuggets.values())

    def search(self, embedding: Sequence[float], k: int) -> list[Nugget]:
        scored = [
            (_cosine(embedding, n.embedding), n)
            for n in self._nuggets.values()
            if n.embedding
        ]
        scored = [(s, n) for s, n in scored if s > -1.0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [n for _s, n in scored[: max(0, k)]]

    def upsert_area(self, area: Area) -> None:
        self._areas[area.id] = area
        self._flush_areas()

    def remove_area(self, area_id: str) -> None:
        if area_id in self._areas:
            del self._areas[area_id]
            self._flush_areas()

    def areas(self) -> list[Area]:
        return sorted(self._areas.values(),
                      key=lambda a: (a.order_index, a.label))

    def assign(self, nugget_id: str, area_id: str | None) -> None:
        existing = self._nuggets.get(nugget_id)
        if existing is None:
            return
        self._nuggets[nugget_id] = existing.with_area(area_id)
        self._flush()

    def set_difficulty(self, nugget_id: str, difficulty: str) -> None:
        existing = self._nuggets.get(nugget_id)
        if existing is None:
            return
        self._nuggets[nugget_id] = existing.with_difficulty(difficulty)
        self._flush()
