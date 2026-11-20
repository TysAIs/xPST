//! The ownership gate: only ids verified against the account may be written,
//! and only a non-empty verified set may purge. Fictional ids throughout.

mod support;

use support::TempDir;
use xpst_store::{
    Admission, Metrics, NewSnapshot, OwnedPostIds, OwnershipEvidence, OwnershipGate, RecordOutcome,
    Store,
};

fn snapshot(platform: &str, post_id: &str, views: i64) -> NewSnapshot {
    NewSnapshot::new(platform, post_id, "2026-11-17T10:00:00+00:00").metrics(Metrics {
        views: Some(views),
        ..Metrics::default()
    })
}

#[test]
fn gate_admits_only_verified_ids() {
    let gate = OwnershipGate::new().with(
        "youtube",
        OwnershipEvidence::Verified(OwnedPostIds::verified([" fic-vid-0001 ", ""])),
    );
    assert_eq!(gate.admit("youtube", "fic-vid-0001"), Admission::Owned);
    assert_eq!(gate.admit("YouTube", "fic-vid-0001"), Admission::Owned);
    assert_eq!(
        gate.admit("youtube", "fic-foreign-0001"),
        Admission::NotOwned
    );
    assert_eq!(
        gate.admit("youtube", "fic-foreign-0001").reason(),
        Some("post id is not in the verified ownership set")
    );
    assert!(gate.admit("youtube", "fic-vid-0001").is_admitted());
}

#[test]
fn missing_or_failed_evidence_admits_nothing() {
    let empty = OwnershipGate::new(); // no evidence registered at all
    assert_eq!(
        empty.admit("youtube", "fic-vid-0001"),
        Admission::Unverifiable
    );

    let failed = OwnershipGate::new()
        .with("youtube", OwnershipEvidence::Unverifiable)
        .with(
            "x",
            OwnershipEvidence::Verified(OwnedPostIds::verified(Vec::<String>::new())),
        );
    assert_eq!(
        failed.admit("youtube", "fic-vid-0001"),
        Admission::Unverifiable
    );
    assert_eq!(
        failed.admit("youtube", "fic-vid-0001").reason(),
        Some("ownership could not be verified")
    );
    // A verified account with no posts is a real empty set: it owns nothing.
    assert_eq!(failed.admit("x", "fic-x-0001"), Admission::NotOwned);
    assert!(!failed.admit("x", "fic-x-0001").is_admitted());
}

#[test]
fn platforms_outside_the_contract_pass_through() {
    let gate = OwnershipGate::new();
    assert_eq!(
        gate.admit("mastodon", "fic-other-0001"),
        Admission::UngatedPlatform,
        "Python preserves integrations that predate the gate"
    );
    assert!(gate.admit("mastodon", "fic-other-0001").is_admitted());
    assert!(xpst_store::is_gated_platform("threads"));
    assert!(!xpst_store::is_gated_platform("mastodon"));
}

#[test]
fn gated_write_persists_only_admitted_rows() {
    let store = Store::open_in_memory().unwrap();
    let gate = OwnershipGate::new().with(
        "youtube",
        OwnershipEvidence::Verified(OwnedPostIds::verified(["fic-vid-0001"])),
    );

    let outcome = store
        .record_snapshots_gated(
            &gate,
            &[
                snapshot("youtube", "fic-vid-0001", 10),
                snapshot("youtube", "fic-foreign-0001", 999),
                NewSnapshot::new("youtube", "", "2026-11-17T10:00:00+00:00"),
                snapshot("mastodon", "fic-other-0001", 3),
            ],
        )
        .unwrap();

    assert_eq!(
        outcome,
        RecordOutcome {
            written: 2,
            skipped_missing_identity: 1,
            skipped_unverifiable: 0,
            skipped_not_owned: 1,
        }
    );
    assert_eq!(store.snapshot_count().unwrap(), 2);
    assert!(store
        .history("youtube", "fic-foreign-0001", 10)
        .unwrap()
        .is_empty());
    assert_eq!(
        store.history("youtube", "fic-vid-0001", 10).unwrap().len(),
        1
    );
    assert_eq!(
        store
            .history("mastodon", "fic-other-0001", 10)
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn unverifiable_platform_writes_nothing_at_all() {
    let store = Store::open_in_memory().unwrap();
    let gate = OwnershipGate::new()
        .with("youtube", OwnershipEvidence::Unverifiable)
        .with(
            "x",
            OwnershipEvidence::Verified(OwnedPostIds::verified(["fic-x-0001"])),
        );

    let outcome = store
        .record_snapshots_gated(
            &gate,
            &[
                snapshot("youtube", "fic-vid-0001", 10),
                snapshot("x", "fic-x-0001", 7),
            ],
        )
        .unwrap();

    assert_eq!(outcome.written, 1);
    assert_eq!(outcome.skipped_unverifiable, 1);
    assert_eq!(store.snapshot_count().unwrap(), 1);
    assert_eq!(store.latest(Some("x")).unwrap().len(), 1);
}

#[test]
fn purge_removes_only_foreign_rows_for_that_platform() {
    let temp = TempDir::new("ownership-purge");
    let store = Store::open(temp.path().join("analytics.db")).unwrap();
    store
        .record_snapshots(&[
            snapshot("youtube", "fic-vid-0001", 10),
            snapshot("youtube", "fic-foreign-0001", 99),
            snapshot("x", "fic-x-0001", 5),
        ])
        .unwrap();

    let owned = OwnedPostIds::verified(["fic-vid-0001"]);
    let deleted = store.delete_youtube_snapshots_not_owned(&owned).unwrap();
    assert_eq!(deleted, 1);
    assert_eq!(store.snapshot_count().unwrap(), 2);
    assert!(store
        .history("youtube", "fic-foreign-0001", 10)
        .unwrap()
        .is_empty());
    assert_eq!(
        store.history("x", "fic-x-0001", 10).unwrap().len(),
        1,
        "another platform is never touched"
    );

    // Idempotent: nothing foreign is left to delete.
    assert_eq!(store.delete_youtube_snapshots_not_owned(&owned).unwrap(), 0);
}

#[test]
fn purge_with_no_verified_ids_is_a_noop() {
    let temp = TempDir::new("ownership-empty-purge");
    let store = Store::open(temp.path().join("analytics.db")).unwrap();
    store
        .record_snapshots(&[
            snapshot("youtube", "fic-vid-0001", 10),
            snapshot("youtube", "fic-vid-0002", 20),
        ])
        .unwrap();

    // A verified-but-empty set must never be read as "delete everything".
    let empty = OwnedPostIds::verified(Vec::<String>::new());
    assert_eq!(store.delete_youtube_snapshots_not_owned(&empty).unwrap(), 0);
    assert_eq!(store.snapshot_count().unwrap(), 2);

    let blanks = OwnedPostIds::verified(["   ", ""]);
    assert_eq!(blanks.count(), 0);
    assert_eq!(
        store
            .delete_snapshots_not_owned("youtube", &blanks)
            .unwrap(),
        0
    );
    assert_eq!(store.snapshot_count().unwrap(), 2);
}
