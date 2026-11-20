//! Concurrency smoke test: two (or four) connections writing at the same time
//! must not corrupt the ledger or lose rows, under the journal mode the store
//! actually uses by default.

mod support;

use std::sync::{Arc, Barrier};
use std::thread;

use support::TempDir;
use xpst_store::{JournalMode, Metrics, NewSnapshot, OpenOptions, Store};

const WRITERS: usize = 4;
const ROWS_PER_WRITER: usize = 50;

fn snapshot(platform: &str, post_id: &str, captured_at: &str, views: i64) -> NewSnapshot {
    NewSnapshot::new(platform, post_id, captured_at).metrics(Metrics {
        views: Some(views),
        ..Metrics::default()
    })
}

/// Distinct rows from concurrent connections are all present afterwards.
#[test]
fn concurrent_writers_do_not_lose_rows() {
    let temp = TempDir::new("concurrent-distinct");
    let db = temp.path().join("analytics.db");
    // Create the schema before the race so the test measures writes, not DDL.
    drop(Store::open(&db).unwrap());

    let barrier = Arc::new(Barrier::new(WRITERS));
    let handles: Vec<_> = (0..WRITERS)
        .map(|writer| {
            let db = db.clone();
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                let store = Store::open(&db).expect("open writer connection");
                barrier.wait();
                for index in 0..ROWS_PER_WRITER {
                    let post_id = format!("fic-conc-{writer:02}-{index:03}");
                    store
                        .record_snapshots(&[snapshot(
                            "youtube",
                            &post_id,
                            &format!("2026-11-17T10:{:02}:00+00:00", index % 60),
                            index as i64,
                        )])
                        .unwrap_or_else(|error| panic!("writer {writer} row {index}: {error}"));
                }
            })
        })
        .collect();

    for handle in handles {
        handle.join().expect("writer thread panicked");
    }

    let store = Store::open(&db).unwrap();
    assert_eq!(
        store.snapshot_count().unwrap(),
        (WRITERS * ROWS_PER_WRITER) as i64,
        "every concurrent write is present"
    );
    for writer in 0..WRITERS {
        for index in 0..ROWS_PER_WRITER {
            let post_id = format!("fic-conc-{writer:02}-{index:03}");
            assert_eq!(
                store.history("youtube", &post_id, 5).unwrap().len(),
                1,
                "lost row {post_id}"
            );
        }
    }
}

/// Concurrent appends to the *same* post interleave without dropping snapshots.
#[test]
fn concurrent_snapshots_for_one_post_are_all_kept() {
    let temp = TempDir::new("concurrent-same-post");
    let db = temp.path().join("analytics.db");
    drop(Store::open(&db).unwrap());

    const PER_WRITER: usize = 25;
    let barrier = Arc::new(Barrier::new(2));
    let handles: Vec<_> = (0..2)
        .map(|writer| {
            let db = db.clone();
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                let store = Store::open(&db).expect("open writer connection");
                barrier.wait();
                for index in 0..PER_WRITER {
                    let sequence = writer * PER_WRITER + index;
                    store
                        .record_snapshots(&[snapshot(
                            "youtube",
                            "fic-vid-0001",
                            &format!("2026-11-17T11:{sequence:02}:00+00:00"),
                            sequence as i64,
                        )])
                        .unwrap();
                }
            })
        })
        .collect();

    for handle in handles {
        handle.join().expect("writer thread panicked");
    }

    let store = Store::open(&db).unwrap();
    let history = store.history("youtube", "fic-vid-0001", 100).unwrap();
    assert_eq!(
        history.len(),
        2 * PER_WRITER,
        "no snapshot was silently replaced"
    );
    let mut views: Vec<i64> = history.iter().filter_map(|row| row.views).collect();
    views.sort_unstable();
    assert_eq!(views, (0..(2 * PER_WRITER) as i64).collect::<Vec<_>>());
}

/// WAL is opt-in, persistent, and never applied implicitly. Documented in the
/// crate README; this test is what makes the statement checkable.
#[test]
fn wal_is_opt_in_and_sticky() {
    let temp = TempDir::new("wal");
    let db = temp.path().join("analytics.db");

    let store = Store::open_with(
        &db,
        OpenOptions {
            journal_mode: JournalMode::Wal,
            ..OpenOptions::default()
        },
    )
    .expect("open in WAL");
    assert_eq!(store.journal_mode().unwrap(), "wal");
    store
        .record_snapshots(&[snapshot(
            "youtube",
            "fic-vid-0001",
            "2026-11-17T10:00:00+00:00",
            1,
        )])
        .unwrap();
    drop(store);

    assert_eq!(
        support::journal_mode(&db),
        "wal",
        "the mode is a property of the file"
    );

    // Reopening with the default must preserve what is there rather than
    // silently downgrade a database another writer may already rely on.
    let store = Store::open(&db).expect("reopen with defaults");
    assert_eq!(store.journal_mode().unwrap(), "wal");
    assert_eq!(store.snapshot_count().unwrap(), 1);
}
