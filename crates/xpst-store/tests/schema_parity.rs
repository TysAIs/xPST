//! Schema parity with the shipped Python engine.
//!
//! These tests run the real `xpst.analytics_store.AnalyticsStore` — no
//! transcription of its SQL — and compare what it produces with what this crate
//! produces, in both directions. They are skipped only when no interpreter can
//! import the engine (`XPST_PYTHON` points at one; CI's Rust lane installs the
//! Python package after `cargo test`, so it skips there and runs locally and in
//! any job where the engine is importable).

mod support;

use rusqlite::Connection;
use support::{assert_success, stdout, Python, TempDir};
use xpst_store::{CollectionStatus, Store};

fn python_or_skip(label: &str) -> Option<Python> {
    match Python::discover() {
        Some(python) => Some(python),
        None => {
            eprintln!(
                "SKIP {label}: no interpreter could import xpst.analytics_store \
                 (set XPST_PYTHON to a python with the engine's dependencies)"
            );
            None
        }
    }
}

fn path_str(path: &std::path::Path) -> &str {
    path.to_str().expect("utf-8 temp path")
}

/// A database written by Python must be readable by Rust, with the real values.
#[test]
fn python_created_database_reads_back_through_rust() {
    let Some(python) = python_or_skip("python_created_database_reads_back_through_rust") else {
        return;
    };
    let temp = TempDir::new("py-to-rs");
    let fixture_path = support::write_fixture(&temp);
    let db = temp.path().join("analytics.db");

    let output = python.run(
        &temp,
        &[
            "create",
            path_str(&db),
            "--fixture",
            path_str(&fixture_path),
        ],
    );
    assert_success(&output, "python create");

    let store = Store::open(&db).expect("rust opens the python ledger");
    // Python's own CREATE already includes the migrated columns, so agreeing
    // with it means there is nothing left to add.
    assert!(
        store.applied_migrations().is_empty(),
        "opening a current Python ledger must not require migrations, added {:?}",
        store.applied_migrations()
    );
    assert_eq!(store.snapshot_count().unwrap(), 3);

    let rows = store.latest(None).unwrap();
    assert_eq!(rows.len(), 3, "one latest row per post");

    let ok_row = rows
        .iter()
        .find(|row| row.post_id == "fic-vid-0001")
        .unwrap();
    assert_eq!(ok_row.platform, "youtube");
    assert_eq!(ok_row.captured_at, "2026-11-01T09:00:00+00:00");
    assert_eq!(ok_row.views, Some(1200));
    assert_eq!(ok_row.likes, Some(64));
    assert_eq!(ok_row.comments, Some(5));
    assert_eq!(ok_row.shares, Some(3));
    assert_eq!(ok_row.reposts, Some(1));
    assert_eq!(ok_row.saves, Some(2));
    assert_eq!(ok_row.collection_status, CollectionStatus::Ok);
    let extra: serde_json::Value =
        serde_json::from_str(ok_row.extra_json.as_deref().expect("extra json")).unwrap();
    assert_eq!(
        extra["quotes"], 4,
        "platform-specific field survives in extra"
    );

    let failed_row = rows
        .iter()
        .find(|row| row.post_id == "fic-vid-0002")
        .unwrap();
    assert_eq!(failed_row.collection_status, CollectionStatus::Failed);
    assert_eq!(
        failed_row.collection_error.as_deref(),
        Some("fic synthetic collector failure")
    );
    assert_eq!(failed_row.likes, None, "a missing count stays missing");

    let unlabelled = rows.iter().find(|row| row.post_id == "fic-x-0001").unwrap();
    assert_eq!(
        unlabelled.collection_status,
        CollectionStatus::Unknown,
        "an unlabelled row is never reported as ok"
    );

    assert_eq!(
        store.history("youtube", "fic-vid-0001", 10).unwrap().len(),
        1
    );

    let followers = store.latest_followers().unwrap();
    // Python's `record_followers` stamps "now" itself, so only the count is
    // comparable across implementations.
    assert_eq!(followers.get("youtube").map(|(count, _)| *count), Some(418));
    assert!(followers
        .get("youtube")
        .is_some_and(|(_, captured_at)| captured_at.ends_with("+00:00")));

    let group = store
        .get_cross_post_group("fic-content-hash-0001")
        .unwrap()
        .expect("cross-post group");
    assert_eq!(group.video_id, "fic-vid-0001");
    assert!(group.platforms_json.contains("fic-vid-0001"));

    let runs = store.latest_collection_runs().unwrap();
    let run = runs.get("youtube").expect("collection run");
    assert_eq!(run.status, CollectionStatus::Failed);
    assert_eq!(run.attempted, Some(2));
    assert_eq!(run.collected, Some(0));
}

