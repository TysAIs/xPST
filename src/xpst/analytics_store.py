"""Persistent per-post analytics snapshots (G22).

Append-only SQLite store keyed on ``(platform, post_id, captured_at)``.
This is the foundation for trends ("vs last week" from real history, not
fabricated multipliers) and for knowledge-base performance weighting.

JOIN CONTRACT (co-designed with the KB Nugget model): a knowledge nugget
resolves to its performance history through ``(source_platform,
source_post_id)`` → ``metric_snapshots(platform, post_id)``. Keep this key
stable; the roadmap's analytics-weighted retrieval depends on it.

Uses stdlib sqlite3 — no new dependency (anti-bloat constraint).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)

_CORE_FIELDS = ("views", "likes", "comments", "shares", "reposts", "saves")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_snapshots (
    platform    TEXT NOT NULL,
    post_id     TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    views       INTEGER,
    likes       INTEGER,
    comments    INTEGER,
    shares      INTEGER,
    reposts     INTEGER,
    saves       INTEGER,
    extra       TEXT,
    PRIMARY KEY (platform, post_id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_post
    ON metric_snapshots (platform, post_id);
"""


class AnalyticsStore:
    """Append-only store of per-post metric snapshots."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = Path("~/.xpst/analytics.db").expanduser()
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def record_snapshots(self, rows: list[dict[str, Any]]) -> int:
        """Persist one snapshot per row. Returns the number of rows written.

        Each row needs ``platform`` and ``post_id``; ``timestamp`` (ISO 8601)
        is used as ``captured_at`` when present, else now. Unknown keys are
        preserved in the ``extra`` JSON column so platform-specific fields
        (quotes, story metrics, ...) survive schema evolution.
        """
        written = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for row in rows:
                platform = row.get("platform")
                post_id = row.get("post_id")
                if not platform or not post_id:
                    continue
                extra = {
                    k: v
                    for k, v in row.items()
                    if k not in (*_CORE_FIELDS, "platform", "post_id", "timestamp")
                }
                conn.execute(
                    """
                    INSERT OR REPLACE INTO metric_snapshots
                    (platform, post_id, captured_at, views, likes, comments,
                     shares, reposts, saves, extra)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(platform),
                        str(post_id),
                        str(row.get("timestamp") or now),
                        *(row.get(f) for f in _CORE_FIELDS),
                        json.dumps(extra, default=str) if extra else None,
                    ),
                )
                written += 1
        if written:
            logger.debug("Persisted %d analytics snapshots", written)
        return written

    def latest(self, platform: str | None = None) -> list[dict[str, Any]]:
        """Latest snapshot per post, optionally filtered by platform."""
        query = """
            SELECT s.* FROM metric_snapshots s
            JOIN (
                SELECT platform, post_id, MAX(captured_at) AS captured_at
                FROM metric_snapshots GROUP BY platform, post_id
            ) m ON s.platform = m.platform AND s.post_id = m.post_id
               AND s.captured_at = m.captured_at
        """
        params: tuple = ()
        if platform:
            query += " WHERE s.platform = ?"
            params = (platform,)
        with self._connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(query, params)]

    def history(
        self, platform: str, post_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Snapshot history for one post, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM metric_snapshots
                WHERE platform = ? AND post_id = ?
                ORDER BY captured_at DESC LIMIT ?
                """,
                (platform, post_id, limit),
            )
            return [self._row_to_dict(r) for r in rows]

    def totals_before(self, cutoff_iso: str) -> dict[str, int] | None:
        """Sum of each core metric over the latest snapshot per post captured
        at or before ``cutoff_iso``. None when no history that old exists —
        callers show "no history yet" instead of fabricating a comparison."""
        query = """
            SELECT s.* FROM metric_snapshots s
            JOIN (
                SELECT platform, post_id, MAX(captured_at) AS captured_at
                FROM metric_snapshots
                WHERE captured_at <= ?
                GROUP BY platform, post_id
            ) m ON s.platform = m.platform AND s.post_id = m.post_id
               AND s.captured_at = m.captured_at
        """
        with self._connect() as conn:
            rows = [self._row_to_dict(r) for r in conn.execute(query, (cutoff_iso,))]
        if not rows:
            return None
        totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
        for row in rows:
            for key in totals:
                totals[key] += row.get(key) or 0
        return totals

    def snapshot_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0])

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        extra = data.pop("extra", None)
        if extra:
            try:
                data.update(json.loads(extra))
            except (ValueError, TypeError):
                pass
        return data
