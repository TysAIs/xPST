//! xPST state read path: `state.json` (schema v2) and the schedule file.
//!
//! P0 is read-only on purpose. The Python engine owns the write path (atomic
//! write-then-rename in `state_store.py`), so a Rust reader that opened a file
//! for writing before it understood every field would be the one way to damage
//! live state during the rebuild. This crate therefore:
//!
//! * parses defensively and tolerates unknown keys (the schema will grow),
//! * refuses a version it does not understand instead of guessing,
//! * reports a **missing** schedule file as "absent", not as an error — on a
//!   paused install there is no `~/.xpst/schedule.json` at all.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::Value;
use thiserror::Error;

/// State schema version this reader understands. Writes stay v2 until a
/// documented migration exists (proposal §2).
pub const SUPPORTED_STATE_VERSION: u64 = 2;

#[derive(Debug, Error)]
pub enum StateError {
    #[error("State file not found: {path}")]
    NotFound { path: String },

    #[error("Invalid JSON in {path}: {message}")]
    InvalidJson { path: String, message: String },

    #[error(
        "Unsupported state schema version {version} in {path} (this engine reads v{supported})"
    )]
    UnsupportedVersion {
        path: String,
        version: u64,
        supported: u64,
    },

    #[error("State file {path} is not a JSON object")]
    NotAnObject { path: String },

    #[error("Failed to read {path}: {message}")]
    Io { path: String, message: String },
}

/// Read-only summary of `state.json`. Deliberately a summary and not the whole
/// document: P0 surfaces counts, not per-video business logic.
#[derive(Debug, Clone, Default)]
pub struct StateSummary {
    pub version: u64,
    /// Videos tracked in `posted_videos`.
    pub videos: usize,
    /// Videos with at least one recorded destination.
    pub videos_with_posts: usize,
    /// Recorded posts per destination (deleted ones included — they are history).
    pub posts_per_platform: BTreeMap<String, usize>,
    /// Posts marked `deleted` per destination.
    pub deleted_per_platform: BTreeMap<String, usize>,
    /// Entries in `content_hashes` (dedupe index size).
    pub content_hashes: usize,
    /// `health.total_processed`, when present.
    pub total_processed: Option<u64>,
    /// `health.last_check`, when present.
    pub last_check: Option<String>,
}

/// Parse `state.json` from a path.
pub fn read_state(path: &Path) -> Result<StateSummary, StateError> {
    let text = read_to_string(path)?;
    parse_state(&text, &path.display().to_string())
}

/// Parse `state.json` from memory (tests, fixtures).
pub fn parse_state(text: &str, path: &str) -> Result<StateSummary, StateError> {
    let root: Value = serde_json::from_str(text).map_err(|err| StateError::InvalidJson {
        path: path.to_string(),
        message: err.to_string(),
    })?;
    let object = root.as_object().ok_or_else(|| StateError::NotAnObject {
        path: path.to_string(),
    })?;

    // A missing version is legacy v1 state; the Python engine upgrades that on
    // load, so the reader accepts it and reports what it found rather than
    // inventing a version.
    let version = object.get("version").and_then(Value::as_u64).unwrap_or(1);
    if version > SUPPORTED_STATE_VERSION {
        return Err(StateError::UnsupportedVersion {
            path: path.to_string(),
            version,
            supported: SUPPORTED_STATE_VERSION,
        });
    }

    let mut summary = StateSummary {
        version,
        ..StateSummary::default()
    };

    if let Some(videos) = object.get("posted_videos").and_then(Value::as_object) {
        summary.videos = videos.len();
        for video in videos.values() {
            let Some(posted_to) = video.get("posted_to").and_then(Value::as_object) else {
                continue;
            };
            if !posted_to.is_empty() {
                summary.videos_with_posts += 1;
            }
            for (platform, record) in posted_to {
                *summary
                    .posts_per_platform
                    .entry(platform.clone())
                    .or_insert(0) += 1;
                if record.get("deleted").and_then(Value::as_bool) == Some(true) {
                    *summary
                        .deleted_per_platform
                        .entry(platform.clone())
                        .or_insert(0) += 1;
                }
            }
        }
    }

    if let Some(hashes) = object.get("content_hashes").and_then(Value::as_object) {
        summary.content_hashes = hashes.len();
    }

    if let Some(health) = object.get("health") {
        summary.total_processed = health.get("total_processed").and_then(Value::as_u64);
        summary.last_check = health
            .get("last_check")
            .and_then(Value::as_str)
            .map(str::to_string);
    }

    Ok(summary)
}

/// Read-only summary of the schedule queue.
///
/// `present == false` is a normal, healthy state: posting is paused, so
/// `~/.xpst/schedule.json` does not exist and only parked snapshots remain.
#[derive(Debug, Clone, Default)]
pub struct ScheduleSummary {
    pub present: bool,
    pub entries: usize,
    /// Entry counts by the `status` string found in the file. Kept as raw
    /// strings so a status this build does not know about is reported, not
    /// silently bucketed away.
    pub by_status: BTreeMap<String, usize>,
}

