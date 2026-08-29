"""QA adversarial regression tests: dashboard webview/auth/concurrency wave.

Covers findings from the 2026-08-28 adversarial QA pass on the Tauri
dashboard surface:

* /state must not 500 when state.json carries timezone-aware or malformed
  timestamps (one bad entry used to kill the whole dashboard summary).
* _parse_ts normalizes aware timestamps to naive UTC.
* AnalyticsCollector._store() caches one AnalyticsStore per db path (the
  old per-call construction re-ran CREATE TABLE DDL — a SQLite write lock —
  several times per request, serializing concurrent dashboard + CLI load).
* Auth security matrix: every non-exempt route 401s unauthenticated and
  with bad credentials; only /health, /metrics, /bio, /oauth/callback are
  exempt.
"""

import json
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest

from xpst.dashboard.analytics import AnalyticsCollector, _parse_ts

from .test_dashboard import (  # reuse helpers
    _auth_headers,
    _make_config,
)

# ──────────────────────────────────────────────
# Timezone-aware / malformed timestamp hardening
# ──────────────────────────────────────────────


def test_parse_ts_normalizes_aware_to_naive_utc():
    """An aware timestamp is converted to naive UTC, never returned aware."""
    aware = "2026-08-20T12:00:00+02:00"
    dt = _parse_ts(aware)
    assert dt is not None
    assert dt.tzinfo is None
    assert dt == datetime(2026, 8, 20, 10, 0, 0)


def test_parse_ts_garbage_and_none():
    assert _parse_ts(None) is None
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None
    assert _parse_ts(12345) is None


def test_state_endpoint_survives_aware_and_malformed_timestamps(tmp_path):
    """One aware/malformed downloaded_at must not 500 the whole /state.

    Regression: get_summary_stats compared naive datetime.now() against
    aware _parse_ts output → TypeError → HTTP 500 for the whole dashboard.
    """
    cfg_dir = _make_config(tmp_path)
    state = {
        "posted_videos": {
            "vid-aware": {
                "caption": "aware",
                "downloaded_at": "2026-08-20T12:00:00+00:00",
                "posted_to": {"youtube": {"post_id": "p1"}},
            },
            "vid-garbage": {
                "caption": "garbage",
                "downloaded_at": "yesterday-ish",
                "posted_to": {},
            },
            "vid-empty": {"caption": "empty", "downloaded_at": "", "posted_to": {}},
        },
        "health": {"platforms": {"youtube": {"status": "ok"}}, "total_processed": 3},
    }
    from pathlib import Path

    (Path(cfg_dir) / "state.json").write_text(json.dumps(state), encoding="utf-8")
    client = _bio_app_from_dir(cfg_dir)
    resp = client.get("/state")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_posts"] == 3


def test_relative_time_handles_aware_timestamp():
    from xpst.dashboard.analytics import _relative_time

    recent = (datetime.now(timezone.utc) + timedelta(minutes=-2)).isoformat()
    assert "m ago" in _relative_time(recent) or _relative_time(recent) == "just now"


def test_posts_over_time_survives_aware_timestamps(tmp_path):
    """get_posts_over_time used the same naive/aware comparison — no crash."""
    cfg_dir = _make_config(tmp_path)
    state = {
        "posted_videos": {
            "vid-aware": {
                "caption": "aware",
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "posted_to": {"youtube": {"post_id": "p1"}},
            },
        },
        "health": {"platforms": {}, "total_processed": 1},
    }
    from pathlib import Path

    (Path(cfg_dir) / "state.json").write_text(json.dumps(state), encoding="utf-8")
    collector = AnalyticsCollector(cfg_dir)
    counts = collector.get_posts_over_time(days=7)
    assert sum(counts.values()) == 1


# ──────────────────────────────────────────────
# AnalyticsStore caching (concurrency/latency)
# ──────────────────────────────────────────────


def test_store_is_cached_per_collector(tmp_path):
    """_store() must reuse one AnalyticsStore per db path, not rebuild."""
    c1 = AnalyticsCollector(str(tmp_path))
    c2 = AnalyticsCollector(str(tmp_path))
    s1a, s1b = c1._store(), c1._store()
    s2 = c2._store()
    assert s1a is s1b, "same collector must return the same store instance"
    assert s1a is not s2, "different collectors use different caches (config-scoped)"


def test_store_cache_respects_config_dir(tmp_path):
    a = AnalyticsCollector(str(tmp_path / "a"))
    b = AnalyticsCollector(str(tmp_path / "b"))
    assert a._store().db_path != b._store().db_path


def test_cached_summary_stats_recomputes_when_state_changes(tmp_path):
    """Fingerprint memoization must not serve stale counts after a write."""

    import time as _time

    from xpst.dashboard.analytics import cached_summary_stats

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    state_path = cfg / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "posted_videos": {"a": {"caption": "x", "downloaded_at": "", "posted_to": {}}},
                "health": {"platforms": {}, "total_processed": 1},
            }
        ),
        encoding="utf-8",
    )
    first = cached_summary_stats(str(cfg))
    assert first["total_posts"] == 1
    _time.sleep(0.01)  # ensure mtime_ns differs
    state_path.write_text(
        json.dumps(
            {
                "posted_videos": {
                    "a": {"caption": "x", "downloaded_at": "", "posted_to": {}},
                    "b": {"caption": "y", "downloaded_at": "", "posted_to": {}},
                },
                "health": {"platforms": {}, "total_processed": 2},
            }
        ),
        encoding="utf-8",
    )
    second = cached_summary_stats(str(cfg))
    assert second["total_posts"] == 2, "stale cache served after state.json changed"


