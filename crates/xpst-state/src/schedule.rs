//! Read-only view of the schedule queue, including pause state and snapshots.
//!
//! Parity target: `ScheduleManager.list()`, `pause_state()` and
//! `paused_snapshots()` in `src/xpst/schedule_manager.py`, surfaced by
//! `xpst schedule list`.
//!
//! Two behaviours are load-bearing and easy to lose:
//!
//! * **An empty list is not the same fact as a paused queue.** The store can be
//!   empty because nothing is scheduled, or because the queue was paused and
//!   the file renamed to `schedule.json.paused-*`. Every listing therefore
//!   reports the pause marker and any snapshots alongside the entries.
//! * **The store holds UTC; surfaces show the wall clock the user typed.** Each
//!   entry gains `scheduled_time_local`, rendered in the entry's own zone
//!   (`timezone`) when it has one, otherwise the machine zone. A naive stored
//!   time is read as machine-local wall time, which is what the Python
//!   `local_time()` helper does.
//!
//! The reader never writes: no lock file, no directory creation, no quarantine
//! of a corrupt store. Python's manager does create `.schedule.lock` and
//! renames a corrupt file; those are write-path behaviours, and the listing is
//! identical either way.

use std::path::Path;

use serde_json::{json, Map, Value};

use crate::status::parse_instant;
use crate::store::StateError;

/// File names that make up the queue's local state.
pub const SCHEDULE_FILE_NAME: &str = "schedule.json";
pub const PAUSE_FILE_NAME: &str = "schedule.paused.json";
/// Prefix of the sibling snapshots a paused queue can leave behind.
pub const SNAPSHOT_PREFIX: &str = "schedule.json.paused-";

/// Everything `xpst schedule list` reports.
#[derive(Debug, Clone, Default)]
pub struct ScheduleListing {
    /// Entries in store order of `scheduled_time`, each with
    /// `scheduled_time_local`.
    pub entries: Vec<Value>,
    /// `{paused, paused_at, reason}`.
    pub pause: Value,
    /// `{file, entries, modified_at}` per snapshot, sorted by file name.
    pub snapshots: Vec<Value>,
    /// Why a present store file was ignored (non-array, unreadable). Reported
    /// on stderr by the CLI; the listing itself stays empty, like Python's.
    pub problem: Option<String>,
}

impl ScheduleListing {
    /// True when the payload must carry the pause block: the queue is paused or
    /// snapshots exist, so "no entries" cannot be reported as a bare list.
    pub fn needs_pause_block(&self) -> bool {
        self.pause["paused"].as_bool().unwrap_or(false) || !self.snapshots.is_empty()
    }
}

/// Read the queue for a config directory.
pub fn listing(dir: &Path) -> ScheduleListing {
    let mut listing = ScheduleListing {
        pause: pause_state(dir),
        snapshots: snapshots(dir),
        ..ScheduleListing::default()
    };
    let path = dir.join(SCHEDULE_FILE_NAME);
    let Ok(text) = std::fs::read_to_string(&path) else {
        return listing;
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Array(items)) => {
            let mut entries: Vec<Value> = items
                .into_iter()
                .filter(|item| item.is_object())
                .map(with_local_view)
                .collect();
            entries.sort_by(|a, b| sort_key(a).cmp(sort_key(b)));
            listing.entries = entries;
        }
        Ok(other) => {
            listing.problem = Some(format!(
                "Schedule file {} is not a JSON array (got {}); starting with an empty schedule",
                path.display(),
                json_type_name(&other)
            ));
        }
        Err(error) => {
            listing.problem = Some(format!(
                "Schedule file {} could not be read ({error}); starting with an empty schedule",
                path.display()
            ));
        }
    }
    listing
}

/// Python's `type(value).__name__` for the message it logs.
fn json_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn sort_key(entry: &Value) -> &str {
    entry
        .get("scheduled_time")
        .and_then(Value::as_str)
        .unwrap_or("")
}

/// Copy of an entry plus its `scheduled_time_local` rendering.
fn with_local_view(mut entry: Value) -> Value {
    let rendered = local_time(&entry);
    if let Some(object) = entry.as_object_mut() {
        object.insert(
            "scheduled_time_local".to_string(),
            rendered.map_or(Value::Null, Value::String),
        );
    }
    entry
}

