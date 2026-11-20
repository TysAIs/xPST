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
from xpst.utils.platform import get_config_dir

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

_CORE_FIELDS = ("views", "likes", "comments", "shares", "reposts", "saves")

#: Per-platform collection outcomes that may accompany a snapshot.
#:
#: ``ok``          — every requested post was fetched this run.
#: ``partial``     — some posts were fetched, some failed; the stored numbers
#:                   are real but INCOMPLETE.
#: ``failed``      — the collection broke; whatever numbers exist are not
#:                   evidence of anything.
#: ``unavailable`` — the platform answered "nothing to collect" with a reason
#:                   (e.g. every recorded post was deleted upstream).
#: ``not_collected`` — the platform was not attempted at all.
#:
#: ``unknown`` is the explicit default for rows written before status was
#: tracked (and for any caller that does not declare a status). It is
#: deliberately NOT ``ok``: an unlabelled row must never be reported as a
#: successful collection.
COLLECTION_STATUS_OK = "ok"
COLLECTION_STATUS_PARTIAL = "partial"
COLLECTION_STATUS_FAILED = "failed"
COLLECTION_STATUS_UNAVAILABLE = "unavailable"
COLLECTION_STATUS_NOT_COLLECTED = "not_collected"
COLLECTION_STATUS_UNKNOWN = "unknown"

KNOWN_COLLECTION_STATUSES = frozenset(
    {
        COLLECTION_STATUS_OK,
        COLLECTION_STATUS_PARTIAL,
        COLLECTION_STATUS_FAILED,
        COLLECTION_STATUS_UNAVAILABLE,
        COLLECTION_STATUS_NOT_COLLECTED,
    }
)

#: Statuses whose numbers may be shown as real measurements. ``partial`` is
#: included because the rows that WERE collected are genuine — but the totals
#: are incomplete, which is why the status is rendered next to them.
COLLECTION_STATUSES_WITH_TRUSTED_ROWS = frozenset(
    {COLLECTION_STATUS_OK, COLLECTION_STATUS_PARTIAL}
)


def normalise_collection_status(value: Any) -> str:
    """Coerce a stored/declared status to the known vocabulary.

    Missing values (legacy rows) and unrecognised text become ``"unknown"`` —
    never ``"ok"``. Reporting an unlabelled snapshot as a successful
    collection is the exact failure this module is meant to prevent.
    """
    text = str(value or "").strip().lower()
    return text if text in KNOWN_COLLECTION_STATUSES else COLLECTION_STATUS_UNKNOWN


def collection_status_reports_real_numbers(status: Any) -> bool:
    """Whether a snapshot row carrying ``status`` can be shown as real data."""
    return normalise_collection_status(status) in COLLECTION_STATUSES_WITH_TRUSTED_ROWS


def default_analytics_db_path() -> Path:
    """Return the analytics database for the active config directory.

    This is the *only* correct zero-argument location for ``analytics.db``: it
    routes through :func:`xpst.utils.platform.get_config_dir`, the documented
    single source of truth that honours the ``XPST_CONFIG_DIR`` sandbox hook.

    Why this exists (two real defects, one fix):

    * A hardcoded ``~/.xpst/analytics.db`` default meant every caller that
      built ``AnalyticsStore()`` with no path — four sites in
      ``engine.py`` and ``mcp/server.py`` — wrote into the *operator's* live
      ledger even when the config dir had been relocated. The pytest suite
      sandboxes ``XPST_CONFIG_DIR`` per test, so engine tests recorded
      cross-post groups straight into the real ``~/.xpst/analytics.db``
      (measured 2026-11-04: 7 fixture rows in a 23-row production
      ``cross_post_groups`` table, rewritten on every suite run).
    * With a relocated config dir the engine wrote its ledger to a database
      the dashboard and MCP analytics tools never read (they already resolve
      ``config.config_dir / "analytics.db"``).
    """
    return get_config_dir() / "analytics.db"


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
    collection_status TEXT,
    collection_error  TEXT,
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

