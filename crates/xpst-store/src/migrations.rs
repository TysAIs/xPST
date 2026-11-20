//! Idempotent, additive migrations.
//!
//! The policy is Python's (`AnalyticsStore._migrate`): on every open, read
//! `PRAGMA table_info(metric_snapshots)` and run the guarded
//! `ALTER TABLE ... ADD COLUMN` only for columns that are missing. No table
//! rewrite, no data movement, no drops, no reordering, no `user_version`
//! counter.
//!
//! Why no schema-version integer: Python owns this file. It never writes a
//! version, so a counter set by Rust would be stale the moment Python created
//! or migrated a database — the number would claim a state the file may not be
//! in. Introspection cannot go stale: it asks the database what it has.
//!
//! Consequences that are deliberate:
//! * Opening a Python database is safe: both sides only add what is missing.
//! * Opening twice is a no-op — no DDL runs, so `PRAGMA schema_version` and
//!   `sqlite_master` are untouched.
//! * A *newer* database (columns this build does not know) still opens: reads
//!   name their columns and writes name their columns, so unknown columns keep
//!   their values or their defaults.

use rusqlite::Connection;

use crate::schema::{ANALYTICS_SCHEMA_SQL, FOLLOWER_SCHEMA_SQL};
use crate::StoreError;

/// Columns added to `metric_snapshots` after its first release.
///
/// Statement text is copied from `analytics_store._ADDITIVE_SNAPSHOT_COLUMNS`;
/// SQLite records it in `sqlite_master.sql`, so a migrated ledger looks the
/// same whether Python or Rust performed the upgrade.
pub const ADDITIVE_SNAPSHOT_COLUMNS: &[(&str, &str)] = &[
    (
        "collection_status",
        "ALTER TABLE metric_snapshots ADD COLUMN collection_status TEXT",
    ),
    (
        "collection_error",
        "ALTER TABLE metric_snapshots ADD COLUMN collection_error TEXT",
    ),
];

/// Create whatever is missing, then migrate. Returns the columns this open added.
pub fn apply_schema(conn: &Connection) -> Result<Vec<&'static str>, StoreError> {
    conn.execute_batch(ANALYTICS_SCHEMA_SQL)?;
    conn.execute_batch(FOLLOWER_SCHEMA_SQL)?;
    migrate_metric_snapshots(conn)
}

/// Apply the additive `metric_snapshots` migrations that are still missing.
///
/// Returns the columns added by *this* call, oldest first. An empty vector on
/// an already-current database is the idempotence signal tests assert.
pub fn migrate_metric_snapshots(conn: &Connection) -> Result<Vec<&'static str>, StoreError> {
    let mut added = Vec::new();
    for (column, statement) in ADDITIVE_SNAPSHOT_COLUMNS {
        if has_column(conn, "metric_snapshots", column)? {
            continue;
        }
        match conn.execute_batch(statement) {
            Ok(()) => added.push(*column),
            // Two processes can race the same upgrade (both see the column
            // missing, one wins). That is not a failure: re-read and continue
            // if the column is now present, otherwise surface the error.
            Err(err) if is_duplicate_column(&err) => {
                if !has_column(conn, "metric_snapshots", column)? {
                    return Err(err.into());
                }
            }
            Err(err) => return Err(err.into()),
        }
    }
    Ok(added)
}

fn has_column(conn: &Connection, table: &str, column: &str) -> Result<bool, StoreError> {
    // `table` is always a crate constant, never caller input.
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        if name == column {
            return Ok(true);
        }
    }
    Ok(false)
}

fn is_duplicate_column(err: &rusqlite::Error) -> bool {
    match err {
        rusqlite::Error::SqliteFailure(_, Some(message)) => {
            message.contains("duplicate column name")
        }
        _ => false,
    }
}
