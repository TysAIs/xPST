"""Dependency-free JSON-file KnowledgeStore. Kept as the always-available
fallback so the KB works without LanceDB. Nuggets persist to ``nuggets.json``
and areas to ``areas.json`` beside it. Vector search is brute-force cosine."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.knowledge.models import Area, Nugget
from xpst.utils.logger import get_logger

logger = get_logger(__name__)
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
        self._nuggets = self._load_file(self._path, Nugget.from_dict)
        self._areas = self._load_file(self._areas_path, Area.from_dict)

    @staticmethod
    def _load_file(path: Path, decode) -> dict:
        """Corruption-tolerant load (G33): a truncated/corrupt JSON file is
        quarantined to ``<name>.corrupt`` and the store starts empty instead
        of crashing every subsequent KB operation."""
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {k: decode(v) for k, v in raw.items()}
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, KeyError) as exc:
            quarantine = path.with_suffix(path.suffix + ".corrupt")
            try:
                path.replace(quarantine)
            except OSError:
                pass
            logger.warning("Corrupt store file %s quarantined to %s (%s)",
                           path.name, quarantine.name, exc)
            return {}

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        """tempfile + os.replace so a crash mid-write never truncates the
        store (G33) — same pattern as manifest.py/queue.py."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _flush(self) -> None:
        self._atomic_write(self._path,
                           {k: v.to_dict() for k, v in self._nuggets.items()})

    def _flush_areas(self) -> None:
        self._atomic_write(self._areas_path,
                           {k: v.to_dict() for k, v in self._areas.items()})

    def add_nugget(self, nugget: Nugget) -> None:
        if nugget.id in self._nuggets:
            return
        self._nuggets[nugget.id] = nugget
        self._flush()

    def replace_nugget(self, nugget: Nugget) -> None:
        self._nuggets[nugget.id] = nugget
        self._flush()

    def get_nugget(self, nugget_id: str) -> Nugget | None:
        return self._nuggets.get(nugget_id)

    def has_nugget(self, nugget_id: str) -> bool:
        return nugget_id in self._nuggets

    def all_nuggets(self) -> Iterable[Nugget]:
        return list(self._nuggets.values())

    def search(self, embedding: Sequence[float], k: int) -> list[Nugget]:
        return [n for n, _s in self.search_with_scores(embedding, k)]

    def search_with_scores(
        self, embedding: Sequence[float], k: int
    ) -> list[tuple[Nugget, float | None]]:
        scored = [
            (_cosine(embedding, n.embedding), n)
            for n in self._nuggets.values()
            if n.embedding
        ]
        scored = [(s, n) for s, n in scored if s > -1.0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [(n, s) for s, n in scored[: max(0, k)]]

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
