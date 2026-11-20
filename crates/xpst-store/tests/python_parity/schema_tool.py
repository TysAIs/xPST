#!/usr/bin/env python3
"""Cross-implementation probes for the Rust SQLite store.

This script is deliberately written against the REAL Python store
(``xpst.analytics_store.AnalyticsStore``), not against a hand-written copy of
its SQL: the point of the exercise is to find out whether the Rust crate
creates a database the shipped Python engine can open, and vice versa.

Modes
-----
create      <db> --fixture <json>   create the ledger with Python and persist the fixture
create-legacy <db>                  create a pre-``collection_status`` ledger (with rows)
dump        <db>                    canonical JSON description of the schema
diff        <a> <b>                 exit 1 unless both schemas are identical
read        <db> --fixture <json>   assert a database written by Rust reads back correctly
upgrade     <db>                    open an existing ledger with Python, report schema change

Safety: refuses to run unless ``XPST_CONFIG_DIR`` is set, so a mistake cannot
write into the operator's real config directory.

Fixtures must contain fictional ids only; the public-safety scanner runs in CI.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

CORE_FIELDS = ("views", "likes", "comments", "shares", "reposts", "saves")

#: The ``metric_snapshots`` DDL as it was before the additive
#: ``collection_status``/``collection_error`` columns (xPST commit 02930c0^),
#: plus the follower table. Used to simulate an operator's older ledger.
LEGACY_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS follower_snapshots (
    platform    TEXT NOT NULL,
    count       INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (platform, captured_at)
);
"""


def require_sandbox() -> None:
    if not os.environ.get("XPST_CONFIG_DIR"):
        sys.exit("refusing to run: set XPST_CONFIG_DIR to a throwaway directory")


def store_module():
    require_sandbox()
    from xpst.analytics_store import AnalyticsStore

    return AnalyticsStore


