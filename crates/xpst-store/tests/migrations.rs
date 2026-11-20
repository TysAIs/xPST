//! Migration discipline: upgrade an older ledger without loss, and open twice
//! without touching anything the second time.

mod support;

use rusqlite::Connection;
use support::TempDir;
use xpst_store::{CollectionStatus, Metrics, NewSnapshot, Store};

const SNAPSHOT_COLUMNS: [&str; 12] = [
    "platform",
    "post_id",
    "captured_at",
    "views",
    "likes",
    "comments",
    "shares",
    "reposts",
    "saves",
    "extra",
    "collection_status",
    "collection_error",
];

fn column_names(path: &std::path::Path) -> Vec<String> {
    let conn = Connection::open(path).unwrap();
    let mut stmt = conn.prepare("PRAGMA table_info(metric_snapshots)").unwrap();
    let names = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    names
}

fn snapshot_values(path: &std::path::Path) -> Vec<(String, Option<i64>)> {
    let conn = Connection::open(path).unwrap();
    let mut stmt = conn
        .prepare("SELECT post_id, views FROM metric_snapshots ORDER BY post_id")
        .unwrap();
    let rows = stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, Option<i64>>(1)?))
        })
        .unwrap()
        .map(Result::unwrap)
        .collect();
    rows
}

#[test]
fn legacy_ledger_upgrades_losslessly_and_idempotently() {
    let temp = TempDir::new("legacy-upgrade");
    let db = temp.path().join("analytics.db");
    drop(support::create_legacy_db(&db));

    let before_values = snapshot_values(&db);
    let before_version = support::schema_version(&db);
    let before_fingerprint = support::schema_fingerprint(&db);
    assert_eq!(
        column_names(&db).len(),
        10,
        "the legacy fixture must not already have the new columns"
    );

    let store = Store::open(&db).expect("rust upgrades the legacy ledger");
    assert_eq!(
        store.applied_migrations(),
        ["collection_status", "collection_error"],
        "an older ledger needs exactly the additive columns"
    );
    assert_eq!(
        column_names(&db),
        SNAPSHOT_COLUMNS,
        "column order is append-only"
    );
    assert_eq!(store.snapshot_count().unwrap(), 2, "no rows lost");
    assert_eq!(
        snapshot_values(&db),
        before_values,
        "values unchanged by the migration"
    );

    let rows = store.latest(None).unwrap();
    let legacy_youtube = rows
        .iter()
        .find(|row| row.post_id == "fic-legacy-0001")
        .unwrap();
    assert_eq!(legacy_youtube.views, Some(500));
    assert_eq!(
        legacy_youtube.collection_status,
        CollectionStatus::Unknown,
        "a legacy row is never promoted to ok"
    );
    assert!(legacy_youtube.collection_error.is_none());
    drop(store);

    let upgraded_version = support::schema_version(&db);
    let upgraded_fingerprint = support::schema_fingerprint(&db);
    assert!(
        upgraded_version > before_version,
        "the upgrade did run DDL ({before_version} -> {upgraded_version})"
    );
    assert_ne!(upgraded_fingerprint, before_fingerprint);

    // Second open: nothing to do, and nothing is done.
    let store = Store::open(&db).expect("second open");
    assert!(
        store.applied_migrations().is_empty(),
        "a current ledger must not be migrated again"
    );
    assert_eq!(
        support::schema_version(&db),
        upgraded_version,
        "opening twice must not run DDL"
    );
    assert_eq!(support::schema_fingerprint(&db), upgraded_fingerprint);
    assert_eq!(store.snapshot_count().unwrap(), 2);
    assert_eq!(snapshot_values(&db), before_values);
}

#[test]
fn fresh_ledger_is_already_current_and_stable() {
    let temp = TempDir::new("fresh-open");
    let db = temp.path().join("analytics.db");

    let store = Store::open(&db).expect("create");
    assert!(
        store.applied_migrations().is_empty(),
        "a fresh database is created at the current schema, not migrated into it"
    );
    assert_eq!(column_names(&db), SNAPSHOT_COLUMNS);
    assert_eq!(
        store.journal_mode().unwrap(),
        "delete",
        "the default keeps Python's rollback journal; WAL is opt-in"
    );
    let version = support::schema_version(&db);
    let fingerprint = support::schema_fingerprint(&db);
    drop(store);

    let store = Store::open(&db).expect("reopen");
    assert!(store.applied_migrations().is_empty());
    assert_eq!(support::schema_version(&db), version);
    assert_eq!(support::schema_fingerprint(&db), fingerprint);
}

/// A database written by a *newer* engine carries columns this build does not
/// know. The store must keep them rather than rewrite the table.
#[test]
fn unknown_future_column_is_preserved() {
    let temp = TempDir::new("future-column");
    let db = temp.path().join("analytics.db");

    let store = Store::open(&db).unwrap();
    store
        .record_snapshots(&[NewSnapshot::new(
            "youtube",
            "fic-vid-0001",
            "2026-11-01T09:00:00+00:00",
        )
        .metrics(Metrics {
            views: Some(5),
            ..Metrics::default()
        })])
        .unwrap();
    drop(store);

    Connection::open(&db)
        .unwrap()
        .execute_batch("ALTER TABLE metric_snapshots ADD COLUMN future_note TEXT DEFAULT 'kept'")
        .unwrap();

    let store = Store::open(&db).expect("open a newer schema");
    assert!(store.applied_migrations().is_empty());
    store
        .record_snapshots(&[NewSnapshot::new(
            "youtube",
            "fic-vid-0002",
            "2026-11-02T09:00:00+00:00",
        )])
        .unwrap();
    drop(store);

    let conn = Connection::open(&db).unwrap();
    let rows: Vec<(String, Option<String>, Option<i64>)> = conn
        .prepare("SELECT post_id, future_note, views FROM metric_snapshots ORDER BY post_id")
        .unwrap()
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(rows.len(), 2);
    assert_eq!(
        rows[0],
        ("fic-vid-0001".to_owned(), Some("kept".to_owned()), Some(5))
    );
    assert_eq!(rows[1].0, "fic-vid-0002");
    assert_eq!(
        rows[1].1.as_deref(),
        Some("kept"),
        "the new column keeps its default"
    );
}
