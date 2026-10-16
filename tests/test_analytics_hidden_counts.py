"""Hidden-counts honesty: missing metrics must end-to-end be null, not 0.

Follow-up to the analytics-accuracy audit (2026-08-28). YouTube omits
``likeCount``/``commentCount`` when the creator hides the counts. Every
layer — collector → store → SQL rollup → dashboard payload → cross-post
aggregates — must carry that as MISSING (None/null). A real reported zero
still sums normally: None means "nobody reported", 0 means "reported zero".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from xpst.analytics_store import AnalyticsStore
from xpst.dashboard.analytics import AnalyticsCollector as DashboardAnalytics


def _hidden_row(post_id: str, views: int = 50) -> dict[str, object]:
    """A snapshot exactly as the YouTube collector writes hidden counts:
    views present, likes/comments keys absent."""
    return {
        "platform": "youtube",
        "post_id": post_id,
        "views": views,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _reported_row(post_id: str, **values: int) -> dict[str, object]:
    row: dict[str, object] = {
        "platform": "youtube",
        "post_id": post_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    row.update(values)
    return row


# ── store layer ──────────────────────────────────────────────────────────


def test_store_stores_hidden_counts_as_sql_null(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([_hidden_row("yt-hidden")])
    row = store.latest("youtube")[0]
    assert row["views"] == 50
    # The honest DB shape: a missing column, not a 0 row.
    assert row["likes"] is None
    assert row["comments"] is None


def test_platform_totals_reports_null_for_all_hidden_never_fabricated_zero(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([_hidden_row("yt-1"), _hidden_row("yt-2")])
    totals = store.platform_totals()["youtube"]
    assert totals["views"] == 100
    assert totals["likes"] is None
    assert totals["comments"] is None


def test_platform_totals_mixes_hidden_and_reported_values(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [
            _hidden_row("yt-hidden"),
            _reported_row("yt-reported", likes=7, comments=2),
        ]
    )
    totals = store.platform_totals()["youtube"]
    # Sum over the reported subset only; a hidden post must not contribute 0.
    assert totals["likes"] == 7
    assert totals["comments"] == 2


def test_platform_totals_real_reported_zero_stays_zero(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    # 0 was actually reported — this is NOT a hidden count.
    store.record_snapshots([_reported_row("yt-zero", views=10, likes=0, comments=0)])
    totals = store.platform_totals()["youtube"]
    assert totals["likes"] == 0
    assert totals["comments"] == 0


def test_totals_before_keeps_hidden_counts_missing(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [
            {**_hidden_row("yt-1"), "timestamp": "2026-01-01T00:00:00+00:00"},
            {**_hidden_row("yt-2"), "timestamp": "2026-01-01T00:00:00+00:00"},
        ]
    )
    totals = store.totals_before("2026-06-01T00:00:00+00:00")
    assert totals is not None
    assert totals["views"] == 100
    assert totals["likes"] is None
    assert totals["comments"] is None


# ── dashboard payload layer ──────────────────────────────────────────────


def _config_dir_with_state(tmp_path, platform: str, post_id: str):
    (tmp_path / "state.json").write_text(
        json.dumps({"posted_videos": {"video-1": {"posted_to": {platform: {"id": post_id}}}}})
    )
    return tmp_path


def test_dashboard_rollup_surfaces_hidden_counts_as_null(tmp_path):
    _config_dir_with_state(tmp_path, "youtube", "yt-1")
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([_hidden_row("yt-1")])

    dashboard = DashboardAnalytics(config_dir=str(tmp_path))
    engagement = dashboard.get_engagement_from_snapshots()
    assert engagement["youtube"]["posts"] == 1
    assert engagement["youtube"]["views"] == 50
    assert engagement["youtube"]["likes"] is None
    assert engagement["youtube"]["comments"] is None

    payload = dashboard.get_analytics_payload(live=False)
    youtube = next(p for p in payload["platforms"] if p["platform"] == "youtube")
    assert youtube["total_views"] == 50
    assert youtube["total_likes"] is None
    assert youtube["total_comments"] is None
    # The cross-platform rollup inherits the missing field.
    assert payload["summary"]["total_likes"] is None
    top = payload["top_posts"][0]
    assert top["total_views"] == 50
    assert top["total_likes"] is None


def test_dashboard_rollup_mixes_hidden_and_reported_values(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "posted_videos": {
                    "video-1": {"posted_to": {"youtube": {"id": "yt-1"}}},
                    "video-2": {"posted_to": {"youtube": {"id": "yt-2"}}},
                }
            }
        )
    )
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [_hidden_row("yt-1"), _reported_row("yt-2", views=30, likes=4, comments=1)]
    )
    engagement = DashboardAnalytics(config_dir=str(tmp_path)).get_engagement_from_snapshots()
    assert engagement["youtube"]["views"] == 80
    assert engagement["youtube"]["likes"] == 4
    assert engagement["youtube"]["comments"] == 1


def test_cross_post_analytics_keeps_hidden_counts_missing(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots(
        [
            _hidden_row("yt-1"),
            {
                "platform": "x",
                "post_id": "x-1",
                "views": 20,
                "likes": 3,
                "comments": 1,
                "shares": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ]
    )
    store.record_cross_post_group(
        "hash-1",
        "video-1",
        None,
        None,
        [
            {"platform": "youtube", "post_id": "yt-1", "url": None},
            {"platform": "x", "post_id": "x-1", "url": None},
        ],
    )
    groups = DashboardAnalytics(config_dir=str(tmp_path)).get_cross_post_analytics()
    group = groups[0]
    yt = group["platforms"]["youtube"]
    assert yt["views"] == 50
    assert yt["likes"] is None
    # An engagement rate built from a fabricated 0 would be a lie.
    assert yt["engagement_rate"] is None
    assert yt["engagement_tier"] == "unknown"
    # The other platform has every component reported, so it is computable.
    x = group["platforms"]["x"]
    assert x["likes"] == 3
    assert x["engagement_rate"] == 25.0
    # Group totals: the hidden YouTube likes stay missing, so the group
    # total is the reported subset (3), and the group rate cannot honestly
    # count the hidden component → None.
    assert group["total_likes"] == 3
    assert group["total_engagement_rate"] is None
    assert group["engagement_tier"] == "unknown"


# ── staleness indicator ("as of Xh ago") ────────────────────────────────


def test_dashboard_summary_carries_freshness_indicator(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics.db")
    store.record_snapshots([_hidden_row("yt-1")])
    summary = DashboardAnalytics(config_dir=str(tmp_path)).get_summary_stats()
    assert summary["last_captured"] is not None
    assert summary["staleness_hours"] == 0.0 or summary["staleness_hours"] > 0
    assert summary["freshness"] == "fresh"
    assert summary["last_captured_relative"] == "just now"
