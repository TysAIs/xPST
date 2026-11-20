//! Shared test scaffolding: throwaway directories, the canonical fictional
//! fixture, and the Python probe runner.

#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU32, Ordering};

use rusqlite::Connection;
use serde_json::Value;
use xpst_store::{CollectionStatus, CrossPostGroup, Metrics, NewCollectionRun, NewSnapshot, Store};

/// A directory under the session scratch area, removed on drop.
pub struct TempDir {
    path: PathBuf,
}

impl TempDir {
    pub fn new(label: &str) -> Self {
        static COUNTER: AtomicU32 = AtomicU32::new(0);
        let id = COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!(
            "xpst-store-{}-{}-{}",
            label,
            std::process::id(),
            id
        ));
        std::fs::create_dir_all(&path).expect("create temp dir");
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// The repository root, derived from this crate's manifest directory.
pub fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("crate lives two levels below the workspace root")
        .to_path_buf()
}

/// The canonical fixture: fictional ids only (the public-safety scanner runs in CI).
///
/// This JSON is the single source of truth for both directions of the parity
/// check — Python seeds its rows from it, and Rust maps it into `NewSnapshot`s,
/// so neither side can drift into asserting its own invention.
pub fn fixture() -> Value {
    serde_json::json!({
        "snapshots": [
            {
                "platform": "youtube",
                "post_id": "fic-vid-0001",
                "captured_at": "2026-11-01T09:00:00+00:00",
                "views": 1200,
                "likes": 64,
                "comments": 5,
                "shares": 3,
                "reposts": 1,
                "saves": 2,
                "extra": {"quotes": 4},
                "collection_status": "ok",
                "collection_error": null
            },
            {
                "platform": "youtube",
                "post_id": "fic-vid-0002",
                "captured_at": "2026-11-02T09:00:00+00:00",
                "views": 80,
                "likes": null,
                "comments": 1,
                "shares": 0,
                "reposts": 0,
                "saves": null,
                "extra": {},
                "collection_status": "failed",
                "collection_error": "fic synthetic collector failure"
            },
            {
                "platform": "x",
                "post_id": "fic-x-0001",
                "captured_at": "2026-11-03T09:00:00+00:00",
                "views": 42,
                "likes": 7,
                "comments": 1,
                "shares": 2,
                "reposts": 0,
                "saves": 1,
                "extra": {},
                "collection_status": null,
                "collection_error": null
            }
        ],
        "followers": [
            {"platform": "youtube", "count": 418, "captured_at": "2026-11-03T09:00:00+00:00"}
        ],
        "groups": [
            {
                "content_hash": "fic-content-hash-0001",
                "video_id": "fic-vid-0001",
                "caption": "fic caption",
                "source_url": null,
                "created_at": "2026-11-03T09:00:00+00:00",
                "platforms_json": "[{\"platform\": \"youtube\", \"post_id\": \"fic-vid-0001\"}]"
            }
        ],
        "runs": [
            {
                "platform": "youtube",
                "captured_at": "2026-11-03T09:00:00+00:00",
                "status": "failed",
                "error": "fic synthetic collector failure",
                "reason": null,
                "detail": null,
                "attempted": 2,
                "failures": 2,
                "requested": 2,
                "collected": 0
            }
        ]
    })
}

/// Write the canonical fixture to `temp` so the Python tool can read it.
pub fn write_fixture(temp: &TempDir) -> PathBuf {
    let path = temp.path().join("fixture.json");
    std::fs::write(
        &path,
        serde_json::to_string_pretty(&fixture()).expect("serialise fixture"),
    )
    .expect("write fixture");
    path
}

pub fn snapshots_from_fixture(fixture: &Value) -> Vec<NewSnapshot> {
    fixture["snapshots"]
        .as_array()
        .expect("snapshots array")
        .iter()
        .map(|item| {
            let metrics = Metrics {
                views: item["views"].as_i64(),
                likes: item["likes"].as_i64(),
                comments: item["comments"].as_i64(),
                shares: item["shares"].as_i64(),
                reposts: item["reposts"].as_i64(),
                saves: item["saves"].as_i64(),
            };
            let mut row = NewSnapshot::new(
                item["platform"].as_str().expect("platform"),
                item["post_id"].as_str().expect("post_id"),
                item["captured_at"].as_str().expect("captured_at"),
            )
            .metrics(metrics);
            let extra = item["extra"].as_object().expect("extra object");
            if !extra.is_empty() {
                row = row.extra_json(Value::Object(extra.clone()).to_string());
            }
            if let Some(status) = item["collection_status"].as_str() {
                row = row.status(CollectionStatus::parse(status));
            }
            if let Some(error) = item["collection_error"].as_str() {
                row = row.error(error);
            }
            row
        })
        .collect()
}