/// `ScheduleManager.local_time(entry).isoformat()`.
fn local_time(entry: &Value) -> Option<String> {
    let raw = entry.get("scheduled_time")?.as_str()?;
    let entry_zone = entry
        .get("timezone")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .and_then(|name| jiff::tz::TimeZone::get(name).ok());
    let system = jiff::tz::TimeZone::system();
    let zone = entry_zone.unwrap_or(system.clone());
    let instant = parse_instant(raw)?;
    Some(
        instant
            .to_zoned_datetime(zone)
            .strftime("%Y-%m-%dT%H:%M:%S%.f%:z")
            .to_string(),
    )
}

/// `ScheduleManager.pause_state()`. An unreadable marker fails closed: it still
/// reports paused, because releasing a queue an operator paused is worse than
/// refusing to post.
pub fn pause_state(dir: &Path) -> Value {
    let path = dir.join(PAUSE_FILE_NAME);
    if !path.exists() {
        return json!({"paused": false, "paused_at": null, "reason": ""});
    }
    let parsed = std::fs::read_to_string(&path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_object().cloned());
    let Some(marker) = parsed else {
        return json!({
            "paused": true,
            "paused_at": null,
            "reason": "unreadable pause marker",
        });
    };
    json!({
        "paused": true,
        "paused_at": marker.get("paused_at").cloned().unwrap_or(Value::Null),
        // Python stringifies with `str(... or "")`: a non-string reason is
        // reported rather than dropped.
        "reason": reason_text(marker.get("reason")),
    })
}

fn reason_text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
    }
}

/// `ScheduleManager.paused_snapshots()`.
pub fn snapshots(dir: &Path) -> Vec<Value> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut paths: Vec<std::path::PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with(SNAPSHOT_PREFIX))
        })
        .collect();
    paths.sort();
    let system = jiff::tz::TimeZone::system();
    paths
        .into_iter()
        .map(|path| {
            let entries = std::fs::read_to_string(&path)
                .ok()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                .and_then(|value| value.as_array().map(Vec::len));
            let mtime = std::fs::metadata(&path)
                .and_then(|meta| meta.modified())
                .ok()
                .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok());
            json!({
                "file": path.to_string_lossy(),
                "entries": entries.map_or(Value::Null, |count| json!(count)),
                "modified_at": mtime.map(|elapsed| {
                    let stamp = jiff::Timestamp::new(elapsed.as_secs() as i64, elapsed.subsec_micros() as i32 * 1000)
                        .unwrap_or(jiff::Timestamp::UNIX_EPOCH);
                    stamp
                        .to_zoned_datetime(system.clone())
                        .strftime("%Y-%m-%dT%H:%M:%S%.f")
                        .to_string()
                }),
            })
        })
        .collect()
}

/// The JSON payload `xpst schedule list --json` prints.
///
/// A bare array when nothing is paused and no snapshot exists; the object form
/// once either is true, so "empty" can never read as "the queue was paused and
/// emptied".
pub fn payload(listing: &ScheduleListing) -> Value {
    if !listing.needs_pause_block() {
        return Value::Array(listing.entries.clone());
    }
    let mut object = Map::new();
    object.insert("entries".to_string(), Value::Array(listing.entries.clone()));
    object.insert("paused".to_string(), listing.pause.clone());
    if !listing.snapshots.is_empty() {
        object.insert(
            "paused_snapshots".to_string(),
            Value::Array(listing.snapshots.clone()),
        );
    }
    Value::Object(object)
}

/// The cross-process lock `ScheduleManager._process_lock()` takes.
///
/// Same file name as Python's, so a Rust `schedule pause` and a Python
/// `schedule add` exclude each other instead of interleaving.
pub const PAUSE_LOCK_FILE_NAME: &str = ".schedule.lock";

