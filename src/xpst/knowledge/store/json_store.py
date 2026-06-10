"""Simple JSON-file KnowledgeStore. Phase 1 only — replaced by a LanceDB
adapter behind the same interface in Phase 2."""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from xpst.knowledge.models import Nugget
from xpst.knowledge.store.base import KnowledgeStore


class JsonKnowledgeStore(KnowledgeStore):
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._nuggets: dict[str, Nugget] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._nuggets = {k: Nugget.from_dict(v) for k, v in raw.items()}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.to_dict() for k, v in self._nuggets.items()}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

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
