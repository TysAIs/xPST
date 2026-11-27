#!/usr/bin/env python3
"""Capture the six analytics-backed dashboard read routes as byte fixtures.

The Rust HTTP surface must not guess what Python's dashboard serves. This
script builds a seeded config directory (a fictional ``state.json`` plus an
``analytics.db`` created by the real ``AnalyticsStore``), freezes the clock,
drives the REAL FastAPI app through ``TestClient`` and writes each response
body verbatim — key order and separators included — under
``crates/xpst-api/tests/fixtures/analytics/expected/``.

``crates/xpst-api/tests/analytics_parity.rs`` copies that fixture into a
temporary directory, serves the same requests from the Rust router with the
same pinned clock, and compares the bodies byte for byte.

Usage (from the repo root):

    PYTHONPATH="$PWD/src" XPST_PYTHON=<venv>/bin/python \
        cargo xtask analytics-routes --write

    ... --check     # re-capture into a temp dir and diff; exit 1 on drift

Nothing here touches the network: the config dir is a temporary directory with
no credentials, and the clock is frozen so the freshness fields are stable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: The pinned "now" both surfaces see. 2026-11-20T12:00:00Z.
FIXED_NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

#: Fictional identities only: no real handle, channel id, post id or path.
STATE: dict[str, Any] = {
    "version": 2,
    "posted_videos": {
        "fic-vid-0001": {
            "source_platform": "local",
            "caption": "Fictional clip one",
            "source_url": "https://example.invalid/fic-1",
            "downloaded_at": "2026-11-19T09:00:00+00:00",
            "last_attempt": "2026-11-19T09:05:00+00:00",
            "local_path": "",
            "thumbnail": "fic-thumb-0001.jpg",
            "posted_to": {
                "youtube": {
                    "id": "fic-yt-0001",
                    "url": "https://www.youtube.com/watch?v=fic-yt-0001",
                    "timestamp": "2026-11-19T09:06:00+00:00",
                },
                "x": {
                    "id": "fic-x-0001",
                    "url": "https://x.com/i/status/fic-x-0001",
                    "timestamp": "2026-11-19T09:07:00+00:00",
                },
            },
            "errors": {},
        },
        "fic-vid-0002": {
            "source_platform": "local",
            "caption": "Fictional clip two",
            "source_url": "https://example.invalid/fic-2",
            "downloaded_at": "2026-11-10T08:00:00+00:00",
            "last_attempt": "2026-11-10T08:10:00+00:00",
            # Legacy `post_id` key: the resolver must fall back to it.
            "posted_to": {
                "youtube": {
                    "post_id": "fic-yt-0002",
                    "url": "https://www.youtube.com/watch?v=fic-yt-0002",
                    "timestamp": "2026-11-10T08:11:00+00:00",
                    "soft_hidden": True,
                },
                "instagram": {
                    "id": "fic-ig-0002",
                    "url": "https://www.instagram.com/p/fic-ig-0002/",
                    "timestamp": "2026-11-10T08:12:00+00:00",
                },
            },
            "errors": {
                "x": {
                    "error": "Fictional rate limit",
                    "retryable": True,
                    "timestamp": "2026-11-10T08:13:00+00:00",
                },
                "tiktok": {"error": "Fictional upload rejected", "retryable": False},
            },
        },
        "fic-vid-0003": {
            "source_platform": "local",
            "caption": "Fictional clip three",
            "downloaded_at": "2026-11-18T20:00:00+00:00",
            "posted_to": {
                "youtube": {
                    "id": "fic-yt-0003",
                    "url": "https://www.youtube.com/watch?v=fic-yt-0003",
                    "timestamp": "2026-11-18T20:05:00+00:00",
                    "deleted": True,
                }
            },
            "errors": {},
        },
        "fic-vid-0004": {
            "source_platform": "local",
            "caption": "Fictional clip four",
            "downloaded_at": "2026-11-01T07:00:00+00:00",
            "posted_to": {},
            "errors": {},
        },
        # Posted to Threads with no snapshot: the state-only lineup branch.
        "fic-vid-0005": {
            "source_platform": "local",
            "caption": "Fictional clip five",
            "downloaded_at": "2026-11-15T10:00:00+00:00",
            "posted_to": {
                "threads": {
                    "id": "fic-th-0005",
                    "url": "https://www.threads.net/post/fic-th-0005",
                    "timestamp": "2026-11-15T10:02:00+00:00",
                }
            },
            "errors": {},
        },
    },
    "content_hashes": {"fic-hash-0001": "fic-vid-0001"},
    "health": {
        "platforms": {
            "youtube": {
                "status": "ok",
                "last_success": "2026-11-19T09:06:00+00:00",
                "failures": 0,
                "last_error": None,
            },
            "x": {
                "status": "degraded",
                "last_success": "2026-11-19T09:07:00+00:00",
                "failures": 2,
                "last_error": "Fictional timeout",
            },
        },
        "sources": {},
        "total_processed": 5,
        "last_check": "2026-11-20T11:00:00+00:00",
        "last_wake_check": None,
    },
}

#: Snapshot rows, written through the real `AnalyticsStore.record_snapshots`.
SNAPSHOTS: list[dict[str, Any]] = [
    # Two captures for one post: the detail route's `series` has history.
    {
        "platform": "youtube",
        "post_id": "fic-yt-0001",
        "timestamp": "2026-11-19T10:00:00+00:00",
        "views": 100,
        "likes": 10,
        "comments": 2,
        "shares": 1,
        "collection_status": "ok",
    },
    {
        "platform": "youtube",
        "post_id": "fic-yt-0001",
        "timestamp": "2026-11-20T00:00:00+00:00",
        "views": 150,
        "likes": 15,
        "comments": 3,
        "shares": 2,
        "collection_status": "ok",
    },
    # `quotes` is a capability metric but not a canonical family: it must stay
    # in the raw row and never reach `metrics`.
    {
        "platform": "x",
        "post_id": "fic-x-0001",
        "timestamp": "2026-11-19T11:00:00+00:00",
        "views": 80,
        "likes": 8,
        "comments": 1,
        "shares": 0,
        "reposts": 4,
        "saves": 2,
        "quotes": 3,
        "collection_status": "ok",
    },
    # Legacy row: no status, and two metrics never reported (hidden counts).
    {
        "platform": "youtube",
        "post_id": "fic-yt-0002",
        "timestamp": "2026-11-10T09:00:00+00:00",
        "views": 40,
        "likes": None,
        "comments": 1,
        "shares": None,
    },
    # A FAILED collection: visible, but excluded from the totals.
    {
        "platform": "instagram",
        "post_id": "fic-ig-0002",
        "timestamp": "2026-11-10T09:30:00+00:00",
        "views": 60,
        "likes": 6,
        "comments": 0,
        "shares": 0,
        "saves": 5,
        "collection_status": "failed",
        "collection_error": "Fictional collection failure",
    },
    # A deleted publish: tracked, not counted in the totals.
    {
        "platform": "youtube",
        "post_id": "fic-yt-0003",
        "timestamp": "2026-11-18T21:00:00+00:00",
        "views": 20,
        "likes": 2,
        "comments": 0,
        "shares": 0,
        "collection_status": "ok",
    },
    # A snapshot with no state record: unowned, so it is dropped from outcomes.
    {
        "platform": "tiktok",
        "post_id": "fic-tk-0009",
        "timestamp": "2026-11-17T00:00:00+00:00",
        "views": 999,
        "likes": 99,
        "comments": 9,
        "shares": 9,
        "collection_status": "ok",
    },
]

#: Collection outcomes, written through `record_collection_runs`.
RUNS: list[dict[str, Any]] = [
    {
        "platform": "youtube",
        "captured_at": "2026-11-20T00:00:00+00:00",
        "status": "ok",
        "attempted": 3,
        "failures": 0,
        "requested": 3,
        "collected": 3,
    },
    {
        "platform": "x",
        "captured_at": "2026-11-19T12:00:00+00:00",
        "status": "failed",
        "error": "Fictional quota exceeded",
        "attempted": 1,
        "failures": 1,
        "requested": 1,
        "collected": 0,
    },
    {
        "platform": "instagram",
        "captured_at": "2026-11-10T09:30:00+00:00",
        "status": "partial",
        "attempted": 2,
        "failures": 1,
        "requested": 2,
        "collected": 1,
    },
]

#: `(fixture name, method, path)` — one captured body each.
ROUTES: list[tuple[str, str, str]] = [
    ("summary", "GET", "/api/summary"),
    ("state", "GET", "/state"),
    ("analytics_outcomes", "GET", "/api/analytics/outcomes"),
    ("videos", "GET", "/api/videos"),
    ("video_detail", "GET", "/api/videos/fic-vid-0001"),
    ("video_detail_unknown", "GET", "/api/videos/fic-vid-9999"),
    ("activity", "GET", "/api/activity"),
]


class FrozenDatetime(datetime):
    """`datetime` whose `now()` is the pinned instant."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # noqa: D102 - mirrors the stdlib
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)