CREATE TABLE IF NOT EXISTS analytics_collection_runs (
    platform    TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    reason      TEXT,
    detail      TEXT,
    attempted   INTEGER,
    failures    INTEGER,
    requested   INTEGER,
    collected   INTEGER,
    PRIMARY KEY (platform, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_collection_runs_platform
    ON analytics_collection_runs (platform, captured_at);
"""

#: Columns added after the first release of ``metric_snapshots``. Applied with
#: a guarded ``ALTER TABLE`` (see :meth:`AnalyticsStore._migrate`) so upgrading
#: over the operator's live ledger is idempotent and lossless.
_ADDITIVE_SNAPSHOT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("collection_status", "ALTER TABLE metric_snapshots ADD COLUMN collection_status TEXT"),
    ("collection_error", "ALTER TABLE metric_snapshots ADD COLUMN collection_error TEXT"),
)

_FOLLOWER_SCHEMA = """
CREATE TABLE IF NOT EXISTS follower_snapshots (
    platform    TEXT NOT NULL,
    count       INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (platform, captured_at)
);
"""


def _normalise_capture_timestamp(value: Any, fallback: str) -> str:
    """Store valid timestamps as UTC ISO-8601 strings.

    Legacy callers may provide naive timestamps; they are interpreted as UTC
    rather than compared directly with aware timestamps. Invalid legacy text
    is preserved for API compatibility, but cannot be used as a reliable
    freshness value by callers.
    """
    if not value:
        return fallback
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def _parse_capture_timestamp(value: Any) -> datetime | None:
    """Parse a stored capture timestamp into aware UTC, if valid."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _optional_int(value: Any) -> int | None:
    """Coerce a counter to ``int``, preserving "not reported" as ``None``."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AnalyticsStore:
    """Append-only store of per-post metric snapshots."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = default_analytics_db_path()
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.executescript(_FOLLOWER_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply additive schema changes to an existing ledger, idempotently.

        Runs on every construction (including the first one against the
        operator's live ``analytics.db``), so it must be safe to repeat:
        ``PRAGMA table_info`` decides whether a column already exists, and the
        only statements executed are guarded ``ALTER TABLE ... ADD COLUMN``s —
        no table rewrite, no data movement. Existing rows keep NULL statuses,
        which the read paths report as ``"unknown"`` (never ``"ok"``).
        """
        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(metric_snapshots)")}
        for column, statement in _ADDITIVE_SNAPSHOT_COLUMNS:
            if column not in existing:
                conn.execute(statement)
                logger.info("Migrated analytics ledger: added metric_snapshots.%s", column)

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

        ``collection_status`` / ``collection_error`` are stored as real
        columns, not in ``extra``, so every read path can see whether the
        numbers came from a collection that actually succeeded. A row that
        declares neither is stored as ``"unknown"`` — explicitly unlabelled,
        NEVER implicitly ``"ok"`` (issue #297: a fetch-failure zero must stay
        distinguishable from a genuine zero once it is written to disk).
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
                    if k
                    not in (
                        *_CORE_FIELDS,
                        "platform",
                        "post_id",
                        "timestamp",
                        "collection_status",
                        "collection_error",
                    )
                }
                collection_error = row.get("collection_error")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO metric_snapshots
                    (platform, post_id, captured_at, views, likes, comments,
                     shares, reposts, saves, extra, collection_status,
                     collection_error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(platform),
                        str(post_id),
                        _normalise_capture_timestamp(row.get("timestamp"), now),
                        *(row.get(f) for f in _CORE_FIELDS),
                        json.dumps(extra, default=str) if extra else None,
                        normalise_collection_status(row.get("collection_status")),
                        str(collection_error)[:240] if collection_error else None,
                    ),
                )
                written += 1
        if written:
            logger.debug("Persisted %d analytics snapshots", written)
        return written

    def record_collection_runs(self, entries: Sequence[dict[str, Any]]) -> int:
        """Persist per-platform collection outcomes, including total failures.

        A platform whose collection failed outright produces NO snapshot rows,
        so there is nowhere on ``metric_snapshots`` to hang the status. Without
        this table the failure vanished with the process, and the next
        recorded-only read happily rendered last week's numbers (issue #297).

        Each entry accepts ``platform``, ``status`` (normalised),
        ``error``/``reason``/``detail`` and the counters the collector
        already computes (``attempted``, ``failures``, ``requested``,
        ``collected``); ``captured_at`` defaults to now.
        """
        written = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for entry in entries:
                platform = str(entry.get("platform") or "").strip()
                if not platform:
                    continue
                error = entry.get("error")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO analytics_collection_runs
                    (platform, captured_at, status, error, reason, detail,
                     attempted, failures, requested, collected)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform.lower(),
                        _normalise_capture_timestamp(entry.get("captured_at"), now),
                        normalise_collection_status(entry.get("status")),
                        str(error)[:240] if error else None,
                        entry.get("reason"),
                        entry.get("detail"),
                        _optional_int(entry.get("attempted")),
                        _optional_int(entry.get("failures")),
                        _optional_int(entry.get("requested")),
                        _optional_int(entry.get("collected")),
                    ),
                )
                written += 1
        if written:
            logger.debug("Recorded %d analytics collection run(s)", written)
        return written

    def latest_collection_runs(self) -> dict[str, dict[str, Any]]:
        """Newest recorded collection run per platform.

        ``{platform: {status, error, reason, detail, attempted, failures,
        requested, collected, captured_at}}``. Platforms absent from the map
        have no recorded status at all — callers must report ``"unknown"``
        rather than assuming the last collection succeeded.
        """
        query = """
            SELECT r.* FROM analytics_collection_runs r
            JOIN (
                SELECT platform, MAX(captured_at) AS captured_at
                FROM analytics_collection_runs GROUP BY platform
            ) m ON r.platform = m.platform AND r.captured_at = m.captured_at
        """
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(query)]
        for row in rows:
            row["status"] = normalise_collection_status(row.get("status"))
        return {str(row["platform"]).lower(): row for row in rows}

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

    def platform_totals(self) -> dict[str, dict[str, int | None]]:
        """Sum core metrics over the LATEST snapshot per post, by platform.

        Same semantics as summing ``latest()`` rows in Python, but the
        aggregation runs inside SQLite (C, GIL-releasing) and returns one
        row per platform. With a 10k-post library the Python-side
        materialization of every snapshot row cost ~100ms of bytecode per
        /state request and collapsed under concurrent load (GIL starvation);
        this keeps the dashboard O(platforms) instead of O(snapshots).

        Missing counts stay missing (analytics-accuracy audit 2026-08-28,
        hidden-counts follow-up): SQLite ``SUM`` ignores NULLs, so a field
        whose every latest snapshot is NULL (e.g. likes hidden on every
        YouTube video) returns ``None`` — "no snapshot reported this" —
        NOT a fabricated real zero. A real reported zero still sums to 0.

        Rows whose ``collection_status`` is ``failed`` are excluded: a failed
        fetch produced no measurement, so its numbers must not enter a total
        (issue #297). ``unknown`` rows are still counted — they are readable
        legacy data — but callers pair this result with
        :meth:`latest_collection_runs` so the uncertainty is visible.

        Returns:
            {platform: {views, likes, comments, shares, reposts, saves}}
            with per-field values of ``int`` or ``None``.
        """
        query = """
            SELECT s.platform AS platform,
                   SUM(s.views)    AS views,
                   SUM(s.likes)    AS likes,
                   SUM(s.comments) AS comments,
                   SUM(s.shares)   AS shares,
                   SUM(s.reposts)  AS reposts,
                   SUM(s.saves)    AS saves
            FROM metric_snapshots s
            JOIN (
                SELECT platform, post_id, MAX(captured_at) AS captured_at
                FROM metric_snapshots GROUP BY platform, post_id
            ) m ON s.platform = m.platform AND s.post_id = m.post_id
               AND s.captured_at = m.captured_at
            WHERE COALESCE(s.collection_status, '') != 'failed'
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
        # Static query joined with a TEMP TABLE (repo pattern, see
        # delete_youtube_snapshots_not_in): avoids B608 string-built SQL
        # entirely — no f-string interpolation into the query text.
        with self._connect() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _post_ids (id TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _post_ids")
            conn.executemany(
                "INSERT OR IGNORE INTO _post_ids (id) VALUES (?)",
                ((i,) for i in ids),
            )
            query = """
                SELECT s.* FROM metric_snapshots s
                JOIN (
                    SELECT platform, post_id, MAX(captured_at) AS captured_at
                    FROM metric_snapshots
                    WHERE post_id IN (SELECT id FROM _post_ids)
                    GROUP BY platform, post_id
                ) m ON s.platform = m.platform AND s.post_id = m.post_id
                   AND s.captured_at = m.captured_at
            """
            params: list[Any] = []
            if platform:
                query += " WHERE s.platform = ?"
                params.append(platform)
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

    def totals_before(self, cutoff_iso: str) -> dict[str, int | None] | None:
        """Sum of each core metric over the latest snapshot per post captured
        at or before ``cutoff_iso``. None when no history that old exists —
        callers show "no history yet" instead of fabricating a comparison.
        Per-field ``None`` means no snapshot that old reported the metric
        (hidden counts stay missing, never a fabricated zero)."""
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
        totals: dict[str, int | None] = {"views": None, "likes": None, "comments": None, "shares": None}
        for row in rows:
            for key in totals:
                value = row.get(key)
                if value is not None:
                    totals[key] = (totals[key] or 0) + int(value)
        return totals

    def snapshot_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0])

    def last_captured_at(self, platform: str | None = None) -> str | None:
        """Return the newest persisted capture timestamp.

        ``None`` means no snapshots exist (or no snapshots exist for the
        requested platform). Values are parsed as UTC before selecting the
        newest one, so legacy offset-aware timestamps remain chronological.
        """
        query = "SELECT captured_at FROM metric_snapshots"
        params: tuple[str, ...] = ()
        if platform:
            query += " WHERE platform = ?"
            params = (platform,)
        with self._connect() as conn:
            values = [row[0] for row in conn.execute(query, params)]
        timestamps = [_parse_capture_timestamp(value) for value in values]
        valid = [timestamp for timestamp in timestamps if timestamp is not None]
        if not valid:
            return None
        return max(valid).isoformat()

    def delete_snapshots_not_in(self, platform: str, owned_ids: set[str] | None) -> int:
        """Purge stale rows for ``platform`` not in a verified ID set.

        The caller must only pass an ownership set obtained from a successful
        identity check. Empty or ``None`` sets are deliberately a no-op:
        absence of ownership evidence must never turn into a destructive
        ``DELETE all rows for this platform`` operation.
        """
        if not platform or not owned_ids:
            return 0
        valid_owned_ids = {
            candidate
            for item in owned_ids
            if (candidate := str(item).strip())
        }
        if not valid_owned_ids:
            return 0
        deleted = 0
        with self._connect() as conn:
            conn.execute("CREATE TEMP TABLE _owned_snap (id TEXT PRIMARY KEY)")
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO _owned_snap (id) VALUES (?)",
                    ((item,) for item in valid_owned_ids),
                )
                deleted = int(
                    conn.execute(
                        """
                        DELETE FROM metric_snapshots
                        WHERE platform = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM _owned_snap o
                              WHERE o.id = metric_snapshots.post_id
                          )
                        """,
                        (platform,),
                    ).rowcount
                )
            finally:
                conn.execute("DROP TABLE _owned_snap")
        if deleted:
            logger.info(
                "Purged %d stale %s snapshot(s) not owned by the account", deleted, platform
            )
        return deleted

    def delete_youtube_snapshots_not_in(self, owned_ids: set[str] | None) -> int:
        """Backwards-compatible YouTube-specific purge wrapper."""
        return self.delete_snapshots_not_in("youtube", owned_ids)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        extra = data.pop("extra", None)
        if extra:
            try:
                data.update(json.loads(extra))
            except (ValueError, TypeError):
                pass
        # Legacy rows (written before issue #297) have no status column value.
        # They are readable, but explicitly "unknown" — never "ok".
        data["collection_status"] = normalise_collection_status(data.get("collection_status"))
        if data.get("collection_error") is not None:
            data["collection_error"] = str(data["collection_error"])
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
            thumbnail_url, collection_status, collection_error. Sorted
            newest-first by captured_at.
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
                    # Provenance of the stored numbers (issue #297): the
                    # Library must not render a failed/unlabelled collection
                    # as a normal row.
                    "collection_status",
                    "collection_error",
                )
            }
            entry.update(self.post_links(str(entry.get("platform") or ""), str(entry.get("post_id") or "")))
            lineup.append(entry)
        lineup.sort(key=lambda e: str(e.get("captured_at") or ""), reverse=True)
        return lineup
