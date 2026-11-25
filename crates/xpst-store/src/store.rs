//! The SQLite handles and the read/write surface over `analytics.db`.
//!
//! Scope is the analytics ledger Python owns in `src/xpst/analytics_store.py`:
//! metric snapshots, follower snapshots, cross-post groups and collection-run
//! outcomes. The knowledge vector store is a different database and is out of
//! scope.
//!
//! # Journal mode
//!
//! Python opens this file with the default rollback journal (`journal_mode` is
//! never set). [`JournalMode::Preserve`] — the default here — leaves that
//! alone. Switching to WAL rewrites the database header, adds `-wal`/`-shm`
//! siblings and makes a plain file copy an unsafe backup, so it must be a
//! deliberate choice, not a side effect of the Rust store opening a file the
//! Python engine still writes. [`JournalMode::Wal`] exists for the point where
//! the Rust engine is the only writer; it is opt-in and covered by a test.
//!
//! Write contention is handled the way SQLite intends: every transaction is
//! short, and the busy timeout matches Python's `timeout=10`.

use std::fmt;
use std::path::Path;
use std::time::Duration;

use rusqlite::{params, Connection, OptionalExtension};

use crate::migrations::apply_schema;
use crate::ownership::{Admission, OwnedPostIds, OwnershipGate};
use crate::StoreError;

/// `analytics_store.COLLECTION_STATUS_*}`, including the explicit `unknown`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CollectionStatus {
    /// Rows written before status was tracked, or callers that declare none.
    /// Never reported as a successful collection.
    #[default]
    Unknown,
    Ok,
    Partial,
    Failed,
    Unavailable,
    NotCollected,
}

impl CollectionStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Ok => "ok",
            Self::Partial => "partial",
            Self::Failed => "failed",
            Self::Unavailable => "unavailable",
            Self::NotCollected => "not_collected",
        }
    }

    /// Coerce stored/declared text to the known vocabulary.
    ///
    /// Unrecognised text becomes `Unknown`, never `Ok` — the exact rule of
    /// `normalise_collection_status`.
    pub fn parse(text: &str) -> Self {
        match text.trim().to_ascii_lowercase().as_str() {
            "ok" => Self::Ok,
            "partial" => Self::Partial,
            "failed" => Self::Failed,
            "unavailable" => Self::Unavailable,
            "not_collected" => Self::NotCollected,
            _ => Self::Unknown,
        }
    }

    /// Whether the row's numbers may be shown as real measurements.
    pub fn is_trusted(self) -> bool {
        matches!(self, Self::Ok | Self::Partial)
    }
}

impl fmt::Display for CollectionStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The six core counters. `None` is "the platform did not report this", which
/// is not the same as a reported zero.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Metrics {
    pub views: Option<i64>,
    pub likes: Option<i64>,
    pub comments: Option<i64>,
    pub shares: Option<i64>,
    pub reposts: Option<i64>,
    pub saves: Option<i64>,
}

/// A snapshot to persist.
///
/// `captured_at` is caller-supplied and stored verbatim. Python normalises it
/// (naive → UTC, invalid text preserved); that belongs to the collector port,
/// not to the store, so this crate does not invent a clock.
#[derive(Debug, Clone, Default)]
pub struct NewSnapshot {
    pub platform: String,
    pub post_id: String,
    pub captured_at: String,
    pub metrics: Metrics,
    /// Platform-specific fields Python keeps in the `extra` JSON column, as
    /// already-serialised JSON. Opaque here; merging it is a read-model concern.
    pub extra_json: Option<String>,
    pub collection_status: Option<CollectionStatus>,
    pub collection_error: Option<String>,
}

impl NewSnapshot {
    #[must_use]
    pub fn new(
        platform: impl Into<String>,
        post_id: impl Into<String>,
        captured_at: impl Into<String>,
    ) -> Self {
        Self {
            platform: platform.into(),
            post_id: post_id.into(),
            captured_at: captured_at.into(),
            ..Self::default()
        }
    }

    #[must_use]
    pub fn metrics(mut self, metrics: Metrics) -> Self {
        self.metrics = metrics;
        self
    }

