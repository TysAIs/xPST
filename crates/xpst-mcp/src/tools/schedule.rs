//! `xpst_schedule_list` — the scheduled-post queue, its pause state, and any
//! renamed `schedule.json.paused-*` snapshots.
//!
//! Ports `_handle_schedule_list()`: entries sorted by `scheduled_time`, each
//! with a `scheduled_time_local` display value, the pause marker's state, and
//! — when a paused snapshot exists — the `note` that stops an empty queue from
//! reading as "the user's posts were deleted".

use jiff::tz::TimeZone;
use jiff::Timestamp;
use serde_json::{json, Value};

use crate::error::ToolError;

use super::stamp;
use super::ToolContext;

pub const SNAPSHOT_NOTE: &str = "A schedule.json.paused-* snapshot exists in the store dir: \
     the queue may have been PAUSED (renamed), not emptied. Verify before concluding entries \
     were cancelled.";

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let config_dir = ctx.paths.config_dir().to_path_buf();
    let schedule_path = ctx.paths.schedule_file();

    let mut entries: Vec<Value> = Vec::new();
    if schedule_path.exists() {
        let text = std::fs::read_to_string(&schedule_path).map_err(|err| ToolError::Malformed {
            kind: "schedule file",
            path: schedule_path.display().to_string(),
            detail: err.to_string(),
        })?;
        let parsed: Value = serde_json::from_str(&text).map_err(|err| ToolError::Malformed {
            kind: "schedule file",
            path: schedule_path.display().to_string(),
            detail: err.to_string(),
        })?;
        entries = match parsed {
            Value::Array(items) => items,
            _ => {
                return Err(ToolError::Malformed {
                    kind: "schedule file",
                    path: schedule_path.display().to_string(),
                    detail: "expected a JSON array of entries".to_string(),
                })
            }
        };
    }

    entries.sort_by_key(|entry| {
        entry
            .get("scheduled_time")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    });
    let entries: Vec<Value> = entries.iter().map(with_local_view).collect();

    let mut payload = serde_json::Map::new();
    payload.insert("schedules".to_string(), Value::Array(entries));
    payload.insert("paused".to_string(), pause_state(ctx));

    let snapshots = paused_snapshots(&config_dir);
    if !snapshots.is_empty() {
        payload.insert("paused_snapshots".to_string(), Value::Array(snapshots));
        payload.insert("note".to_string(), json!(SNAPSHOT_NOTE));
    }

    Ok(Value::Object(payload))
}

/// `_with_local_view()`: a copy plus its display rendering, `null` when the
/// stored time cannot be parsed (a corrupt entry must not fail the listing).
fn with_local_view(entry: &Value) -> Value {
    let mut view = entry.clone();
    let rendered = entry
        .get("scheduled_time")
        .and_then(Value::as_str)
        .and_then(|raw| local_iso(entry, raw));
    if let Value::Object(map) = &mut view {
        map.insert(
            "scheduled_time_local".to_string(),
            rendered.map(Value::String).unwrap_or(Value::Null),
        );
    }
    view
}

/// The entry's instant rendered in its own zone (`entry.timezone` when the
/// store knows it, otherwise the machine's local zone).
///
/// The zone name is IANA data, not a Rust enum: an unknown name falls back to
/// the local zone (as the Python engine does) instead of failing the listing.
fn local_iso(entry: &Value, raw: &str) -> Option<String> {
    let normalized = raw.replacen(' ', "T", 1);
    let zone = entry
        .get("timezone")
        .and_then(Value::as_str)
        .map(zone_or_local);

    if let Some(instant) = stamp::parse_instant(&normalized) {
        let zone = zone.unwrap_or_else(TimeZone::system);
        return Some(stamp::render(&instant.to_zoned_datetime(zone)));
    }
    // A naive stamp is local wall-clock time (legacy entries). A wall clock
    // that a DST transition makes ambiguous or impossible has no single
    // instant, so it renders as null rather than being silently shifted.
    let naive = stamp::parse_naive(&normalized)?;
    let zoned = TimeZone::system()
        .to_ambiguous_zoned_datetime(naive)
        .unambiguous()
        .ok()?;
    Some(stamp::render(&zoned))
}

/// An IANA name to a zone, falling back to the machine's zone.
fn zone_or_local(name: &str) -> TimeZone {
    TimeZone::get(name).unwrap_or_else(|_| TimeZone::system())
}

/// `pause_state()`: the marker fails closed — an unreadable marker means
/// "paused", never "free to post".
fn pause_state(ctx: &ToolContext) -> Value {
    let path = ctx.paths.pause_marker();
    if !path.exists() {
        return json!({"paused": false, "paused_at": null, "reason": ""});
    }
    let parsed = std::fs::read_to_string(&path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok());
    match parsed {
        Some(Value::Object(map)) => json!({
            "paused": true,
            "paused_at": map.get("paused_at").cloned().unwrap_or(Value::Null),
            "reason": map.get("reason").and_then(Value::as_str).unwrap_or(""),
        }),
        _ => json!({
            "paused": true,
            "paused_at": null,
            "reason": "unreadable pause marker",
        }),
    }
}

/// `paused_snapshots()`: every `schedule.json.paused-*` sibling, with its entry
/// count and mtime, so a silent rename is visible instead of reading as empty.
fn paused_snapshots(config_dir: &std::path::Path) -> Vec<Value> {
    let Ok(read_dir) = std::fs::read_dir(config_dir) else {
        return Vec::new();
    };
    let mut paths: Vec<std::path::PathBuf> = read_dir
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with(crate::store::SCHEDULE_PAUSED_PREFIX))
        })
        .collect();
    paths.sort();

    paths
        .into_iter()
        .map(|path| {
            let entries = std::fs::read_to_string(&path)
                .ok()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                .and_then(|value| value.as_array().map(|items| items.len()))
                .map(|count| json!(count))
                .unwrap_or(Value::Null);
            let modified_at = std::fs::metadata(&path)
                .and_then(|meta| meta.modified())
                .ok()
                .and_then(|time| Timestamp::try_from(time).ok())
                .map(|instant| {
                    instant
                        .to_zoned_datetime(TimeZone::system())
                        .strftime("%Y-%m-%dT%H:%M:%S%.f%:z")
                        .to_string()
                })
                .map(Value::String)
                .unwrap_or(Value::Null);
            json!({
                "file": path.display().to_string(),
                "entries": entries,
                "modified_at": modified_at,
            })
        })
        .collect()
}
