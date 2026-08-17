"""sqlite-vec-backed KnowledgeStore.

``sqlite-vec`` is a tiny (<1MB) C extension that adds vector search to
SQLite, replacing the ~50MB LanceDB dependency. Nuggets and areas are
stored in standard SQLite tables; vectors use sqlite-vec's virtual table
for KNN search.

The store implements the same ``KnowledgeStore`` interface as
``LanceDBStore`` and ``JsonKnowledgeStore`` so swapping is transparent.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from xpst.knowledge.models import Area, Nugget
from xpst.knowledge.store.base import KnowledgeStore

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_DEFAULT_DIM = 768

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nuggets (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    embedded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS areas (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""

_VEC_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS nugget_vec USING vec0(
    nugget_id TEXT PRIMARY KEY,
    embedding FLOAT[{dim}]
);
"""


def _nugget_to_payload(n: Nugget) -> str:
    return json.dumps(n.to_dict())


def _payload_to_nugget(payload: str) -> Nugget:
    return Nugget.from_dict(json.loads(payload))


def vec0_available() -> bool:
    """True only if the sqlite-vec extension can actually load.

    Importing ``sqlite_vec`` is not enough — the native library may be
    missing or incompatible with the platform/Python build, in which case
    ``sqlite_vec.load()`` raises. Callers use this to select a store that
    will not crash at schema time.
    """
    try:
        import sqlite_vec

        probe = sqlite3.connect(":memory:")
        probe.enable_load_extension(True)
        sqlite_vec.load(probe)
        probe.close()
        return True
    except Exception:
        return False


class SQLiteVecStore(KnowledgeStore):
    """sqlite-vec vector store — the default from Phase D3 onward.

    LanceDB remains available as a legacy backend via
    ``vector_backend: "lancedb"`` in KnowledgeConfig.
    """

    def __init__(self, path: str | Path, *, dim: int = _DEFAULT_DIM) -> None:
        self._path = Path(path)
        self._dim = dim
        self._conn: sqlite3.Connection | None = None
        self._vec_available = vec0_available()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path))
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            # Load sqlite-vec extension. When unavailable, the vec virtual
            # table is simply not created and every vector op degrades to a
            # linear scan — never a hard crash.
            if self._vec_available:
                import sqlite_vec

                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                conn.executescript(_VEC_SCHEMA.format(dim=self._dim))
            self._conn = conn
        return self._conn

    # ── nuggets ──

    def add_nugget(self, nugget: Nugget) -> None:
        db = self._db()
        if self.has_nugget(nugget.id):
            return
        embedded = 1 if nugget.embedding else 0
        db.execute(
            "INSERT OR IGNORE INTO nuggets (id, payload, embedded) VALUES (?, ?, ?)",
            (nugget.id, _nugget_to_payload(nugget), embedded),
        )
        if nugget.embedding and self._vec_available:
            vec = list(nugget.embedding)
            db.execute(
                "INSERT OR REPLACE INTO nugget_vec (nugget_id, embedding) VALUES (?, ?)",
                (nugget.id, json.dumps(vec)),
            )
        db.commit()

    def get_nugget(self, nugget_id: str) -> Nugget | None:
        db = self._db()
        row = db.execute(
            "SELECT payload FROM nuggets WHERE id = ?", (nugget_id,)
        ).fetchone()
        if row is None:
            return None
        return _payload_to_nugget(row["payload"])

    def has_nugget(self, nugget_id: str) -> bool:
        db = self._db()
        row = db.execute(
            "SELECT 1 FROM nuggets WHERE id = ?", (nugget_id,)
        ).fetchone()
        return row is not None

    def all_nuggets(self) -> Iterable[Nugget]:
        db = self._db()
        rows = db.execute("SELECT payload FROM nuggets").fetchall()
        return [_payload_to_nugget(r["payload"]) for r in rows]

    def replace_nugget(self, nugget: Nugget) -> None:
        db = self._db()
        db.execute("DELETE FROM nuggets WHERE id = ?", (nugget.id,))
        if self._vec_available:
            db.execute("DELETE FROM nugget_vec WHERE nugget_id = ?", (nugget.id,))
        self.add_nugget(nugget)

    def search(self, embedding: Sequence[float], k: int) -> list[Nugget]:
        db = self._db()
        if k <= 0:
            return []
        try:
            vec = json.dumps(list(embedding))
            rows = db.execute(
                """
                SELECT n.payload
                FROM nugget_vec v
                JOIN nuggets n ON n.id = v.nugget_id
                WHERE n.embedded = 1
                ORDER BY v.embedding <-> ?
                LIMIT ?
                """,
                (vec, k),
            ).fetchall()
            return [_payload_to_nugget(r["payload"]) for r in rows]
        except sqlite3.OperationalError:
            # sqlite-vec extension not loaded — fall back to linear scan
            return list(self.all_nuggets())[:k]

    def search_with_scores(
        self, embedding: Sequence[float], k: int
    ) -> list[tuple[Nugget, float | None]]:
        db = self._db()
        if k <= 0:
            return []
        try:
            vec = json.dumps(list(embedding))
            rows = db.execute(
                """
                SELECT n.payload, v.distance
                FROM nugget_vec v
                JOIN nuggets n ON n.id = v.nugget_id
                WHERE n.embedded = 1
                ORDER BY v.embedding <-> ?
                LIMIT ?
                """,
                (vec, k),
            ).fetchall()
            return [(_payload_to_nugget(r["payload"]), r["distance"]) for r in rows]
        except sqlite3.OperationalError:
            nuggets = list(self.all_nuggets())[:k]
            return [(n, None) for n in nuggets]

    # ── areas ──

    def upsert_area(self, area: Area) -> None:
        db = self._db()
        db.execute(
            "INSERT OR REPLACE INTO areas (id, payload) VALUES (?, ?)",
            (area.id, json.dumps(area.to_dict())),
        )
        db.commit()

    def remove_area(self, area_id: str) -> None:
        db = self._db()
        db.execute("DELETE FROM areas WHERE id = ?", (area_id,))
        db.commit()

    def areas(self) -> list[Area]:
        db = self._db()
        rows = db.execute("SELECT payload FROM areas").fetchall()
        areas = [Area.from_dict(json.loads(r["payload"])) for r in rows]
        return sorted(areas, key=lambda a: (a.order_index, a.label))

    def assign(self, nugget_id: str, area_id: str | None) -> None:
        existing = self.get_nugget(nugget_id)
        if existing is None:
            return
        updated = existing.with_area(area_id)
        db = self._db()
        db.execute(
            "UPDATE nuggets SET payload = ? WHERE id = ?",
            (_nugget_to_payload(updated), nugget_id),
        )
        db.commit()

    def set_difficulty(self, nugget_id: str, difficulty: str) -> None:
        existing = self.get_nugget(nugget_id)
        if existing is None:
            return
        updated = existing.with_difficulty(difficulty)
        db = self._db()
        db.execute(
            "UPDATE nuggets SET payload = ? WHERE id = ?",
            (_nugget_to_payload(updated), nugget_id),
        )
        db.commit()
