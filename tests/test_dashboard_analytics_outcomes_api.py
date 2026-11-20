"""HTTP contract for ``GET /api/analytics/outcomes`` (D5).

The Analytics page must never receive a zero it cannot justify: every number
in this payload belongs to a post the account published, each carries a
``recorded``/``live`` label, and a platform with nothing reports
``totals: null`` so the page can say "No data".
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xpst.analytics import AnalyticsCollector
from xpst.analytics_store import AnalyticsStore
from xpst.dashboard.server import _create_app


@pytest.fixture(autouse=True)
def _no_report_cache(monkeypatch):
    """Each test gets a fresh report, not another test's memoized one."""
    monkeypatch.setenv("XPST_ANALYTICS_OUTCOME_TTL", "0")


def _client(tmp_path: Path) -> TestClient:
    return TestClient(_create_app(config_dir=str(tmp_path)))


def _write_state(tmp_path: Path) -> None:
    state = {
        "version": 1,
        "posted_videos": {
            "vid-1": {
                "caption": "clip",
                "downloaded_at": "2026-09-14T00:13:57+00:00",
                "posted_to": {
                    "youtube": {
                        "id": "mine",
                        "url": "https://youtube.com/shorts/mine",
                        "timestamp": "2026-09-14T00:13:57+00:00",
                    },
                    "instagram": {"id": "not-mine"},
                },
            }
        },
        "content_hashes": {},
        "health": {},
    }
    (tmp_path / "state.json").write_text(json.dumps(state))


def _seed_store(tmp_path: Path) -> None:
    AnalyticsStore(tmp_path / "analytics.db").record_snapshots(
        [
            {"platform": "youtube", "post_id": "mine", "views": 21, "likes": 2, "comments": 1},
            {"platform": "instagram", "post_id": "not-mine", "views": 5000, "likes": 900},
        ]
    )


def _own_only(monkeypatch, mapping):
    monkeypatch.setattr(
        AnalyticsCollector,
        "_get_owned_platform_ids",
        lambda self, platform: mapping.get(platform),
    )


def test_outcomes_endpoint_reports_no_data_without_zeros(tmp_path, monkeypatch):
    _own_only(monkeypatch, {})
    with _client(tmp_path) as client:
        payload = client.get("/api/analytics/outcomes").json()

    assert payload["platforms"]["youtube"]["totals"] is None
    assert payload["platforms"]["youtube"]["has_data"] is False
    assert payload["platforms"]["youtube"]["data_source_label"] == "No data"
    assert payload["data_source"] is None


def test_outcomes_endpoint_labels_recorded_rows_and_filters_foreign_ids(tmp_path, monkeypatch):
    _write_state(tmp_path)
    _seed_store(tmp_path)
    _own_only(monkeypatch, {"youtube": {"mine"}, "instagram": set()})

    with _client(tmp_path) as client:
        payload = client.get("/api/analytics/outcomes").json()

    youtube = payload["platforms"]["youtube"]
    assert youtube["totals"] == {"views": 21, "likes": 2, "comments": 1}
    assert youtube["data_source"] == "recorded"
    assert youtube["data_source_label"].startswith("Recorded")
    assert youtube["outcomes"][0]["metric_source"] == "recorded"

    instagram = payload["platforms"]["instagram"]
    assert instagram["totals"] is None
    assert payload["diagnostics"]["unowned_ids_excluded"] == ["instagram:not-mine"]


def test_outcomes_endpoint_never_reports_a_foreign_post(tmp_path, monkeypatch):
    _write_state(tmp_path)
    _seed_store(tmp_path)
    _own_only(monkeypatch, {"youtube": {"mine"}, "instagram": set()})

    with _client(tmp_path) as client:
        payload = client.get("/api/analytics/outcomes").json()

    seen_ids = [
        outcome["post_id"]
        for entry in payload["platforms"].values()
        for outcome in entry["outcomes"]
    ]
    assert "not-mine" not in seen_ids
    assert 5000 not in [entry["totals"] for entry in payload["platforms"].values()]
