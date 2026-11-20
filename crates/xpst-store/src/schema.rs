//! Schema text, copied verbatim from `src/xpst/analytics_store.py`.
//!
//! Python is the schema authority: `AnalyticsStore.__init__` runs [`ANALYTICS_SCHEMA_SQL`]
//! and [`FOLLOWER_SCHEMA_SQL`] through `sqlite3.executescript`, then applies the
//! guarded `ALTER TABLE`s in [`crate::migrations`]. SQLite stores the CREATE text
//! it was given in `sqlite_master.sql`, so "byte-compatible structure" means
//! copying these strings character-for-character rather than writing an
//! equivalent DDL by hand.
//!
//! `tests/schema_parity.rs` checks this against a database produced by the real
//! Python store and fails on any difference in `sqlite_master`, column order,
//! declared types, NOT NULL flags, primary keys or indexes.
//!
//! Do not reformat these strings. A "tidier" CREATE changes `sqlite_master.sql`
//! and turns a compatible database into a divergent one.

/// `analytics_store._SCHEMA` as of the current `main`.
pub const ANALYTICS_SCHEMA_SQL: &str = r#"
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
"#;

/// `analytics_store._FOLLOWER_SCHEMA` as of the current `main`.
pub const FOLLOWER_SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS follower_snapshots (
    platform    TEXT NOT NULL,
    count       INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (platform, captured_at)
);
"#;