/// `ScheduleManager.pause()`: create the pause marker.
///
/// Returns `true` when this call created the marker and `false` when the queue
/// was already paused — the distinction the CLI reports as `already_paused`.
///
/// The marker is written exactly as Python writes it (`json.dumps(indent=2)`,
/// no trailing newline, mode 0600) because `schedule list` reads it back and
/// `pause_state()` reports `paused_at` verbatim.
pub fn pause(dir: &Path, reason: &str, now: jiff::Timestamp) -> Result<bool, StateError> {
    let lock_path = dir.join(PAUSE_LOCK_FILE_NAME);
    crate::store::with_exclusive_lock(&lock_path, || {
        let path = dir.join(PAUSE_FILE_NAME);
        if path.exists() {
            return Ok(false);
        }
        let stamp = now
            .to_zoned_datetime(jiff::tz::TimeZone::UTC)
            // `datetime.now(timezone.utc).isoformat()` always carries six
            // fractional digits and a `+00:00` offset.
            .strftime("%Y-%m-%dT%H:%M:%S%.6f%:z")
            .to_string();
        let reason = serde_json::to_string(reason).map_err(|err| StateError::Io {
            path: path.display().to_string(),
            message: format!("cannot encode the pause reason: {err}"),
        })?;
        let payload = format!("{{\n  \"paused_at\": \"{stamp}\",\n  \"reason\": {reason}\n}}");
        crate::store::write_atomic_bytes(&path, payload.as_bytes())?;
        // `write_text_atomic` creates the file 0600; the marker names the
        // operator's reason, which is not a secret but is still profile data.
        restrict_to_owner(&path);
        Ok(true)
    })
}

/// `ScheduleManager.resume()`: remove the pause marker.
///
/// Returns `true` when a marker was removed and `false` when the queue was not
/// paused. Snapshots are deliberately left alone — Python's `resume()` only
/// unlinks the marker, and deleting a snapshot would destroy the only copy of a
/// queue that was renamed aside.
pub fn resume(dir: &Path) -> Result<bool, StateError> {
    let lock_path = dir.join(PAUSE_LOCK_FILE_NAME);
    crate::store::with_exclusive_lock(&lock_path, || {
        let path = dir.join(PAUSE_FILE_NAME);
        match std::fs::remove_file(&path) {
            Ok(()) => Ok(true),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(err) => Err(StateError::Io {
                path: path.display().to_string(),
                message: err.to_string(),
            }),
        }
    })
}