def test_cached_summary_stats_thread_safety(tmp_path):
    """Concurrent cold calls share one computation and all get valid data."""
    import concurrent.futures

    from xpst.dashboard.analytics import cached_summary_stats

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "state.json").write_text(
        json.dumps(
            {
                "posted_videos": {},
                "health": {"platforms": {"youtube": {"status": "ok"}}, "total_processed": 0},
            }
        ),
        encoding="utf-8",
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: cached_summary_stats(str(cfg)), range(24)))
    assert all(r["total_posts"] == 0 for r in results)
    assert all(r["platform_health"]["youtube"]["status"] == "ok" for r in results)


# ──────────────────────────────────────────────
# Auth security matrix (route × auth-status)
# ──────────────────────────────────────────────

EXEMPT = {"/health", "/metrics", "/bio", "/oauth/callback"}
PROTECTED = ["/", "/state", "/bio/edit", "/openapi.json", "/docs"]


def _bio_app_from_dir(cfg_dir):
    from fastapi.testclient import TestClient

    from xpst.dashboard.server import _create_app

    return TestClient(_create_app(cfg_dir))


@pytest.mark.parametrize("path", PROTECTED)
def test_every_non_exempt_route_rejects_anonymous(tmp_path, path):
    """Any route outside the exempt set must 401 without credentials."""
    cfg_dir = _make_config(tmp_path, auth=("admin", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()))
    client = _bio_app_from_dir(cfg_dir)
    resp = client.get(path) if path != "/bio/edit" else client.get(path)
    assert resp.status_code == 401, f"{path} returned {resp.status_code} anonymously"


@pytest.mark.parametrize("path", PROTECTED)
def test_every_non_exempt_route_rejects_bad_password(tmp_path, path):
    cfg_dir = _make_config(tmp_path, auth=("admin", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()))
    client = _bio_app_from_dir(cfg_dir)
    headers = _auth_headers(pwd="wrong")
    assert client.get(path, headers=headers).status_code == 401


@pytest.mark.parametrize("path", ["/", "/state", "/bio/edit"])
def test_valid_credentials_pass_and_slash_variants_do_not_bypass(tmp_path, path):
    """Valid creds 200; trailing-slash / double-slash / case tricks stay 401."""
    cfg_dir = _make_config(tmp_path, auth=("admin", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()))
    client = _bio_app_from_dir(cfg_dir)
    assert client.get(path, headers=_auth_headers()).status_code == 200
    for variant in (path + "/", "//" + path.lstrip("/"), "/" + path.lstrip("/").upper()):
        if variant == path:
            continue
        resp = client.get(variant)
        assert resp.status_code in (401, 404), f"bypass candidate {variant} unexpectedly returned {resp.status_code}"


def test_exempt_routes_are_exactly_the_documented_set(tmp_path):
    """/health /metrics /bio /oauth/callback answer anonymously; nothing else."""
    cfg_dir = _make_config(tmp_path, auth=("admin", bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()))
    client = _bio_app_from_dir(cfg_dir)
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.get("/bio").status_code == 200
    resp = client.post("/oauth/callback", json={"source": "test", "url": "xpst://callback?code=c&state=s"})
    assert resp.status_code == 200


# ──────────────────────────────────────────────
# CSP hardening (webview navigates to http:// origin)
# ──────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/bio", "/health", "/state"])
def test_engine_responses_carry_csp(tmp_path, path):
    """Every engine response must carry Content-Security-Policy.

    The webview navigates to http://127.0.0.1:<port> — the Tauri CSP in
    tauri.conf.json does NOT apply to that origin, so without a server-side
    header the dashboard ran with no script restrictions at all.
    """
    cfg_dir = _make_config(tmp_path)
    client = _bio_app_from_dir(cfg_dir)
    headers = _auth_headers() if path in ("/", "/state") else {}
    resp = client.get(path, headers=headers)
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp, f"no CSP on {path}: {dict(resp.headers)}"
    assert "script-src 'none'" in csp
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_relative_time_uses_utc_not_local():
    """A timestamp exactly 2h before NOW (UTC) must read '2h ago' regardless
    of the machine's local timezone — the old local-naive now() skewed every
    label by the UTC offset (audit analytics-accuracy-2026-08-28)."""
    from datetime import datetime, timedelta, timezone

    from xpst.dashboard.analytics import _relative_time

    two_hours_ago_utc = datetime.now(timezone.utc) - timedelta(hours=2)
    assert _relative_time(two_hours_ago_utc.isoformat()) == "2h ago"

    # Naive stamps are interpreted as UTC by _parse_ts.
    naive = datetime.utcnow() - timedelta(minutes=30)
    assert _relative_time(naive.isoformat()) == "30m ago"