/// A database written by Rust must be readable by Python, with the real values
/// (the Python tool asserts them and exits non-zero on any mismatch).
#[test]
fn rust_created_database_reads_back_through_python() {
    let Some(python) = python_or_skip("rust_created_database_reads_back_through_python") else {
        return;
    };
    let temp = TempDir::new("rs-to-py");
    let fixture = support::fixture();
    let fixture_path = support::write_fixture(&temp);
    let db = temp.path().join("analytics.db");

    let store = Store::open(&db).expect("rust creates the ledger");
    let written = support::seed_with_rust(&store, &fixture);
    assert_eq!(written, 3);
    drop(store);

    let output = python.run(
        &temp,
        &["read", path_str(&db), "--fixture", path_str(&fixture_path)],
    );
    assert_success(&output, "python read-back");
    assert!(
        stdout(&output).contains("read-back ok"),
        "unexpected python output: {}",
        stdout(&output)
    );
}

/// The two implementations must produce byte-identical schemas.
#[test]
fn schema_diff_between_python_and_rust_is_empty() {
    let Some(python) = python_or_skip("schema_diff_between_python_and_rust_is_empty") else {
        return;
    };
    let temp = TempDir::new("schema-diff");
    let fixture = support::fixture();
    let fixture_path = support::write_fixture(&temp);
    let py_db = temp.path().join("python.db");
    let rs_db = temp.path().join("rust.db");

    let output = python.run(
        &temp,
        &[
            "create",
            path_str(&py_db),
            "--fixture",
            path_str(&fixture_path),
        ],
    );
    assert_success(&output, "python create");

    let store = Store::open(&rs_db).expect("rust creates the ledger");
    support::seed_with_rust(&store, &fixture);
    drop(store);

    let diff = python.run(&temp, &["diff", path_str(&py_db), path_str(&rs_db)]);
    assert_success(&diff, "schema diff");
    assert!(
        stdout(&diff).contains("no schema differences"),
        "schema diff reported differences:\n{}",
        stdout(&diff)
    );

    // Belt and braces: sqlite_master text is identical, not just the PRAGMAs.
    assert_eq!(
        support::schema_fingerprint(&py_db),
        support::schema_fingerprint(&rs_db),
        "sqlite_master text differs between Python and Rust"
    );
}

/// Upgrading a legacy ledger must land on exactly the schema Python's own
/// migration produces, preserving the rows.
#[test]
fn legacy_upgrade_matches_pythons_migration() {
    let Some(python) = python_or_skip("legacy_upgrade_matches_pythons_migration") else {
        return;
    };
    let temp = TempDir::new("legacy-parity");
    let fixture_path = support::write_fixture(&temp);
    let py_db = temp.path().join("python-upgraded.db");
    let rs_db = temp.path().join("rust-upgraded.db");

    for db in [&py_db, &rs_db] {
        let output = python.run(
            &temp,
            &[
                "create-legacy",
                path_str(db),
                "--fixture",
                path_str(&fixture_path),
            ],
        );
        assert_success(&output, "python create-legacy");
    }
    // The two files start life identical; each side migrates its own copy.
    assert_eq!(
        std::fs::read(&py_db).unwrap(),
        std::fs::read(&rs_db).unwrap(),
        "legacy fixtures must be byte-identical before migration"
    );

    let output = python.run(&temp, &["upgrade", path_str(&py_db)]);
    assert_success(&output, "python upgrade");
    assert!(
        stdout(&output).contains("python upgrade changed the schema"),
        "expected Python to migrate the legacy ledger, got: {}",
        stdout(&output)
    );

    let store = Store::open(&rs_db).expect("rust upgrades the legacy ledger");
    assert_eq!(
        store.applied_migrations(),
        ["collection_status", "collection_error"],
        "exactly the two additive columns are added"
    );
    let legacy_count: i64 = Connection::open(&rs_db)
        .unwrap()
        .query_row("SELECT COUNT(*) FROM metric_snapshots", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(legacy_count, 3, "migration must not lose rows");
    let statuses: Vec<CollectionStatus> = store
        .latest(None)
        .unwrap()
        .iter()
        .map(|row| row.collection_status)
        .collect();
    assert!(
        statuses
            .iter()
            .all(|status| *status == CollectionStatus::Unknown),
        "legacy rows are readable but never labelled ok"
    );
    drop(store);

    let diff = python.run(&temp, &["diff", path_str(&py_db), path_str(&rs_db)]);
    assert_success(&diff, "post-migration schema diff");
    assert!(
        stdout(&diff).contains("no schema differences"),
        "migrated schemas differ:\n{}",
        stdout(&diff)
    );
    assert_eq!(
        support::schema_fingerprint(&py_db),
        support::schema_fingerprint(&rs_db),
        "migrated sqlite_master text differs"
    );
}