def freeze_clock() -> None:
    """Pin every module that reads the wall clock for these payloads."""
    import xpst.analytics_outcomes
    import xpst.dashboard.analytics

    xpst.dashboard.analytics.datetime = FrozenDatetime
    xpst.analytics_outcomes.datetime = FrozenDatetime


def seed(config_dir: Path) -> None:
    """Write the fictional state file and a Python-created analytics.db."""
    from xpst.analytics_store import AnalyticsStore

    (config_dir / "state.json").write_text(
        json.dumps(STATE, indent=2) + "\n", encoding="utf-8"
    )
    store = AnalyticsStore(config_dir / "analytics.db")
    store.record_snapshots(SNAPSHOTS)
    store.record_collection_runs(RUNS)


def capture(config_dir: Path) -> dict[str, Any]:
    """Drive the real app and return `{name: {status, body}}` with raw bodies."""
    from fastapi.testclient import TestClient
    from xpst.dashboard.server import _create_app

    token = os.environ["XPST_API_TOKEN"]
    app = _create_app(str(config_dir), require_read_auth=False)
    client = TestClient(app, raise_server_exceptions=False)
    captured: dict[str, Any] = {}
    for name, method, path in ROUTES:
        response = client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}
        )
        captured[name] = {"status": response.status_code, "body": response.text}
    return captured


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(out: Path) -> dict[str, Any]:
    """Seed a scratch dir, capture every route, write the fixture tree."""
    workdir = Path(tempfile.mkdtemp(prefix="xpst-analytics-capture-"))
    try:
        seed(workdir)
        freeze_clock()
        captured = capture(workdir)
        expected = out / "expected"
        expected.mkdir(parents=True, exist_ok=True)
        for name, payload in captured.items():
            (expected / f"{name}.json").write_text(payload["body"], encoding="utf-8")
        # `state.json` is gitignored (it is runtime state), so the fixture keeps
        # it under a name that can be committed; the test copies it back.
        shutil.copyfile(workdir / "state.json", out / "state.fixture.json")
        shutil.copyfile(workdir / "analytics.db", out / "analytics.db")
        meta = {
            "captured_from": "src/xpst/dashboard/api.py + src/xpst/dashboard/analytics.py",
            "fixed_now": FIXED_NOW.isoformat(),
            "fixed_now_ms": int(FIXED_NOW.timestamp() * 1000),
            "routes": {name: {"method": method, "path": path} for name, method, path in ROUTES},
            "statuses": {name: payload["status"] for name, payload in captured.items()},
            "sha256": {
                "state.fixture.json": sha256(out / "state.fixture.json"),
                "analytics.db": sha256(out / "analytics.db"),
            },
        }
        (out / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return meta
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check(out: Path) -> int:
    """Re-capture into a temp dir and diff against the checked-in fixture."""
    scratch = Path(tempfile.mkdtemp(prefix="xpst-analytics-check-"))
    try:
        build(scratch)
        problems: list[str] = []
        for name in sorted({path.name for path in (out / "expected").glob("*.json")}):
            checked = out / "expected" / name
            fresh = scratch / "expected" / name
            if not fresh.exists():
                problems.append(f"{name}: missing from the re-capture")
                continue
            if checked.read_bytes() != fresh.read_bytes():
                problems.append(f"{name}: body differs from the re-capture")
        for name in ("state.fixture.json", "analytics.db"):
            if sha256(out / name) != sha256(scratch / name):
                problems.append(f"{name}: bytes differ from the re-capture")
        if problems:
            for problem in problems:
                print(f"analytics-routes: {problem}", file=sys.stderr)
            return 1
        print("analytics-routes: the checked-in fixture matches a fresh capture")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    out = Path(os.environ.get("XPST_CAPTURE_OUT") or root / "crates/xpst-api/tests/fixtures/analytics")
    os.environ.setdefault("XPST_API_TOKEN", "capture-token-not-a-secret")
    os.environ["XPST_DISABLE_AUTH_WARM"] = "1"
    if "--check" in argv:
        return check(out)
    out.mkdir(parents=True, exist_ok=True)
    meta = build(out)
    print(f"wrote {out} ({len(meta['routes'])} routes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
