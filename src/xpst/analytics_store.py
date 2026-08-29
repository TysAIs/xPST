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
from typing import TYPE_CHECKING, Any

from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

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

CREATE TABLE IF NOT EXISTS cross_post_groups (
    content_hash  TEXT PRIMARY KEY,
    video_id      TEXT NOT NULL,
    caption       TEXT,
    source_url    TEXT,
    created_at    TEXT NOT NULL,
    platforms_json TEXT NOT NULL
);
"""

_FOLLOWER_SCHEMA = """
CREATE TABLE IF NOT EXISTS follower_snapshots (
    platform    TEXT NOT NULL,
    count       INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (platform, captured_at)
);
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
            conn.executescript(_FOLLOWER_SCHEMA)

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
                extra = {k: v for k, v in row.items() if k not in (*_CORE_FIELDS, "platform", "post_id", "timestamp")}
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

    def platform_totals(self) -> dict[str, dict[str, int]]:
        """Sum core metrics over the LATEST snapshot per post, by platform.

        Same semantics as summing ``latest()`` rows in Python, but the
        aggregation runs inside SQLite (C, GIL-releasing) and returns one
        row per platform. With a 10k-post library the Python-side
        materialization of every snapshot row cost ~100ms of bytecode per
        /state request and collapsed under concurrent load (GIL starvation);
        this keeps the dashboard O(platforms) instead of O(snapshots).

        Returns:
            {platform: {views, likes, comments, shares, reposts, saves}}
        """
        query = """
            SELECT s.platform AS platform,
                   COALESCE(SUM(s.views), 0)    AS views,
                   COALESCE(SUM(s.likes), 0)    AS likes,
                   COALESCE(SUM(s.comments), 0) AS comments,
                   COALESCE(SUM(s.shares), 0)   AS shares,
                   COALESCE(SUM(s.reposts), 0)  AS reposts,
                   COALESCE(SUM(s.saves), 0)    AS saves
            FROM metric_snapshots s
            JOIN (
                SELECT platform, post_id, MAX(captured_at) AS captured_at
                FROM metric_snapshots GROUP BY platform, post_id
            ) m ON s.platform = m.platform AND s.post_id = m.post_id
               AND s.captured_at = m.captured_at
            GROUP BY s.platform
        """
        with self._connect() as conn:
            return {row["platform"]: dict(row) for row in conn.execute(query)}

    def history(self, platform: str, post_id: str, limit: int = 100) -> list[dict[str, Any]]:
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

    def get_video_metrics(
        self,
        post_ids: Sequence[str],
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        """Latest metric snapshot per (platform, post_id) for the given post ids.

        Drill-down primitive (QA wave): one video posted to N platforms has N
        platform post ids (resolved by the caller from state ``posted_to``).
        Returns the newest snapshot row for each in the same shape as
        :meth:`latest`, filtered to ``platform`` when given.
        """
        ids = [str(p) for p in post_ids if p]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        query = f"""
            SELECT s.* FROM metric_snapshots s
            JOIN (
                SELECT platform, post_id, MAX(captured_at) AS captured_at
                FROM metric_snapshots WHERE post_id IN ({placeholders})
                GROUP BY platform, post_id
            ) m ON s.platform = m.platform AND s.post_id = m.post_id
               AND s.captured_at = m.captured_at
        """
        params: list[Any] = list(ids)
        if platform:
            query += " WHERE s.platform = ?"
            params.append(platform)
        with self._connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(query, params)]

    def get_video_metrics_map(
        self,
        post_ids: Sequence[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Map post_id → latest snapshot rows across platforms (drill-down).

        Companion to :meth:`get_video_metrics` for UIs that key metrics by
        the platform post id (e.g. per-video detail panels).
        """
        result: dict[str, list[dict[str, Any]]] = {}
        for row in self.get_video_metrics(post_ids):
            result.setdefault(str(row.get("post_id") or ""), []).append(row)
        return result

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

    def delete_youtube_snapshots_not_in(self, owned_ids: set[str]) -> int:
        """Purge stale ``metric_snapshots`` rows whose id is not owned.

        Self-healing half of the ownership invariant (root-cause hardening
        for the skewed views/likes/comments dashboard aggregates): the
        persistence gate in the collector stops NEW foreign ids from being
        written, and this removes foreign rows that are ALREADY present —
        test posts, videos later deleted from the channel, or rows persisted
        before the ownership gate existed. Only ``platform='youtube'`` rows
        are touched; X/Instagram identity is governed by state.json and
        never purged here.

        Args:
            owned_ids: Verified ids on the authenticated channel's uploads
                playlist. Callers must pass a VERIFIED set — never a
                failure sentinel — or a transient ownership check failure
                would wipe the whole table.

        Returns:
            Number of rows deleted.
        """
        deleted = 0
        # A temp table avoids the sqlite parameter limit entirely and makes
        # the "channel owns nothing" case (empty set → delete all youtube
        # rows) a plain inner join, not a special branch.
        with self._connect() as conn:
            conn.execute("CREATE TEMP TABLE _owned_snap (id TEXT PRIMARY KEY)")
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO _owned_snap (id) VALUES (?)",
                    ((str(i),) for i in owned_ids),
                )
                deleted = int(
                    conn.execute(
                        """
                        DELETE FROM metric_snapshots
                        WHERE platform = 'youtube'
                          AND NOT EXISTS (
                              SELECT 1 FROM _owned_snap o
                              WHERE o.id = metric_snapshots.post_id
                          )
                        """
                    ).rowcount
                )
            finally:
                conn.execute("DROP TABLE _owned_snap")
        if deleted:
            logger.info("Purged %d stale youtube snapshot(s) not owned by the channel", deleted)
        return deleted

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

    # ── Follower snapshots ──────────────────────────────────────────────

    def record_followers(self, platform: str, count: int) -> None:
        """Record a follower count snapshot for a platform."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO follower_snapshots (platform, count, captured_at) VALUES (?, ?, ?)",
                (platform, count, now),
            )

    def latest_followers(self) -> dict[str, dict[str, Any]]:
        """Return the most recent follower count per platform.

        Returns dict: {platform: {"count": int, "captured_at": str}}
        """
        query = """
            SELECT f.* FROM follower_snapshots f
            JOIN (
                SELECT platform, MAX(captured_at) AS captured_at
                FROM follower_snapshots GROUP BY platform
            ) m ON f.platform = m.platform AND f.captured_at = m.captured_at
        """
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return {row["platform"]: {"count": row["count"], "captured_at": row["captured_at"]} for row in rows}

    def follower_history(self, platform: str, limit: int = 30) -> list[dict[str, Any]]:
        """Return follower count history for a platform, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM follower_snapshots WHERE platform = ? ORDER BY captured_at DESC LIMIT ?",
                (platform, limit),
            ).fetchall()
        return [{"count": row["count"], "captured_at": row["captured_at"]} for row in reversed(rows)]

    # ── Cross-post groups (B1) ──────────────────────────────────────────

    def record_cross_post_group(
        self,
        content_hash: str,
        video_id: str,
        caption: str | None,
        source_url: str | None,
        platforms: list[dict[str, Any]],
    ) -> None:
        """Insert or replace a cross-post group record.

        A cross-post group links every platform post that originated from the
        same source video (identified by ``content_hash``) so analytics can be
        aggregated across platforms as a single entry.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cross_post_groups
                (content_hash, video_id, caption, source_url, created_at,
                 platforms_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    content_hash,
                    video_id,
                    caption,
                    source_url,
                    now,
                    json.dumps(platforms, default=str),
                ),
            )

    def get_cross_post_groups(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Return cross-post groups newest-first with parsed platforms_json."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cross_post_groups
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._group_row_to_dict(r) for r in rows]

    def get_cross_post_group(self, content_hash: str) -> dict[str, Any] | None:
        """Return a single cross-post group by content_hash, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cross_post_groups WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        return self._group_row_to_dict(row) if row else None

    def latest_for_post(self, platform: str, post_id: str) -> list[dict[str, Any]]:
        """Return all snapshots for a single post, oldest-first.

        Ordered by ``captured_at`` ascending so the last element is the most
        recent snapshot — convenient for callers that want ``snapshots[-1]``.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM metric_snapshots
                WHERE platform = ? AND post_id = ?
                ORDER BY captured_at ASC
                """,
                (platform, post_id),
            )
            return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _group_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        raw = data.pop("platforms_json", None)
        if raw:
            try:
                data["platforms"] = json.loads(raw)
            except (ValueError, TypeError):
                data["platforms"] = []
        else:
            data["platforms"] = []
        return data

    # ── Video lineup (desktop Library / dashboard listing) ─────────────

    @staticmethod
    def post_links(platform: str, post_id: str) -> dict[str, str]:
        """Public/embed/thumbnail links for a tracked platform post.

        Returns only fields that are meaningfully derivable from the
        platform post id — YouTube is the only provider with a reliable
        embed + thumbnail URL, so X/Instagram/TikTok/Threads return
        empty ``embed_url``/``thumbnail_url``. Callers must fall back to
        open-in-browser (or a local cached thumbnail) for those.
        """
        pid = str(post_id or "").strip()
        plat = str(platform or "").lower()
        if not pid:
            return {"url": "", "embed_url": "", "thumbnail_url": ""}
        if plat == "youtube":
            return {
                "url": f"https://www.youtube.com/watch?v={pid}",
                "embed_url": f"https://www.youtube.com/embed/{pid}",
                "thumbnail_url": f"https://i.ytimg.com/vi/{pid}/hqdefault.jpg",
            }
        if plat == "x":
            return {
                "url": f"https://x.com/i/status/{pid}",
                "embed_url": "",
                "thumbnail_url": "",
            }
        if plat == "instagram":
            return {
                "url": f"https://www.instagram.com/p/{pid}/",
                "embed_url": "",
                "thumbnail_url": "",
            }
        if plat == "tiktok":
            return {
                "url": f"https://www.tiktok.com/video/{pid}",
                "embed_url": "",
                "thumbnail_url": "",
            }
        if plat == "threads":
            return {
                "url": f"https://www.threads.net/post/{pid}",
                "embed_url": "",
                "thumbnail_url": "",
            }
        return {"url": "", "embed_url": "", "thumbnail_url": ""}

    def get_lineup(self) -> list[dict[str, Any]]:
        """Latest snapshot per tracked post across every platform.

        This is the read model behind the desktop video lineup: it lists
        *every* video the analytics collector has ever recorded metrics
        for (not just today's sessions), newest snapshot first. Each
        entry carries the cleaned per-post metrics plus derived
        url/embed_url/thumbnail_url links (see ``post_links``).

        Returns:
            List of dicts with: platform, post_id, captured_at, views,
            likes, comments, shares, reposts, saves, url, embed_url,
            thumbnail_url. Sorted newest-first by captured_at.
        """
        rows = self.latest()
        lineup: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {
                key: row.get(key)
                for key in (
                    "platform",
                    "post_id",
                    "captured_at",
                    "views",
                    "likes",
                    "comments",
                    "shares",
                    "reposts",
                    "saves",
                )
            }
            entry.update(self.post_links(str(entry.get("platform") or ""), str(entry.get("post_id") or "")))
            lineup.append(entry)
        lineup.sort(key=lambda e: str(e.get("captured_at") or ""), reverse=True)
        return lineup