/// Read the schedule file. A missing file is `Ok(present: false)`.
pub fn read_schedule(path: &Path) -> Result<ScheduleSummary, StateError> {
    if !path.exists() {
        return Ok(ScheduleSummary::default());
    }
    let text = read_to_string(path)?;
    parse_schedule(&text, &path.display().to_string())
}

/// Parse a schedule document from memory.
pub fn parse_schedule(text: &str, path: &str) -> Result<ScheduleSummary, StateError> {
    let root: Value = serde_json::from_str(text).map_err(|err| StateError::InvalidJson {
        path: path.to_string(),
        message: err.to_string(),
    })?;
    let entries = root.as_array().ok_or_else(|| StateError::NotAnObject {
        path: path.to_string(),
    })?;

    let mut summary = ScheduleSummary {
        present: true,
        entries: entries.len(),
        by_status: BTreeMap::new(),
    };
    for entry in entries {
        let status = entry
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string();
        *summary.by_status.entry(status).or_insert(0) += 1;
    }
    Ok(summary)
}

fn read_to_string(path: &Path) -> Result<String, StateError> {
    std::fs::read_to_string(path).map_err(|err| {
        if err.kind() == std::io::ErrorKind::NotFound {
            StateError::NotFound {
                path: path.display().to_string(),
            }
        } else {
            StateError::Io {
                path: path.display().to_string(),
                message: err.to_string(),
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const STATE: &str = r#"{
      "version": 2,
      "posted_videos": {
        "vid-one": {
          "source_platform": "local",
          "caption": "hello",
          "posted_to": {
            "youtube": {"id": "abc", "url": "https://youtube.com/shorts/abc", "timestamp": "2026-01-01T00:00:00", "deleted": true},
            "x": {"id": "123", "url": "https://x.com/i/status/123", "timestamp": "2026-01-01T00:00:01"}
          }
        },
        "vid-two": {"source_platform": "local", "posted_to": {}}
      },
      "content_hashes": {"aaa": "vid-one", "bbb": "vid-two"},
      "health": {"total_processed": 7, "last_check": "2026-01-02T03:04:05"}
    }"#;

    #[test]
    fn summarises_v2_state() {
        let s = parse_state(STATE, "state.json").unwrap();
        assert_eq!(s.version, 2);
        assert_eq!(s.videos, 2);
        assert_eq!(s.videos_with_posts, 1);
        assert_eq!(s.posts_per_platform.get("youtube"), Some(&1));
        assert_eq!(s.posts_per_platform.get("x"), Some(&1));
        assert_eq!(s.deleted_per_platform.get("youtube"), Some(&1));
        assert_eq!(s.deleted_per_platform.get("x"), None);
        assert_eq!(s.content_hashes, 2);
        assert_eq!(s.total_processed, Some(7));
        assert_eq!(s.last_check.as_deref(), Some("2026-01-02T03:04:05"));
    }

    #[test]
    fn refuses_a_future_schema_instead_of_guessing() {
        let err = parse_state(r#"{"version": 3, "posted_videos": {}}"#, "s.json").unwrap_err();
        assert!(matches!(
            err,
            StateError::UnsupportedVersion { version: 3, .. }
        ));
        assert!(err
            .to_string()
            .contains("Unsupported state schema version 3"));
    }

    #[test]
    fn missing_version_reads_as_legacy_v1() {
        let s = parse_state(r#"{"posted_videos": {}}"#, "s.json").unwrap();
        assert_eq!(s.version, 1);
    }

    #[test]
    fn tolerates_unknown_fields() {
        let s = parse_state(
            r#"{"version": 2, "posted_videos": {}, "future_section": {"a": 1}}"#,
            "s.json",
        )
        .unwrap();
        assert_eq!(s.videos, 0);
    }

    #[test]
    fn rejects_invalid_json() {
        let err = parse_state("{not json", "s.json").unwrap_err();
        assert!(err.to_string().starts_with("Invalid JSON in s.json:"));
    }

    #[test]
    fn missing_state_file_is_an_error_but_missing_schedule_is_not() {
        let tmp = std::path::Path::new("/nonexistent/xpst-state.json");
        assert!(matches!(
            read_state(tmp).unwrap_err(),
            StateError::NotFound { .. }
        ));
        let schedule = read_schedule(std::path::Path::new("/nonexistent/schedule.json")).unwrap();
        assert!(!schedule.present);
        assert_eq!(schedule.entries, 0);
    }

    #[test]
    fn counts_schedule_entries_by_raw_status() {
        let s = parse_schedule(
            r#"[{"status": "pending"}, {"status": "failed"}, {"status": "pending"}, {}]"#,
            "schedule.json",
        )
        .unwrap();
        assert!(s.present);
        assert_eq!(s.entries, 4);
        assert_eq!(s.by_status.get("pending"), Some(&2));
        assert_eq!(s.by_status.get("failed"), Some(&1));
        assert_eq!(s.by_status.get("unknown"), Some(&1));
    }
}