    #[must_use]
    pub fn extra_json(mut self, extra_json: impl Into<String>) -> Self {
        self.extra_json = Some(extra_json.into());
        self
    }

    #[must_use]
    pub fn status(mut self, status: CollectionStatus) -> Self {
        self.collection_status = Some(status);
        self
    }

    #[must_use]
    pub fn error(mut self, error: impl Into<String>) -> Self {
        self.collection_error = Some(error.into());
        self
    }
}

/// A stored snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotRow {
    pub platform: String,
    pub post_id: String,
    pub captured_at: String,
    pub views: Option<i64>,
    pub likes: Option<i64>,
    pub comments: Option<i64>,
    pub shares: Option<i64>,
    pub reposts: Option<i64>,
    pub saves: Option<i64>,
    pub extra_json: Option<String>,
    /// Already coerced: legacy NULL becomes [`CollectionStatus::Unknown`].
    pub collection_status: CollectionStatus,
    pub collection_error: Option<String>,
}

/// One platform's collection outcome.
#[derive(Debug, Clone)]
pub struct NewCollectionRun {
    pub platform: String,
    pub captured_at: String,
    pub status: CollectionStatus,
    pub error: Option<String>,
    pub reason: Option<String>,
    pub detail: Option<String>,
    pub attempted: Option<i64>,
    pub failures: Option<i64>,
    pub requested: Option<i64>,
    pub collected: Option<i64>,
}

impl NewCollectionRun {
    #[must_use]
    pub fn new(
        platform: impl Into<String>,
        captured_at: impl Into<String>,
        status: CollectionStatus,
    ) -> Self {
        Self {
            platform: platform.into(),
            captured_at: captured_at.into(),
            status,
            error: None,
            reason: None,
            detail: None,
            attempted: None,
            failures: None,
            requested: None,
            collected: None,
        }
    }
}

/// A stored collection outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CollectionRunRow {
    pub platform: String,
    pub captured_at: String,
    pub status: CollectionStatus,
    pub error: Option<String>,
    pub reason: Option<String>,
    pub detail: Option<String>,
    pub attempted: Option<i64>,
    pub failures: Option<i64>,
    pub requested: Option<i64>,
    pub collected: Option<i64>,
}

/// A stored cross-post group.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CrossPostGroup {
    pub content_hash: String,
    pub video_id: String,
    pub caption: Option<String>,
    pub source_url: Option<String>,
    pub created_at: String,
    pub platforms_json: String,
}

/// What a gated write did, so callers can log it the way Python does.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RecordOutcome {
    pub written: usize,
    pub skipped_missing_identity: usize,
    pub skipped_unverifiable: usize,
    pub skipped_not_owned: usize,
}

/// Journal-mode policy for a database file Python also opens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum JournalMode {
    /// Change nothing (Python's default rollback journal, or whatever the file
    /// already uses). Downgrading a WAL database is also a change, so it is
    /// never done implicitly.
    #[default]
    Preserve,
    /// Opt in to WAL, persistently. Only for a single-writer Rust engine.
    Wal,
}

/// How to open the ledger.
#[derive(Debug, Clone)]
pub struct OpenOptions {
    pub journal_mode: JournalMode,
    pub busy_timeout: Duration,
}

impl Default for OpenOptions {
    fn default() -> Self {
        Self {
            journal_mode: JournalMode::Preserve,
            // Python connects with `timeout=10`.
            busy_timeout: Duration::from_secs(10),
        }
    }
}

/// A connection to `analytics.db`, with the schema ensured on open.
pub struct Store {
    conn: Connection,
    applied_migrations: Vec<&'static str>,
}