#[cfg(unix)]
fn restrict_to_owner(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_to_owner(_path: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("xpst-schedule-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn missing_store_is_an_empty_bare_list() {
        let dir = scratch("missing");
        let listing = listing(&dir);
        assert!(listing.entries.is_empty());
        assert_eq!(payload(&listing), json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn entries_are_sorted_and_gain_a_local_view() {
        let dir = scratch("sorted");
        std::fs::write(
            dir.join(SCHEDULE_FILE_NAME),
            json!([
                {"id": "late", "scheduled_time": "2026-02-01T12:00:00+00:00"},
                {"id": "none"},
                {"id": "early", "scheduled_time": "2026-01-15T09:30:00+00:00"}
            ])
            .to_string(),
        )
        .unwrap();
        let listing = listing(&dir);
        let ids: Vec<&str> = listing
            .entries
            .iter()
            .map(|entry| entry["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["none", "early", "late"]);
        assert_eq!(listing.entries[0]["scheduled_time_local"], Value::Null);
        // Rendered in the machine zone (no `timezone` on the entry), so the
        // offset differs per machine — the *instant* must not.
        let rendered = listing.entries[1]["scheduled_time_local"].as_str().unwrap();
        assert_eq!(
            parse_instant(rendered).unwrap(),
            parse_instant("2026-01-15T09:30:00+00:00").unwrap()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_paused_queue_reports_the_pause_and_keeps_entries() {
        let dir = scratch("paused");
        std::fs::write(dir.join(SCHEDULE_FILE_NAME), "[]").unwrap();
        std::fs::write(
            dir.join(PAUSE_FILE_NAME),
            json!({"paused_at": "2026-01-01T00:00:00+00:00", "reason": "operator"}).to_string(),
        )
        .unwrap();
        let value = payload(&listing(&dir));
        assert_eq!(value["paused"]["paused"], json!(true));
        assert_eq!(value["paused"]["reason"], json!("operator"));
        assert_eq!(value["entries"], json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unreadable_pause_marker_fails_closed() {
        let dir = scratch("pause-corrupt");
        std::fs::write(dir.join(PAUSE_FILE_NAME), "[1,2,3]").unwrap();
        let value = payload(&listing(&dir));
        assert_eq!(value["paused"]["paused"], json!(true));
        assert_eq!(value["paused"]["reason"], json!("unreadable pause marker"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_snapshot_is_reported_with_its_entry_count() {
        let dir = scratch("snapshot");
        std::fs::write(
            dir.join(format!("{SNAPSHOT_PREFIX}20260101-000000")),
            json!([{"id": "a"}, {"id": "b"}]).to_string(),
        )
        .unwrap();
        let value = payload(&listing(&dir));
        assert_eq!(value["paused_snapshots"][0]["entries"], json!(2));
        assert_eq!(value["entries"], json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_non_array_store_is_empty_with_a_problem_note() {
        let dir = scratch("non-array");
        std::fs::write(dir.join(SCHEDULE_FILE_NAME), "{}").unwrap();
        let listing = listing(&dir);
        assert!(listing.entries.is_empty());
        assert!(listing
            .problem
            .as_deref()
            .unwrap_or("")
            .contains("not a JSON array (got dict)"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn non_object_entries_are_dropped_like_python_does() {
        let dir = scratch("non-object");
        std::fs::write(
            dir.join(SCHEDULE_FILE_NAME),
            json!([{"id": "keep"}, 7, "text", null]).to_string(),
        )
        .unwrap();
        let listing = listing(&dir);
        assert_eq!(listing.entries.len(), 1);
        assert_eq!(listing.entries[0]["id"], json!("keep"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn pause_writes_the_marker_python_writes_and_is_idempotent() {
        let dir = scratch("pause-write");
        let now = parse_instant("2026-01-02T03:04:05.678901+00:00").unwrap();
        assert!(pause(&dir, "operator pause", now).unwrap());
        let written = std::fs::read_to_string(dir.join(PAUSE_FILE_NAME)).unwrap();
        assert_eq!(
            written,
            "{\n  \"paused_at\": \"2026-01-02T03:04:05.678901+00:00\",\n  \"reason\": \"operator pause\"\n}",
            "the marker must be byte-identical to json.dumps(indent=2)"
        );
        // A second pause reports "already paused" and leaves the first marker.
        assert!(!pause(&dir, "second reason", now).unwrap());
        assert_eq!(
            std::fs::read_to_string(dir.join(PAUSE_FILE_NAME)).unwrap(),
            written
        );
        // The lock file is created, as Python's `_process_lock` does.
        assert!(dir.join(PAUSE_LOCK_FILE_NAME).exists());
        // And the listing sees it.
        let listing = listing(&dir);
        assert_eq!(listing.pause["paused"], json!(true));
        assert_eq!(listing.pause["reason"], json!("operator pause"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn pause_escapes_a_reason_that_would_break_the_json() {
        let dir = scratch("pause-escape");
        let now = parse_instant("2026-01-02T03:04:05+00:00").unwrap();
        pause(&dir, "quote \" and \\ backslash", now).unwrap();
        let written = std::fs::read_to_string(dir.join(PAUSE_FILE_NAME)).unwrap();
        let parsed: Value = serde_json::from_str(&written).unwrap();
        assert_eq!(parsed["reason"], json!("quote \" and \\ backslash"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn resume_removes_only_the_marker_and_reports_whether_it_was_paused() {
        let dir = scratch("resume");
        let now = parse_instant("2026-01-02T03:04:05+00:00").unwrap();
        pause(&dir, "why", now).unwrap();
        let snapshot = dir.join(format!("{SNAPSHOT_PREFIX}20260101-000000"));
        std::fs::write(&snapshot, "[]").unwrap();

        assert!(resume(&dir).unwrap(), "a paused queue resumes");
        assert!(!dir.join(PAUSE_FILE_NAME).exists());
        assert!(
            snapshot.exists(),
            "resume must not delete a pause snapshot — it is the only copy"
        );
        assert!(!resume(&dir).unwrap(), "an unpaused queue reports false");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
