"""Tests for the persistent analytics snapshot store (G22).

The store is the foundation for real trend data and for the KB join
contract: nuggets resolve to performance history by (platform, post_id).
"""

import asyncio
from unittest.mock import patch

from xpst.analytics import AnalyticsCollector
from xpst.analytics_store import AnalyticsStore


def _store(tmp_path) -> AnalyticsStore:
    return AnalyticsStore(tmp_path / "analytics.db")


class TestAnalyticsStore:
    def test_analytics_persists_snapshot(self, tmp_path):
        store = _store(tmp_path)
        written = store.record_snapshots([
            {
                "platform": "youtube", "post_id": "vid1",
                "views": 100, "likes": 10, "comments": 2, "shares": 1,
                "timestamp": "2026-06-11T10:00:00+00:00",
            }
        ])
        assert written == 1
        assert store.snapshot_count() == 1
        latest = store.latest("youtube")
        assert latest[0]["post_id"] == "vid1"
        assert latest[0]["views"] == 100

    def test_history_is_append_only_and_ordered(self, tmp_path):
        store = _store(tmp_path)
        for hour, views in [(10, 100), (11, 150), (12, 230)]:
            store.record_snapshots([
                {
                    "platform": "x", "post_id": "tw1", "views": views,
                    "timestamp": f"2026-06-11T{hour}:00:00+00:00",
                }
            ])
        history = store.history("x", "tw1")
        assert [h["views"] for h in history] == [230, 150, 100]
        assert store.snapshot_count() == 3

    def test_latest_returns_one_row_per_post(self, tmp_path):
        store = _store(tmp_path)
        store.record_snapshots([
            {"platform": "x", "post_id": "a", "views": 1, "timestamp": "2026-06-11T10:00:00+00:00"},
            {"platform": "x", "post_id": "a", "views": 5, "timestamp": "2026-06-11T11:00:00+00:00"},
            {"platform": "youtube", "post_id": "b", "views": 9, "timestamp": "2026-06-11T11:00:00+00:00"},
        ])
        latest = store.latest()
        assert len(latest) == 2
        by_post = {(r["platform"], r["post_id"]): r["views"] for r in latest}
        assert by_post[("x", "a")] == 5
        assert by_post[("youtube", "b")] == 9

    def test_platform_specific_fields_survive_in_extra(self, tmp_path):
        store = _store(tmp_path)
        store.record_snapshots([
            {
                "platform": "instagram", "post_id": "m1", "views": 7,
                "saves": 3, "quotes": 4,
                "timestamp": "2026-06-11T10:00:00+00:00",
            }
        ])
        row = store.latest("instagram")[0]
        assert row["saves"] == 3
        assert row["quotes"] == 4  # preserved through the extra JSON column

    def test_rows_without_identity_are_skipped(self, tmp_path):
        store = _store(tmp_path)
        written = store.record_snapshots([
            {"views": 5},
            {"platform": "x", "views": 5},
            {"platform": "x", "post_id": "ok", "views": 5},
        ])
        assert written == 1

    def test_same_timestamp_replaces_not_duplicates(self, tmp_path):
        store = _store(tmp_path)
        row = {"platform": "x", "post_id": "t", "views": 1, "timestamp": "2026-06-11T10:00:00+00:00"}
        store.record_snapshots([row])
        store.record_snapshots([dict(row, views=2)])
        assert store.snapshot_count() == 1
        assert store.latest("x")[0]["views"] == 2


class TestCollectorPersistence:
    def test_collect_all_records_snapshots(self, tmp_path):
        """collect_all must append snapshots for every collected post (G22)."""
        collector = AnalyticsCollector(config_dir=str(tmp_path))

        async def fake_platform(platform, ids):
            return {
                pid: {"views": 42, "likes": 4, "timestamp": "2026-06-11T10:00:00+00:00"}
                for pid in ids
            }

        with patch.object(collector, "_collect_platform", side_effect=fake_platform):
            data = asyncio.run(collector.collect_all({"youtube": ["v1", "v2"]}))

        assert set(data["youtube"]) == {"v1", "v2"}
        assert collector.store.snapshot_count() == 2
        assert collector.store.history("youtube", "v1")[0]["views"] == 42

    def test_store_lives_inside_config_dir(self, tmp_path):
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        assert collector.store.db_path == tmp_path / "analytics.db"
