"""Issue #297: a persisted collection failure must not become a stored zero.

The silent-zero defense (QA-2026-09-28 D1) only kept per-platform
``collection.status`` in memory. Once the numbers were written to
``analytics.db`` a fetch-failure zero and a genuine zero were identical
forever, so a later ``xpst analytics`` rendered last week's collection
failure as data.

These tests pin the persistence contract:

* every snapshot row carries ``collection_status`` / ``collection_error``;
* a platform whose collection failed outright (no rows at all) still records
  its outcome, because otherwise there is nowhere to hang the failure;
* an existing ledger is migrated additively — old rows stay readable and are
  reported as ``unknown``, never quietly promoted to ``ok``;
* every read path (outcome report, MCP payload, lineup, platform totals)
  surfaces the stored status.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from xpst.analytics import AnalyticsCollector
from xpst.analytics_store import (
    COLLECTION_STATUS_FAILED,
    COLLECTION_STATUS_PARTIAL,
    COLLECTION_STATUS_UNKNOWN,
    AnalyticsStore,
    collection_status_reports_real_numbers,
    default_analytics_db_path,
    normalise_collection_status,
)

#: The schema shipped before issue #297: no status columns, no runs table.
LEGACY_SCHEMA = """
CREATE TABLE metric_snapshots (
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
CREATE INDEX idx_snapshots_post ON metric_snapshots (platform, post_id);
CREATE TABLE cross_post_groups (
    content_hash  TEXT PRIMARY KEY,
    video_id      TEXT NOT NULL,
    caption       TEXT,
    source_url    TEXT,
    created_at    TEXT NOT NULL,
    platforms_json TEXT NOT NULL
);
"""


def _legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            """
            INSERT INTO metric_snapshots
            (platform, post_id, captured_at, views, likes, comments, shares,
             reposts, saves, extra)
            VALUES ('x', 'legacy-post', '2026-10-01T12:00:00+00:00', 7, 1, 0, 0,
                    NULL, NULL, '{"quote_count": 2}')
            """
        )
        conn.commit()
    finally:
        conn.close()


def _state(tmp_path, platform: str, post_id: str) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps({"posted_videos": {"v1": {"posted_to": {platform: {"id": post_id}}}}}),
        encoding="utf-8",
    )


def _columns(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(metric_snapshots)")}
    finally:
        conn.close()


def test_legacy_ledger_is_migrated_additively_and_rows_read_as_unknown(tmp_path):
    db = tmp_path / "analytics.db"
    _legacy_db(db)
    assert "collection_status" not in _columns(db)

    store = AnalyticsStore(db)

    assert {"collection_status", "collection_error"} <= _columns(db)
    assert store.snapshot_count() == 1
    row = store.latest("x")[0]
    # The numbers survive untouched...
    assert (row["post_id"], row["views"], row["likes"]) == ("legacy-post", 7, 1)
    assert row["quote_count"] == 2  # extra JSON still readable
    # ...but the row can never claim a successful collection.
    assert row["collection_status"] == COLLECTION_STATUS_UNKNOWN
    assert row["collection_error"] is None
    assert store.latest_collection_runs() == {}


def test_migration_is_idempotent_over_repeated_construction(tmp_path):
    db = tmp_path / "analytics.db"
    _legacy_db(db)

    stores = [AnalyticsStore(db) for _ in range(3)]
    store = stores[-1]

    assert store.snapshot_count() == 1
    assert store.record_snapshots(
        [
            {
                "platform": "x",
                "post_id": "new-post",
                "views": 3,
                "collection_status": "ok",
            }
        ]
    ) == 1
    statuses = {row["post_id"]: row["collection_status"] for row in store.latest("x")}
    assert statuses == {"legacy-post": "unknown", "new-post": "ok"}


def test_record_snapshots_persists_status_and_error(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [
            {
                "platform": "instagram",
                "post_id": "ig-1",
                "views": 0,
                "collection_status": COLLECTION_STATUS_FAILED,
                "collection_error": "403 Forbidden from i.instagram.com",
                "timestamp": "2026-10-02T09:00:00+00:00",
            }
        ]
    )

    row = store.latest("instagram")[0]
    assert row["collection_status"] == COLLECTION_STATUS_FAILED
    assert "403 Forbidden" in row["collection_error"]
    # Provenance is a real column, not duplicated into the extra blob.
    assert "collection_status" not in row or row["collection_status"] == "failed"

    history = store.history("instagram", "ig-1")
    assert history[0]["collection_status"] == COLLECTION_STATUS_FAILED
    lineup = store.get_lineup()
    assert lineup[0]["collection_status"] == COLLECTION_STATUS_FAILED


def test_unlabelled_snapshot_is_stored_as_unknown_not_ok(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([{"platform": "x", "post_id": "p1", "views": 4}])

    row = store.latest("x")[0]
    assert row["collection_status"] == COLLECTION_STATUS_UNKNOWN
    assert normalise_collection_status(None) == "unknown"
    assert normalise_collection_status("") == "unknown"
    assert normalise_collection_status("something-new") == "unknown"
    assert normalise_collection_status("OK") == "ok"
    assert collection_status_reports_real_numbers("ok") is True
    assert collection_status_reports_real_numbers("partial") is True
    assert collection_status_reports_real_numbers("failed") is False
    assert collection_status_reports_real_numbers(None) is False


@pytest.mark.asyncio
async def test_total_failure_is_persisted_and_surfaces_in_a_later_read(tmp_path):
    """The defect: a failed platform wrote no rows, so the failure vanished."""
    _state(tmp_path, "x", "p1")
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    with patch.object(
        collector,
        "_collect_platform",
        AsyncMock(side_effect=RuntimeError("upstream broke")),
    ):
        data = await collector.collect_all({"x": ["p1"]})

    assert data["x"] == {}
    assert collector.store.latest() == []

    runs = collector.store.latest_collection_runs()
    assert runs["x"]["status"] == COLLECTION_STATUS_FAILED
    assert "upstream broke" in runs["x"]["error"]
    assert runs["x"]["failures"] == 1

    # A NEW process (no in-memory error surface) reads the ledger instead of
    # inventing a successful collection.
    fresh = AnalyticsCollector(config_dir=str(tmp_path))
    report = fresh.outcome_report()
    x_entry = report["platforms"]["x"]
    assert x_entry["collection_status"] == COLLECTION_STATUS_FAILED
    assert x_entry["collection_status_source"] == "recorded"
    assert x_entry["last_collection_attempt_at"] is not None
    assert x_entry["has_data"] is False
    assert "upstream broke" in (x_entry["note"] or "")
    assert "x" in report["diagnostics"]["untrusted_recorded_platforms"]
    assert "x" not in report["diagnostics"]["unknown_collection_status_platforms"]


@pytest.mark.asyncio
async def test_partial_collection_is_labelled_on_the_row_and_the_report(tmp_path):
    _state(tmp_path, "x", "p1")
    collector = AnalyticsCollector(config_dir=str(tmp_path))

    async def partial(platform: str, ids: list[str]) -> dict:
        collector._count_collect_attempt(platform)
        collector._record_collect_failure(platform, RuntimeError("second post failed"))
        collector._collect_errors[platform]["requested"] = 2
        collector._collect_errors[platform]["collected"] = 1
        return {"p1": {"platform": platform, "post_id": "p1", "views": 3}}

    with patch.object(collector, "_collect_platform", side_effect=partial):
        await collector.collect_all({"x": ["p1", "p2"]})

    row = collector.store.latest("x")[0]
    assert row["collection_status"] == COLLECTION_STATUS_PARTIAL
    assert "second post failed" in row["collection_error"]

    fresh = AnalyticsCollector(config_dir=str(tmp_path))
    x_entry = fresh.outcome_report()["platforms"]["x"]
    assert x_entry["collection_status"] == COLLECTION_STATUS_PARTIAL
    assert x_entry["has_data"] is True
    # The partial numbers are real but incomplete: the report says so.
    assert x_entry["totals"]["views"] == 3
    assert x_entry["collection_status_label"].startswith("Collection partial")


def test_platform_totals_ignores_failed_rows_but_keeps_unknown_rows(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [
            {"platform": "x", "post_id": "failed", "views": 999, "collection_status": "failed"},
            {"platform": "x", "post_id": "legacy", "views": 5},
            {"platform": "x", "post_id": "ok", "views": 2, "collection_status": "ok"},
        ]
    )

    totals = store.platform_totals()["x"]
    assert totals["views"] == 7  # 999 from the failed fetch is not a measurement


@pytest.mark.asyncio
async def test_mcp_analytics_payload_carries_collection_status(tmp_path):
    from xpst.config import XPSTConfig
    from xpst.mcp.server import _handle_analytics

    _state(tmp_path, "x", "p1")
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [
            {
                "platform": "x",
                "post_id": "p1",
                "views": 11,
                "collection_status": COLLECTION_STATUS_PARTIAL,
                "collection_error": "one post failed",
            }
        ]
    )
    store.record_collection_runs(
        [
            {
                "platform": "x",
                "status": COLLECTION_STATUS_PARTIAL,
                "error": "one post failed",
                "attempted": 1,
                "failures": 1,
                "requested": 2,
                "collected": 1,
            }
        ]
    )

    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    result = await _handle_analytics(config, {})
    payload = json.loads(result.content[0].text)

    assert payload["platforms"]["x"]["collection_status"] == COLLECTION_STATUS_PARTIAL
    assert payload["platforms"]["x"]["collection_error"]["error"] == "one post failed"
    assert payload["collection_status"]["x"]["status"] == COLLECTION_STATUS_PARTIAL
    assert payload["collection_status"]["x"]["error"] == "one post failed"
    assert payload["posts"][0]["collection_status"] == COLLECTION_STATUS_PARTIAL
    # No platform is upgraded to "ok" without evidence.
    assert payload["collection_status"]["youtube"]["status"] == COLLECTION_STATUS_UNKNOWN
    assert payload["collection_status"]["youtube"]["source"] is None


def test_zero_arg_store_keeps_run_records_in_the_config_dir(tmp_path):
    """The known trap: the default ledger is the config dir, never ~/.xpst."""
    store = AnalyticsStore()
    assert store.db_path == default_analytics_db_path()
    assert store.db_path.parent == default_analytics_db_path().parent

    store.record_collection_runs(
        [{"platform": "instagram", "status": "failed", "error": "session expired"}]
    )

    reopened = AnalyticsStore(store.db_path)
    assert reopened.latest_collection_runs()["instagram"]["status"] == "failed"
    assert reopened.latest_collection_runs()["instagram"]["error"] == "session expired"


def test_best_time_ignores_failed_rows(tmp_path):
    """Derived views must not learn a posting time from a failed fetch."""
    from xpst.best_time import BestTimeAnalyzer

    db = tmp_path / "analytics.db"
    store = AnalyticsStore(db)
    store.record_snapshots(
        [
            {
                "platform": "x",
                "post_id": "good",
                "views": 10,
                "likes": 1,
                "timestamp": "2026-11-10T09:00:00+00:00",
                "collection_status": "ok",
            }
        ]
    )
    assert BestTimeAnalyzer(db).analyze("x")[0]["avg_engagement_rate"] == 10.0

    store.record_snapshots(
        [
            {
                "platform": "x",
                "post_id": "failed",
                "views": 100,
                "likes": 0,
                "timestamp": "2026-11-10T09:30:00+00:00",
                "collection_status": "failed",
            }
        ]
    )
    slots = BestTimeAnalyzer(db).analyze("x")
    assert slots[0]["avg_engagement_rate"] == 10.0
    assert slots[0]["post_count"] == 1


def test_best_time_still_reads_an_unmigrated_legacy_ledger(tmp_path):
    """Read-only tools must not break on a ledger the migration hasn't seen."""
    from xpst.best_time import BestTimeAnalyzer

    db = tmp_path / "analytics.db"
    _legacy_db(db)
    assert "collection_status" not in _columns(db)

    slots = BestTimeAnalyzer(db).analyze("x")
    assert slots
    assert slots[0]["avg_engagement_rate"] == round(1 / 7 * 100, 1)