def load_fixture(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def snapshot_records(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixture entry → the dict shape ``record_snapshots`` expects."""
    records = []
    for item in fixture["snapshots"]:
        record: dict[str, Any] = {
            "platform": item["platform"],
            "post_id": item["post_id"],
            "timestamp": item["captured_at"],
        }
        for field in CORE_FIELDS:
            if item.get(field) is not None:
                record[field] = item[field]
        record.update(item.get("extra") or {})
        if item.get("collection_status"):
            record["collection_status"] = item["collection_status"]
        if item.get("collection_error"):
            record["collection_error"] = item["collection_error"]
        records.append(record)
    return records


def seed_with_python(db: Path, fixture: dict[str, Any]) -> None:
    analytics_store = store_module()
    store = analytics_store(db)
    store.record_snapshots(snapshot_records(fixture))
    for follower in fixture.get("followers", []):
        store.record_followers(follower["platform"], follower["count"])
    for group in fixture.get("groups", []):
        store.record_cross_post_group(
            group["content_hash"],
            group["video_id"],
            group.get("caption"),
            group.get("source_url"),
            json.loads(group["platforms_json"]),
        )
    if fixture.get("runs"):
        store.record_collection_runs(fixture["runs"])


def create_legacy(db: Path, fixture: dict[str, Any]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.executescript(LEGACY_SCHEMA)
        for item in fixture["snapshots"]:
            extra = item.get("extra")
            conn.execute(
                """
                INSERT OR REPLACE INTO metric_snapshots
                (platform, post_id, captured_at, views, likes, comments,
                 shares, reposts, saves, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["platform"],
                    item["post_id"],
                    item["captured_at"],
                    *(item.get(field) for field in CORE_FIELDS),
                    json.dumps(extra) if extra else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def dump(db: Path) -> dict[str, Any]:
    """Everything that decides whether two databases are the same store."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        objects = [
            {"type": row[0], "name": row[1], "table": row[2], "sql": row[3]}
            for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
        tables: dict[str, Any] = {}
        for name in [obj["name"] for obj in objects if obj["type"] == "table"]:
            indexes = []
            for index in conn.execute(f'PRAGMA index_list("{name}")'):
                indexes.append(
                    {
                        "seq": index[0],
                        "name": index[1],
                        "unique": index[2],
                        "origin": index[3],
                        "partial": index[4],
                        "columns": [row[2] for row in conn.execute(f'PRAGMA index_info("{index[1]}")')],
                    }
                )
            tables[name] = {
                "columns": [
                    {"cid": row[0], "name": row[1], "type": row[2], "notnull": row[3], "default": row[4], "pk": row[5]}
                    for row in conn.execute(f'PRAGMA table_info("{name}")')
                ],
                "indexes": sorted(indexes, key=lambda entry: entry["name"]),
                "foreign_keys": [
                    {"table": row[2], "from": row[3], "to": row[4], "on_update": row[5], "on_delete": row[6]}
                    for row in conn.execute(f'PRAGMA foreign_key_list("{name}")')
                ],
            }
        return {
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "objects": objects,
            "tables": tables,
        }
    finally:
        conn.close()


def canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def diff(left: Path, right: Path) -> int:
    a, b = canonical(dump(left)), canonical(dump(right))
    if a == b:
        print(f"no schema differences between {left} and {right}")
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=str(left),
            tofile=str(right),
        )
    )
    return 1


def read_back(db: Path, fixture: dict[str, Any]) -> None:
    """Assert a database written by Rust matches the fixture, via Python's API."""
    analytics_store = store_module()
    store = analytics_store(db)
    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    latest = {(row["platform"], row["post_id"]): row for row in store.latest()}
    check(store.snapshot_count() == len(fixture["snapshots"]), "snapshot_count mismatch")
    for item in fixture["snapshots"]:
        key = (item["platform"], item["post_id"])
        row = latest.get(key)
        if row is None:
            problems.append(f"missing latest row for {key}")
            continue
        check(row["captured_at"] == item["captured_at"], f"{key}: captured_at")
        for field in CORE_FIELDS:
            check(row.get(field) == item.get(field), f"{key}: {field} = {row.get(field)!r}")
        expected_status = (item.get("collection_status") or "unknown").lower()
        if expected_status not in {"ok", "partial", "failed", "unavailable", "not_collected"}:
            expected_status = "unknown"
        check(row.get("collection_status") == expected_status, f"{key}: collection_status")
        check(row.get("collection_error") == item.get("collection_error"), f"{key}: collection_error")
        for name, value in (item.get("extra") or {}).items():
            check(row.get(name) == value, f"{key}: extra field {name} = {row.get(name)!r}")

        history = store.history(item["platform"], item["post_id"])
        check(len(history) == 1, f"{key}: history length {len(history)}")

    followers = store.latest_followers()
    for follower in fixture.get("followers", []):
        stored = followers.get(follower["platform"])
        check(stored is not None and stored["count"] == follower["count"], f"follower {follower['platform']}")

    for group in fixture.get("groups", []):
        stored = store.get_cross_post_group(group["content_hash"])
        check(stored is not None, f"missing cross-post group {group['content_hash']}")
        if stored is not None:
            check(stored["video_id"] == group["video_id"], "cross-post video_id")
            check(stored["platforms"] == json.loads(group["platforms_json"]), "cross-post platforms")

    runs = store.latest_collection_runs()
    for run in fixture.get("runs", []):
        stored = runs.get(run["platform"].lower())
        check(stored is not None, f"missing collection run {run['platform']}")
        if stored is not None:
            check(stored["status"] == run["status"], f"run {run['platform']}: status")
            check(stored.get("collected") == run.get("collected"), f"run {run['platform']}: collected")

    if problems:
        sys.exit("read-back failed:\n  " + "\n  ".join(problems))
    print(
        f"read-back ok: {len(fixture['snapshots'])} snapshots, "
        f"{len(fixture.get('followers', []))} follower rows, "
        f"{len(fixture.get('groups', []))} groups, {len(fixture.get('runs', []))} runs"
    )


def upgrade(db: Path) -> None:
    """Open with Python and report whether its schema changed."""
    before = canonical(dump(db))
    analytics_store = store_module()
    analytics_store(db)  # CREATE IF NOT EXISTS + additive ALTERs
    after = canonical(dump(db))
    if before == after:
        print(f"python upgrade was a no-op: {db}")
        return
    print(f"python upgrade changed the schema of {db}")
    sys.stdout.writelines(
        difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), "before", "after")
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["create", "create-legacy", "dump", "diff", "read", "upgrade"])
    parser.add_argument("db", type=Path)
    parser.add_argument("other", type=Path, nargs="?")
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args(argv)

    if args.mode == "dump":
        print(canonical(dump(args.db)), end="")
        return 0
    if args.mode == "diff":
        if args.other is None:
            parser.error("diff needs two databases")
        return diff(args.db, args.other)
    if args.mode == "upgrade":
        upgrade(args.db)
        return 0
    if args.fixture is None:
        parser.error(f"{args.mode} needs --fixture")
    fixture = load_fixture(str(args.fixture))
    if args.mode == "create":
        seed_with_python(args.db, fixture)
        print(f"created {args.db} with Python")
        return 0
    if args.mode == "create-legacy":
        create_legacy(args.db, fixture)
        print(f"created legacy {args.db}")
        return 0
    read_back(args.db, fixture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
