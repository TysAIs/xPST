//! The xPST SQLite store: `analytics.db`, the only database in the product
//! besides the knowledge vector store.
//!
//! This crate owns the analytics ledger that `src/xpst/analytics_store.py`
//! currently writes: per-post metric snapshots, follower snapshots, cross-post
//! groups and per-platform collection-run outcomes, all in one SQLite file.
//!
//! Three promises, each backed by a test:
//!
//! 1. **Schema parity.** [`schema`] carries the CREATE statements copied
//!    verbatim from Python, so a database created here is indistinguishable
//!    from one Python created — same `sqlite_master` text, column order,
//!    declared types, NOT NULL flags, primary keys and indexes.
//!    `tests/schema_parity.rs` proves it in both directions with the real
//!    Python module.
//! 2. **Safe migrations.** [`migrations`] only ever adds missing columns, so
//!    opening an older ledger upgrades it without data movement and opening it
//!    twice runs no DDL at all.
//! 3. **Ownership.** [`ownership`] ports the collector's fail-closed ownership
//!    gate into a type, and [`store::Store::delete_snapshots_not_owned`] keeps
//!    the purge side of the same invariant.
//!
//! Out of scope for this slice: the knowledge vector store, the analytics
//! collectors, HTTP and the CLI.
//!
//! ```no_run
//! use xpst_store::{Metrics, NewSnapshot, Store};
//!
//! # fn main() -> Result<(), xpst_store::StoreError> {
//! let store = Store::open("xpst-store-doc-demo/analytics.db")?;
//! store.record_snapshots(&[NewSnapshot::new(
//!     "youtube",
//!     "fic-vid-0001",
//!     "2026-11-17T10:00:00+00:00",
//! )
//! .metrics(Metrics { views: Some(12), ..Metrics::default() })])?;
//! assert_eq!(store.snapshot_count()?, 1);
//! # Ok(())
//! # }
//! ```

#![forbid(unsafe_code)]

pub mod migrations;
pub mod ownership;
pub mod schema;
pub mod store;

pub use migrations::{apply_schema, migrate_metric_snapshots, ADDITIVE_SNAPSHOT_COLUMNS};
pub use ownership::{
    is_gated_platform, Admission, OwnedPostIds, OwnershipEvidence, OwnershipGate,
    STATE_OWNERSHIP_PLATFORMS,
};
pub use store::{
    CollectionRunRow, CollectionStatus, CrossPostGroup, JournalMode, Metrics, NewCollectionRun,
    NewSnapshot, OpenOptions, RecordOutcome, SnapshotRow, Store,
};

/// Everything that can go wrong opening or using the ledger.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("store configuration: {0}")]
    Config(String),
}
