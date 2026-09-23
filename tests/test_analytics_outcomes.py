"""Outcome-sourced analytics (D5): ownership, recorded-vs-live, and no-data.

Three rules are protected here, because each one was a real way the numbers
lied to Tyler:

1. every number traces to a post the account actually owns — a foreign (or
   since-deleted) post id contributes nothing, and unverifiable ownership
   fails closed instead of guessing;
2. a metric is labelled ``live`` only when it was fetched from the platform
   API during this run, and ``recorded`` when it is the last persisted
   snapshot — never a value with no provenance;
3. a platform with nothing to show reports ``totals: None`` (rendered "No
   data"), never a real zero, and a metric a platform cannot report is omitted
   rather than shown as 0.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xpst.analytics import AnalyticsCollector
from xpst.analytics_outcomes import build_outcome_report

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _state(**platform_posts) -> dict:
    """State.json shape with one posted video per platform keyword."""
    posted_to = {}
    for platform, info in platform_posts.items():
        posted_to[platform] = info
    return {
        "posted_videos": {
            "vid-1": {
                "caption": "clip",
                "downloaded_at": "2026-09-14T00:13:57+00:00",
                "posted_to": posted_to,
            }
        }
    }


def _snapshot(platform: str, post_id: str, captured_at: str = "2026-09-14T14:21:17+00:00", **metrics):
    return {"platform": platform, "post_id": post_id, "captured_at": captured_at, **metrics}


# ── 1. Ownership ─────────────────────────────────────────────────────────


class TestOwnershipFiltering:
    def test_foreign_post_id_is_excluded_from_every_number(self):
        report = build_outcome_report(
            state=_state(instagram={"id": "foreign-1", "url": "https://ig/x"}),
            snapshots=[_snapshot("instagram", "foreign-1", views=999, likes=99)],
            owned_ids={"instagram": {"owned-1"}},
            now=NOW,
        )
        entry = report["platforms"]["instagram"]
        assert entry["outcomes"] == []
        assert entry["totals"] is None
        assert entry["has_data"] is False
        assert "instagram:foreign-1" in report["diagnostics"]["unowned_ids_excluded"]

    def test_owned_post_is_counted(self):
        report = build_outcome_report(
            state=_state(instagram={"id": "owned-1", "url": "https://ig/owned"}),
            snapshots=[_snapshot("instagram", "owned-1", views=12, likes=3, comments=1, shares=2, saves=4)],
            owned_ids={"instagram": {"owned-1"}},
            now=NOW,
        )
        entry = report["platforms"]["instagram"]
        assert entry["totals"] == {"views": 12, "likes": 3, "comments": 1, "shares": 2, "saves": 4}
        assert [o["post_id"] for o in entry["outcomes"]] == ["owned-1"]

    def test_unverifiable_ownership_fails_closed(self):
        """A platform whose ownership could not be verified shows nothing."""
        report = build_outcome_report(
            state=_state(x={"id": "tweet-1"}),
            snapshots=[_snapshot("x", "tweet-1", views=50)],
            owned_ids={"x": None},
            now=NOW,
        )
        entry = report["platforms"]["x"]
        assert entry["ownership_checked"] is True
        assert entry["ownership_verified"] is False
        assert entry["totals"] is None
        assert entry["outcomes"] == []
        assert "x" in report["diagnostics"]["ownership_unverified_platforms"]

    def test_snapshot_for_unowned_id_never_enters_the_report(self):
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("youtube", "someone-elses-video", views=12345)],
            owned_ids={"youtube": {"mine"}},
            now=NOW,
        )
        entry = report["platforms"]["youtube"]
        assert entry["outcomes"] == []
        assert entry["totals"] is None

    def test_owned_snapshot_without_a_publish_record_is_labelled(self):
        """Videos uploaded outside xPST still count — they are proven owned."""
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("youtube", "mine", views=7)],
            owned_ids={"youtube": {"mine"}},
            now=NOW,
        )
        outcome = report["platforms"]["youtube"]["outcomes"][0]
        assert outcome["outcome_source"] == "snapshot"
        assert outcome["status"] == "published"
        assert report["platforms"]["youtube"]["totals"] == {"views": 7}


# ── 2. Recorded vs live ──────────────────────────────────────────────────


class TestRecordedVsLiveLabelling:
    def test_snapshot_rows_are_labelled_recorded(self):
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("youtube", "vid", views=5)],
            owned_ids={"youtube": {"vid"}},
            now=NOW,
        )
        entry = report["platforms"]["youtube"]
        assert entry["data_source"] == "recorded"
        assert entry["data_source_label"].startswith("Recorded")
        assert entry["outcomes"][0]["metric_source"] == "recorded"
        assert entry["outcomes"][0]["captured_at"] == "2026-09-14T14:21:17+00:00"
        assert report["live"] is False

    def test_live_rows_are_labelled_live_and_win_over_the_snapshot(self):
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("youtube", "vid", views=5)],
            live_rows=[_snapshot("youtube", "vid", captured_at="2026-09-15T11:59:00+00:00", views=9)],
            owned_ids={"youtube": {"vid"}},
            now=NOW,
        )
        entry = report["platforms"]["youtube"]
        assert entry["data_source"] == "live"
        assert entry["data_source_label"] == "Live (fetched now)"
        assert entry["totals"] == {"views": 9}
        assert entry["outcomes"][0]["metric_source"] == "live"
        assert report["live"] is True
        assert report["data_source"] == "live"

    def test_stale_recorded_snapshot_says_so(self):
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("youtube", "vid", captured_at="2026-09-01T00:00:00+00:00", views=5)],
            owned_ids={"youtube": {"vid"}},
            now=NOW,
        )
        entry = report["platforms"]["youtube"]
        assert entry["staleness"] == "stale"
        assert entry["data_source_label"] == "Recorded (stale)"

    def test_a_failed_collection_keeps_the_recorded_label(self):
        """Only rows that actually came back from the API get the live label."""
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("x", "tweet", views=4)],
            live_rows=[],
            owned_ids={"x": {"tweet"}},
            now=NOW,
        )
        entry = report["platforms"]["x"]
        assert entry["data_source"] == "recorded"
        assert entry["outcomes"][0]["metric_source"] == "recorded"


# ── 3. No data is not zero ───────────────────────────────────────────────


class TestNoDataIsNotZero:
    def test_platform_with_nothing_reports_none_not_zero(self):
        report = build_outcome_report(state={}, snapshots=[], owned_ids={}, now=NOW)
        for platform in ("youtube", "x", "instagram", "tiktok", "threads"):
            entry = report["platforms"][platform]
            assert entry["totals"] is None
            assert entry["has_data"] is False
            assert entry["data_source"] is None
            assert entry["data_source_label"] == "No data"
            assert entry["ownership_checked"] is False
        assert report["data_source"] is None

    def test_unpublished_platforms_are_still_listed(self):
        report = build_outcome_report(
            state=_state(youtube={"id": "vid", "url": "u"}),
            snapshots=[_snapshot("youtube", "vid", views=3)],
            owned_ids={"youtube": {"vid"}},
            now=NOW,
        )
        assert "tiktok" in report["platforms"]
        assert report["platforms"]["tiktok"]["totals"] is None

    def test_metrics_the_platform_cannot_report_are_omitted(self):
        """YouTube has no share count: it must be absent, not 0."""
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("youtube", "vid", views=3)],
            owned_ids={"youtube": {"vid"}},
            now=NOW,
        )
        totals = report["platforms"]["youtube"]["totals"]
        assert totals == {"views": 3}
        metrics = report["platforms"]["youtube"]["outcomes"][0]["metrics"]
        assert "shares" not in metrics
        assert "saves" not in metrics
        assert "likes" not in metrics  # not fetched this run → omitted, never 0

    def test_a_real_zero_is_preserved(self):
        report = build_outcome_report(
            state={},
            snapshots=[_snapshot("youtube", "vid", views=0, likes=0)],
            owned_ids={"youtube": {"vid"}},
            now=NOW,
        )
        entry = report["platforms"]["youtube"]
        assert entry["has_data"] is True
        assert entry["totals"] == {"views": 0, "likes": 0}

    def test_deleted_and_failed_posts_are_listed_but_not_counted(self):
        state = {
            "posted_videos": {
                "deleted-vid": {
                    "posted_to": {"instagram": {"id": "gone", "deleted": True}},
                },
                "failed-vid": {
                    "posted_to": {},
                    "errors": {"x": {"error": "boom"}},
                },
                "ok-vid": {"posted_to": {"instagram": {"id": "live-post"}}},
            }
        }
        report = build_outcome_report(
            state=state,
            snapshots=[_snapshot("instagram", "gone", views=500), _snapshot("instagram", "live-post", views=4)],
            owned_ids={"instagram": {"gone", "live-post"}},
            now=NOW,
        )
        entry = report["platforms"]["instagram"]
        by_id = {o["post_id"]: o for o in entry["outcomes"]}
        assert by_id["gone"]["status"] == "deleted"
        assert by_id["gone"]["counted_in_totals"] is False
        assert entry["totals"] == {"views": 4}


# ── 4. Collector integration (state + store + ownership) ─────────────────


class TestCollectorOutcomeReport:
    def _write_state(self, tmp_path: Path, state: dict) -> None:
        (tmp_path / "state.json").write_text(json.dumps(state))

    def test_report_reads_state_and_store_and_gates_on_ownership(self, tmp_path):
        self._write_state(
            tmp_path,
            {
                "posted_videos": {
                    "vid-1": {
                        "posted_to": {
                            "youtube": {"id": "mine", "url": "https://youtube.com/shorts/mine"},
                            "instagram": {"id": "foreign"},
                        }
                    }
                }
            },
        )
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector.store.record_snapshots(
            [
                {"platform": "youtube", "post_id": "mine", "views": 11, "likes": 1, "comments": 0},
                {"platform": "instagram", "post_id": "foreign", "views": 4321},
            ]
        )
        collector._get_owned_platform_ids = lambda platform: {  # type: ignore[method-assign]
            "youtube": {"mine"},
            "instagram": set(),
        }.get(platform)

        report = collector.outcome_report()
        assert report["platforms"]["youtube"]["totals"] == {"views": 11, "likes": 1, "comments": 0}
        assert report["platforms"]["instagram"]["totals"] is None
        assert report["diagnostics"]["unowned_ids_excluded"] == ["instagram:foreign"]
        # Nothing in the report claims the foreign id.
        assert all(
            o["post_id"] != "foreign"
            for entry in report["platforms"].values()
            for o in entry["outcomes"]
        )

    def test_live_collection_marks_rows_live(self, tmp_path):
        self._write_state(tmp_path, {"posted_videos": {}})
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector.store.record_snapshots([{"platform": "youtube", "post_id": "mine", "views": 3}])
        collector._get_owned_platform_ids = lambda platform: {"youtube": {"mine"}}.get(platform)  # type: ignore[method-assign]

        recorded = collector.outcome_report()
        assert recorded["platforms"]["youtube"]["outcomes"][0]["metric_source"] == "recorded"

        live = collector.outcome_report(live_data={"youtube": {"mine": {"views": 8}}})
        assert live["platforms"]["youtube"]["outcomes"][0]["metric_source"] == "live"
        assert live["platforms"]["youtube"]["totals"] == {"views": 8}
        assert live["live"] is True

    def test_unverifiable_ownership_never_produces_numbers(self, tmp_path):
        self._write_state(tmp_path, {"posted_videos": {"vid": {"posted_to": {"tiktok": {"id": "tt-1"}}}}})
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector.store.record_snapshots([{"platform": "tiktok", "post_id": "tt-1", "views": 42}])
        collector._get_owned_platform_ids = lambda platform: None  # type: ignore[method-assign]

        report = collector.outcome_report()
        assert report["platforms"]["tiktok"]["totals"] is None
        assert report["platforms"]["tiktok"]["ownership_verified"] is False

    def test_platform_with_no_records_is_not_ownership_checked(self, tmp_path):
        """No state entry and no snapshot → no spare API round trip."""
        self._write_state(tmp_path, {"posted_videos": {}})
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        calls: list[str] = []
        collector._get_owned_platform_ids = lambda platform: calls.append(platform) or set()  # type: ignore[method-assign]

        report = collector.outcome_report()
        assert calls == []
        assert all(not entry["ownership_checked"] for entry in report["platforms"].values())

    def test_foreign_ids_never_reach_metric_snapshots(self, tmp_path):
        """The persistence gate, asserted through the report's own store."""
        self._write_state(tmp_path, {"posted_videos": {}})
        collector = AnalyticsCollector(config_dir=str(tmp_path))
        collector._get_owned_platform_ids = lambda platform: {"youtube": {"mine"}}.get(platform)  # type: ignore[method-assign]
        rows = [
            {"platform": "youtube", "post_id": "mine", "views": 1},
            {"platform": "youtube", "post_id": "not-mine", "views": 999},
        ]
        collector.store.record_snapshots(collector._filter_owned_snapshots(rows))
        stored = {row["post_id"] for row in collector.store.latest()}
        assert stored == {"mine"}


@pytest.mark.parametrize("platform", ["youtube", "x", "instagram", "tiktok", "threads"])
def test_every_platform_row_carries_an_explicit_source(platform):
    report = build_outcome_report(state={}, snapshots=[], owned_ids={}, now=NOW)
    entry = report["platforms"][platform]
    assert entry["data_source"] in (None, "live", "recorded")
    assert entry["data_source_label"] in ("No data", "Recorded", "Recorded (stale)", "Live (fetched now)")