impl Store {
    /// Open (creating if needed) the ledger at `path`, applying additive
    /// migrations and preserving the journal mode.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, StoreError> {
        Self::open_with(path, OpenOptions::default())
    }

    pub fn open_with(path: impl AsRef<Path>, options: OpenOptions) -> Result<Self, StoreError> {
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let conn = Connection::open(path)?;
        Self::finish_open(conn, options)
    }

    /// In-memory ledger, for tests.
    pub fn open_in_memory() -> Result<Self, StoreError> {
        Self::finish_open(Connection::open_in_memory()?, OpenOptions::default())
    }

    fn finish_open(conn: Connection, options: OpenOptions) -> Result<Self, StoreError> {
        conn.busy_timeout(options.busy_timeout)?;
        if options.journal_mode == JournalMode::Wal {
            // Returns the mode actually achieved; some filesystems refuse WAL.
            let mode: String = conn.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))?;
            if !mode.eq_ignore_ascii_case("wal") {
                return Err(StoreError::Config(format!(
                    "journal_mode=WAL requested but SQLite reported {mode:?}"
                )));
            }
        }
        let applied_migrations = apply_schema(&conn)?;
        Ok(Self {
            conn,
            applied_migrations,
        })
    }

    /// Columns added by the migrations this open executed. Empty means the
    /// database was already current, which is the idempotence signal.
    pub fn applied_migrations(&self) -> &[&'static str] {
        &self.applied_migrations
    }

    /// The journal mode the connection is actually using.
    pub fn journal_mode(&self) -> Result<String, StoreError> {
        Ok(self
            .conn
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))?)
    }

    // ── metric snapshots ────────────────────────────────────────────────

    /// Persist one snapshot per row. Rows without a platform or post id are
    /// skipped, matching Python's identity check.
    pub fn record_snapshots(&self, rows: &[NewSnapshot]) -> Result<usize, StoreError> {
        let usable: Vec<&NewSnapshot> = rows
            .iter()
            .filter(|row| !row.platform.is_empty() && !row.post_id.is_empty())
            .collect();
        self.insert_snapshots(&usable)
    }

    /// Persist only rows the [`OwnershipGate`] admits.
    ///
    /// This is the store-level form of `_filter_owned_snapshots`: the gate runs
    /// before anything is written, and refused rows are counted by reason so a
    /// caller can warn once per id instead of guessing.
    pub fn record_snapshots_gated(
        &self,
        gate: &OwnershipGate,
        rows: &[NewSnapshot],
    ) -> Result<RecordOutcome, StoreError> {
        let mut outcome = RecordOutcome::default();
        let mut admitted: Vec<&NewSnapshot> = Vec::with_capacity(rows.len());
        for row in rows {
            if row.platform.is_empty() || row.post_id.is_empty() {
                outcome.skipped_missing_identity += 1;
                continue;
            }
            match gate.admit(&row.platform, &row.post_id) {
                Admission::Owned | Admission::UngatedPlatform => admitted.push(row),
                Admission::NotOwned => outcome.skipped_not_owned += 1,
                Admission::Unverifiable => outcome.skipped_unverifiable += 1,
            }
        }
        outcome.written = self.insert_snapshots(&admitted)?;
        Ok(outcome)
    }

    fn insert_snapshots(&self, rows: &[&NewSnapshot]) -> Result<usize, StoreError> {
        if rows.is_empty() {
            return Ok(0);
        }
        let tx = self.conn.unchecked_transaction()?;
        {
            let mut stmt = tx.prepare(
                "
                INSERT OR REPLACE INTO metric_snapshots
                (platform, post_id, captured_at, views, likes, comments,
                 shares, reposts, saves, extra, collection_status,
                 collection_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
            )?;
            for row in rows {
                let body = row.extra_json.as_deref().filter(|text| !text.is_empty());
                let error = row
                    .collection_error
                    .as_deref()
                    .filter(|text| !text.is_empty())
                    .map(|text| truncate_chars(text, 240));
                stmt.execute(params![
                    row.platform,
                    row.post_id,
                    row.captured_at,
                    row.metrics.views,
                    row.metrics.likes,
                    row.metrics.comments,
                    row.metrics.shares,
                    row.metrics.reposts,
                    row.metrics.saves,
                    body,
                    row.collection_status.unwrap_or_default().as_str(),
                    error,
                ])?;
            }
        }
        tx.commit()?;
        Ok(rows.len())
    }

    /// Latest snapshot per post, optionally filtered by platform.
    pub fn latest(&self, platform: Option<&str>) -> Result<Vec<SnapshotRow>, StoreError> {
        const BASE: &str = "
            SELECT s.* FROM metric_snapshots s
            JOIN (
                SELECT platform, post_id, MAX(captured_at) AS captured_at
                FROM metric_snapshots GROUP BY platform, post_id
            ) m ON s.platform = m.platform AND s.post_id = m.post_id
               AND s.captured_at = m.captured_at
        ";
        // Python leaves the order undefined; SQLite returns the grouped rows in
        // `(platform, post_id)` order (the GROUP BY key), and `xpst_analytics`
        // serves `latest()` verbatim, so that is the order pinned here.
        const ORDER: &str = " ORDER BY s.platform, s.post_id";
        let mut rows = Vec::new();
        match platform {
            Some(platform) => {
                let sql = format!("{BASE} WHERE s.platform = ?{ORDER}");
                let mut stmt = self.conn.prepare(&sql)?;
                let mut query = stmt.query(params![platform])?;
                while let Some(row) = query.next()? {
                    rows.push(snapshot_from_row(row)?);
                }
            }
            None => {
                let sql = format!("{BASE}{ORDER}");
                let mut stmt = self.conn.prepare(&sql)?;
                let mut query = stmt.query([])?;
                while let Some(row) = query.next()? {
                    rows.push(snapshot_from_row(row)?);
                }
            }
        }
        Ok(rows)
    }

    /// Snapshot history for one post, newest first.
    pub fn history(
        &self,
        platform: &str,
        post_id: &str,
        limit: u32,
    ) -> Result<Vec<SnapshotRow>, StoreError> {
        let mut stmt = self.conn.prepare(
            "
            SELECT * FROM metric_snapshots
            WHERE platform = ? AND post_id = ?
            ORDER BY captured_at DESC LIMIT ?
            ",
        )?;
        let mut query = stmt.query(params![platform, post_id, limit])?;
        let mut rows = Vec::new();
        while let Some(row) = query.next()? {
            rows.push(snapshot_from_row(row)?);
        }
        Ok(rows)
    }

    pub fn snapshot_count(&self) -> Result<i64, StoreError> {
        Ok(self
            .conn
            .query_row("SELECT COUNT(*) FROM metric_snapshots", [], |row| {
                row.get(0)
            })?)
    }

    /// Delete rows for `platform` whose id is not in `owned`.
    ///
    /// `owned` is documented as verified, but an empty set is still refused:
    /// an empty verified set and incomplete evidence are indistinguishable from
    /// here, and deleting a whole platform's history is not recoverable. Python
    /// applies the same guard.
    pub fn delete_snapshots_not_owned(
        &self,
        platform: &str,
        owned: &OwnedPostIds,
    ) -> Result<usize, StoreError> {
        if platform.is_empty() || owned.is_empty() {
            return Ok(0);
        }
        let tx = self.conn.unchecked_transaction()?;
        tx.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _xpst_owned_snap (id TEXT PRIMARY KEY)",
            [],
        )?;
        tx.execute("DELETE FROM _xpst_owned_snap", [])?;
        {
            let mut stmt = tx.prepare("INSERT OR IGNORE INTO _xpst_owned_snap (id) VALUES (?)")?;
            for id in owned.iter() {
                stmt.execute(params![id])?;
            }
        }
        let deleted = tx.execute(
            "
            DELETE FROM metric_snapshots
            WHERE platform = ?
              AND NOT EXISTS (
                  SELECT 1 FROM _xpst_owned_snap o
                  WHERE o.id = metric_snapshots.post_id
              )
            ",
            params![platform],
        )?;
        tx.execute("DROP TABLE _xpst_owned_snap", [])?;
        tx.commit()?;
        Ok(deleted)
    }

    /// YouTube-specific wrapper, mirroring `delete_youtube_snapshots_not_in`.
    pub fn delete_youtube_snapshots_not_owned(
        &self,
        owned: &OwnedPostIds,
    ) -> Result<usize, StoreError> {
        self.delete_snapshots_not_owned("youtube", owned)
    }

    // ── collection runs ────────────────────────────────────────────────

    /// Persist per-platform collection outcomes, including total failures.
    pub fn record_collection_runs(
        &self,
        entries: &[NewCollectionRun],
    ) -> Result<usize, StoreError> {
        let tx = self.conn.unchecked_transaction()?;
        let mut written = 0usize;
        {
            let mut stmt = tx.prepare(
                "
                INSERT OR REPLACE INTO analytics_collection_runs
                (platform, captured_at, status, error, reason, detail,
                 attempted, failures, requested, collected)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
            )?;
            for entry in entries {
                // Python's `str(...).strip()` then falsy check on the platform.
                if entry.platform.trim().is_empty() {
                    continue;
                }
                let error = entry
                    .error
                    .as_deref()
                    .filter(|text| !text.is_empty())
                    .map(|text| truncate_chars(text, 240));
                stmt.execute(params![
                    entry.platform.trim().to_ascii_lowercase(),
                    entry.captured_at,
                    entry.status.as_str(),
                    error,
                    entry.reason,
                    entry.detail,
                    entry.attempted,
                    entry.failures,
                    entry.requested,
                    entry.collected,
                ])?;
                written += 1;
            }
        }
        tx.commit()?;
        Ok(written)
    }

    /// Newest recorded run per platform.
    pub fn latest_collection_runs(
        &self,
    ) -> Result<std::collections::BTreeMap<String, CollectionRunRow>, StoreError> {
        let mut stmt = self.conn.prepare(
            "
            SELECT r.* FROM analytics_collection_runs r
            JOIN (
                SELECT platform, MAX(captured_at) AS captured_at
                FROM analytics_collection_runs GROUP BY platform
            ) m ON r.platform = m.platform AND r.captured_at = m.captured_at
            ORDER BY r.platform
            ",
        )?;
        let mut query = stmt.query([])?;
        let mut runs = std::collections::BTreeMap::new();
        while let Some(row) = query.next()? {
            let run = CollectionRunRow {
                platform: row.get("platform")?,
                captured_at: row.get("captured_at")?,
                status: CollectionStatus::parse(&row.get::<_, String>("status")?),
                error: row.get("error")?,
                reason: row.get("reason")?,
                detail: row.get("detail")?,
                attempted: row.get("attempted")?,
                failures: row.get("failures")?,
                requested: row.get("requested")?,
                collected: row.get("collected")?,
            };
            runs.insert(run.platform.to_ascii_lowercase(), run);
        }
        Ok(runs)
    }

    // ── follower snapshots ─────────────────────────────────────────────

    pub fn record_followers(
        &self,
        platform: &str,
        count: i64,
        captured_at: &str,
    ) -> Result<(), StoreError> {
        self.conn.execute(
            "INSERT OR REPLACE INTO follower_snapshots (platform, count, captured_at) VALUES (?, ?, ?)",
            params![platform, count, captured_at],
        )?;
        Ok(())
    }

    /// Most recent follower count per platform.
    pub fn latest_followers(
        &self,
    ) -> Result<std::collections::BTreeMap<String, (i64, String)>, StoreError> {
        let mut stmt = self.conn.prepare(
            "
            SELECT f.* FROM follower_snapshots f
            JOIN (
                SELECT platform, MAX(captured_at) AS captured_at
                FROM follower_snapshots GROUP BY platform
            ) m ON f.platform = m.platform AND f.captured_at = m.captured_at
            ORDER BY f.platform
            ",
        )?;
        let mut query = stmt.query([])?;
        let mut latest = std::collections::BTreeMap::new();
        while let Some(row) = query.next()? {
            latest.insert(
                row.get::<_, String>("platform")?,
                (row.get("count")?, row.get("captured_at")?),
            );
        }
        Ok(latest)
    }

    /// Follower history for one platform, oldest first.
    ///
    /// `AnalyticsStore.follower_history` selects the newest `limit` rows and
    /// reverses them, so the payload reads oldest → newest.
    pub fn follower_history(
        &self,
        platform: &str,
        limit: u32,
    ) -> Result<Vec<(i64, String)>, StoreError> {
        let mut stmt = self.conn.prepare(
            "
            SELECT count, captured_at FROM follower_snapshots
            WHERE platform = ? ORDER BY captured_at DESC LIMIT ?
            ",
        )?;
        let mut query = stmt.query(params![platform, limit])?;
        let mut rows = Vec::new();
        while let Some(row) = query.next()? {
            rows.push((row.get("count")?, row.get("captured_at")?));
        }
        rows.reverse();
        Ok(rows)
    }

    // ── cross-post groups ──────────────────────────────────────────────

    pub fn record_cross_post_group(&self, group: &CrossPostGroup) -> Result<(), StoreError> {
        self.conn.execute(
            "
            INSERT OR REPLACE INTO cross_post_groups
            (content_hash, video_id, caption, source_url, created_at,
             platforms_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ",
            params![
                group.content_hash,
                group.video_id,
                group.caption,
                group.source_url,
                group.created_at,
                group.platforms_json,
            ],
        )?;
        Ok(())
    }

    pub fn get_cross_post_group(
        &self,
        content_hash: &str,
    ) -> Result<Option<CrossPostGroup>, StoreError> {
        Ok(self
            .conn
            .query_row(
                "SELECT * FROM cross_post_groups WHERE content_hash = ?",
                params![content_hash],
                cross_post_group_from_row,
            )
            .optional()?)
    }

    /// `AnalyticsStore.get_cross_post_groups()` — newest first.
    ///
    /// Python's default is `limit=50, offset=0`; the MCP tool takes no
    /// arguments, so the defaults are the contract.
    pub fn get_cross_post_groups(
        &self,
        limit: i64,
        offset: i64,
    ) -> Result<Vec<CrossPostGroup>, StoreError> {
        let mut stmt = self.conn.prepare(
            "
            SELECT * FROM cross_post_groups
            ORDER BY created_at DESC LIMIT ? OFFSET ?
            ",
        )?;
        let mut query = stmt.query(params![limit, offset])?;
        let mut rows = Vec::new();
        while let Some(row) = query.next()? {
            rows.push(cross_post_group_from_row(row)?);
        }
        Ok(rows)
    }

    /// `AnalyticsStore.latest_for_post()` — every snapshot for one post,
    /// oldest first, so the last element is the most recent.
    pub fn latest_for_post(
        &self,
        platform: &str,
        post_id: &str,
    ) -> Result<Vec<SnapshotRow>, StoreError> {
        let mut stmt = self.conn.prepare(
            "
            SELECT * FROM metric_snapshots
            WHERE platform = ? AND post_id = ?
            ORDER BY captured_at ASC
            ",
        )?;
        let mut query = stmt.query(params![platform, post_id])?;
        let mut rows = Vec::new();
        while let Some(row) = query.next()? {
            rows.push(snapshot_from_row(row)?);
        }
        Ok(rows)
    }
}

fn cross_post_group_from_row(row: &rusqlite::Row<'_>) -> Result<CrossPostGroup, rusqlite::Error> {
    Ok(CrossPostGroup {
        content_hash: row.get("content_hash")?,
        video_id: row.get("video_id")?,
        caption: row.get("caption")?,
        source_url: row.get("source_url")?,
        created_at: row.get("created_at")?,
        platforms_json: row.get("platforms_json")?,
    })
}

fn snapshot_from_row(row: &rusqlite::Row<'_>) -> Result<SnapshotRow, rusqlite::Error> {
    Ok(SnapshotRow {
        platform: row.get("platform")?,
        post_id: row.get("post_id")?,
        captured_at: row.get("captured_at")?,
        views: row.get("views")?,
        likes: row.get("likes")?,
        comments: row.get("comments")?,
        shares: row.get("shares")?,
        reposts: row.get("reposts")?,
        saves: row.get("saves")?,
        extra_json: row.get("extra")?,
        collection_status: CollectionStatus::parse(
            &row.get::<_, Option<String>>("collection_status")?
                .unwrap_or_default(),
        ),
        collection_error: row.get("collection_error")?,
    })
}

/// Python slices `str(error)` to 240 characters; count characters, not bytes.
fn truncate_chars(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}
