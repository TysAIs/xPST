"""LanceDB-backed KnowledgeStore. ``lancedb`` is imported lazily so the package
imports without the heavy extra. Nuggets live in a 'nuggets' table keyed by
``Nugget.id`` (idempotent add); areas live in an 'areas' table. Vector recall
uses LanceDB's native search; everything else is a table scan, which is fine for
the corpus sizes the KB targets.

Real-LanceDB behavior is exercised by the parametrized store contract test
(tests/test_knowledge_store_contract.py) under RUN_KB_SMOKE=1; the JSON store
covers the same contract for fast default runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.store.base import KnowledgeStore

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_NUGGETS = "nuggets"
_AREAS = "areas"
# LanceDB needs a fixed vector width; unembedded nuggets store a zero vector of
# this length and are filtered out of search by the embedded flag.
_DEFAULT_DIM = 768


def _nugget_to_row(n: Nugget, dim: int) -> dict:
    vec = list(n.embedding) if n.embedding else [0.0] * dim
    return {
        "id": n.id,
        "vector": vec,
        "embedded": bool(n.embedding),
        # The rest of the nugget is round-tripped as a JSON blob so schema
        # changes to Nugget never require a LanceDB migration.
        "payload": json.dumps(n.to_dict()),
    }


def _row_to_nugget(row: dict) -> Nugget:
    return Nugget.from_dict(json.loads(row["payload"]))


class LanceDBStore(KnowledgeStore):
    def __init__(self, path: str | Path, *, dim: int = _DEFAULT_DIM) -> None:
        self._path = Path(path)
        self._dim = dim
        self._db = None  # opened lazily

    def _conn(self):
        if self._db is None:
            import lancedb  # lazy, heavy

            self._path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self._path))
        return self._db

    def _open(self, name: str, sample_row: dict | None):
        db = self._conn()
        if name in db.table_names():
            return db.open_table(name)
        if sample_row is None:
            return None
        return db.create_table(name, data=[sample_row])

    # ── nuggets ──

    def add_nugget(self, nugget: Nugget) -> None:
        if self.has_nugget(nugget.id):
            return
        if nugget.embedding:
            self._dim = len(nugget.embedding)
        row = _nugget_to_row(nugget, self._dim)
        db = self._conn()
        if _NUGGETS in db.table_names():
            db.open_table(_NUGGETS).add([row])
        else:
            db.create_table(_NUGGETS, data=[row])

    def get_nugget(self, nugget_id: str) -> Nugget | None:
        tbl = self._open(_NUGGETS, None)
        if tbl is None:
            return None
        rows = tbl.search().where(f"id = '{nugget_id}'").limit(1).to_list()
        return _row_to_nugget(rows[0]) if rows else None

    def has_nugget(self, nugget_id: str) -> bool:
        return self.get_nugget(nugget_id) is not None

    def all_nuggets(self) -> Iterable[Nugget]:
        tbl = self._open(_NUGGETS, None)
        if tbl is None:
            return []
        return [_row_to_nugget(r) for r in tbl.search().limit(10_000).to_list()]

    def search(self, embedding: Sequence[float], k: int) -> list[Nugget]:
        tbl = self._open(_NUGGETS, None)
        if tbl is None or k <= 0:
            return []
        rows = (
            tbl.search(list(embedding))
            .where("embedded = true")
            .limit(k)
            .to_list()
        )
        return [_row_to_nugget(r) for r in rows]

    # ── areas ──

    def _area_row(self, area: Area) -> dict:
        return {"id": area.id, "payload": json.dumps(area.to_dict())}

    def upsert_area(self, area: Area) -> None:
        db = self._conn()
        row = self._area_row(area)
        if _AREAS in db.table_names():
            tbl = db.open_table(_AREAS)
            tbl.delete(f"id = '{area.id}'")
            tbl.add([row])
        else:
            db.create_table(_AREAS, data=[row])

    def remove_area(self, area_id: str) -> None:
        db = self._conn()
        if _AREAS in db.table_names():
            db.open_table(_AREAS).delete(f"id = '{area_id}'")

    def areas(self) -> list[Area]:
        db = self._conn()
        if _AREAS not in db.table_names():
            return []
        rows = db.open_table(_AREAS).search().limit(10_000).to_list()
        areas = [Area.from_dict(json.loads(r["payload"])) for r in rows]
        return sorted(areas, key=lambda a: (a.order_index, a.label))

    def assign(self, nugget_id: str, area_id: str | None) -> None:
        existing = self.get_nugget(nugget_id)
        if existing is None:
            return
        updated = existing.with_area(area_id)
        db = self._conn()
        tbl = db.open_table(_NUGGETS)
        tbl.delete(f"id = '{nugget_id}'")
        tbl.add([_nugget_to_row(updated, self._dim)])

    def set_difficulty(self, nugget_id: str, difficulty: str) -> None:
        existing = self.get_nugget(nugget_id)
        if existing is None:
            return
        updated = existing.with_difficulty(difficulty)
        db = self._conn()
        tbl = db.open_table(_NUGGETS)
        tbl.delete(f"id = '{nugget_id}'")
        tbl.add([_nugget_to_row(updated, self._dim)])