/// Persist the fixture through the Rust API.
pub fn seed_with_rust(store: &Store, fixture: &Value) -> usize {
    let written = store
        .record_snapshots(&snapshots_from_fixture(fixture))
        .expect("record snapshots");
    for follower in fixture["followers"].as_array().expect("followers") {
        store
            .record_followers(
                follower["platform"].as_str().expect("platform"),
                follower["count"].as_i64().expect("count"),
                follower["captured_at"].as_str().expect("captured_at"),
            )
            .expect("record follower");
    }
    for group in fixture["groups"].as_array().expect("groups") {
        store
            .record_cross_post_group(&CrossPostGroup {
                content_hash: group["content_hash"]
                    .as_str()
                    .expect("content_hash")
                    .to_owned(),
                video_id: group["video_id"].as_str().expect("video_id").to_owned(),
                caption: group["caption"].as_str().map(str::to_owned),
                source_url: group["source_url"].as_str().map(str::to_owned),
                created_at: group["created_at"].as_str().expect("created_at").to_owned(),
                platforms_json: group["platforms_json"]
                    .as_str()
                    .expect("platforms_json")
                    .to_owned(),
            })
            .expect("record group");
    }
    let runs: Vec<NewCollectionRun> = fixture["runs"]
        .as_array()
        .expect("runs")
        .iter()
        .map(|run| {
            let mut entry = NewCollectionRun::new(
                run["platform"].as_str().expect("platform"),
                run["captured_at"].as_str().expect("captured_at"),
                CollectionStatus::parse(run["status"].as_str().expect("status")),
            );
            entry.error = run["error"].as_str().map(str::to_owned);
            entry.reason = run["reason"].as_str().map(str::to_owned);
            entry.detail = run["detail"].as_str().map(str::to_owned);
            entry.attempted = run["attempted"].as_i64();
            entry.failures = run["failures"].as_i64();
            entry.requested = run["requested"].as_i64();
            entry.collected = run["collected"].as_i64();
            entry
        })
        .collect();
    store
        .record_collection_runs(&runs)
        .expect("record collection runs");
    written
}

/// The pre-migration `metric_snapshots` schema (xPST commit 02930c0^), used to
/// simulate an operator's older ledger. Historical text: do not "modernise".
pub const LEGACY_SCHEMA_SQL: &str = "
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
";

/// Create a ledger with the historical schema and a couple of rows in it.
pub fn create_legacy_db(path: &Path) -> Connection {
    let conn = Connection::open(path).expect("open legacy db");
    conn.execute_batch(LEGACY_SCHEMA_SQL)
        .expect("legacy schema");
    conn.execute(
        "INSERT INTO metric_snapshots
         (platform, post_id, captured_at, views, likes, comments, shares, reposts, saves, extra)
         VALUES ('youtube', 'fic-legacy-0001', '2026-10-01T09:00:00+00:00', 500, 20, 3, 1, 0, 2, '{\"quotes\": 1}')",
        [],
    )
    .expect("legacy row 1");
    conn.execute(
        "INSERT INTO metric_snapshots
         (platform, post_id, captured_at, views, likes, comments, shares, reposts, saves, extra)
         VALUES ('x', 'fic-legacy-0002', '2026-10-02T09:00:00+00:00', 15, 1, NULL, NULL, NULL, NULL, NULL)",
        [],
    )
    .expect("legacy row 2");
    conn
}

/// `sqlite_master` text, in a stable order — any DDL difference shows up here.
pub fn schema_fingerprint(path: &Path) -> String {
    let conn = Connection::open(path).expect("open for fingerprint");
    let mut stmt = conn
        .prepare(
            "SELECT type, name, tbl_name, ifnull(sql, '') FROM sqlite_master ORDER BY type, name",
        )
        .expect("prepare fingerprint");
    let rows: Vec<String> = stmt
        .query_map([], |row| {
            Ok(format!(
                "{}|{}|{}|{}",
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?
            ))
        })
        .expect("query fingerprint")
        .map(Result::unwrap)
        .collect();
    rows.join("\n")
}

pub fn schema_version(path: &Path) -> i64 {
    Connection::open(path)
        .expect("open for schema_version")
        .query_row("PRAGMA schema_version", [], |row| row.get(0))
        .expect("schema_version")
}

pub fn journal_mode(path: &Path) -> String {
    Connection::open(path)
        .expect("open for journal_mode")
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .expect("journal_mode")
}

/// A Python interpreter that can import the real engine, if one exists.
pub struct Python {
    program: String,
}

impl Python {
    /// Interpreters to try. `XPST_PYTHON` wins; the rest are best-effort.
    pub fn discover() -> Option<Self> {
        let mut candidates: Vec<String> = Vec::new();
        if let Ok(configured) = std::env::var("XPST_PYTHON") {
            candidates.push(configured);
        }
        candidates.extend(["python3".to_owned(), "python".to_owned()]);
        candidates
            .into_iter()
            .find(|program| Self::can_import(program))
            .map(|program| Self { program })
    }

    fn can_import(program: &str) -> bool {
        Command::new(program)
            .args(["-c", "import xpst.analytics_store"])
            .env("PYTHONPATH", workspace_root().join("src"))
            .env(
                "XPST_CONFIG_DIR",
                std::env::temp_dir().join("xpst-store-probe"),
            )
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    pub fn run(&self, temp: &TempDir, args: &[&str]) -> Output {
        let tool = workspace_root().join("crates/xpst-store/tests/python_parity/schema_tool.py");
        Command::new(&self.program)
            .arg(tool)
            .args(args)
            .env("PYTHONPATH", workspace_root().join("src"))
            .env("XPST_CONFIG_DIR", temp.path().join("config"))
            .env("PYTHONHASHSEED", "0")
            .output()
            .expect("run python parity tool")
    }
}

pub fn assert_success(output: &Output, what: &str) {
    assert!(
        output.status.success(),
        "{what} failed (status {:?})\n--- stdout ---\n{}\n--- stderr ---\n{}",
        output.status.code(),
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

pub fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}
