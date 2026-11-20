//! `xpst_rate_limit_status` — active rate-limit cooldowns.
//!
//! Ports `rate_limit_status.cooldown_report()`: the same snapshot filter
//! (`strikes == 0 && remaining <= 0` is idle, so it is omitted), the same
//! per-record fields, the same human duration formatter, and the same fixed
//! sentence saying that a cooldown defers posts rather than failing them.
//!
//! Read-only by construction: one file read, no sleep, no platform probe.

use jiff::tz::TimeZone;
use jiff::Timestamp;
use serde_json::{json, Map, Value};

use crate::error::ToolError;

use super::stamp;
use super::ToolContext;

pub const COOLDOWN_SEMANTICS: &str = "A cooldown defers posts to the affected platform; \
     it does not fail them. xPST resumes automatically when the pause expires.";

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    report(&ctx.paths.cooldown_file(), Timestamp::now())
}

/// Build the report for one cooldown file at a given instant.
pub fn report(path: &std::path::Path, now: Timestamp) -> Result<Value, ToolError> {
    let path_text = path.display().to_string();
    let file_present = path.is_file();

    let mut records: Map<String, Value> = Map::new();
    if file_present {
        let text = std::fs::read_to_string(path).map_err(|err| ToolError::Malformed {
            kind: "cooldown file",
            path: path_text.clone(),
            detail: err.to_string(),
        })?;
        if !text.trim().is_empty() {
            // A corrupt cooldown file must not take the whole status surface
            // down; the Python store tolerates it and reports idle.
            if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&text) {
                records = map;
            }
        }
    }

    let mut cooldowns = Map::new();
    let mut active: Vec<String> = Vec::new();
    let mut platforms: Vec<&String> = records.keys().collect();
    platforms.sort();

    for platform in platforms {
        let record = records.get(platform).cloned().unwrap_or_else(|| json!({}));
        let strikes = record.get("strikes").and_then(Value::as_i64).unwrap_or(0);
        let remaining = remaining_seconds(&record, now);
        if strikes == 0 && remaining <= 0.0 {
            continue;
        }
        let mut entry = match record {
            Value::Object(map) => map,
            _ => Map::new(),
        };
        entry.insert("remaining_s".to_string(), json!(round1(remaining)));
        entry.insert(
            "remaining_human".to_string(),
            json!(format_duration(remaining)),
        );
        entry.insert(
            "until_display".to_string(),
            json!(display_time(entry.get("until_iso").and_then(Value::as_str))),
        );
        if remaining > 0.0 {
            active.push(platform.clone());
        }
        cooldowns.insert(platform.clone(), Value::Object(entry));
    }

    Ok(json!({
        "state": if active.is_empty() { "idle" } else { "cooling" },
        "active": active,
        "cooldowns": Value::Object(cooldowns),
        "source": path_text,
        "file_present": file_present,
        "note": COOLDOWN_SEMANTICS,
    }))
}

fn remaining_seconds(record: &Value, now: Timestamp) -> f64 {
    let Some(until) = record.get("until_iso").and_then(Value::as_str) else {
        return 0.0;
    };
    match parse_instant(until) {
        Some(until) => {
            let millis = i64::try_from(until.duration_since(now).as_millis()).unwrap_or(0);
            let delta = millis as f64 / 1000.0;
            if delta.is_finite() {
                delta.max(0.0)
            } else {
                0.0
            }
        }
        None => 0.0,
    }
}

/// Parse the ISO instants the cooldown store writes (`datetime.isoformat()`),
/// including the space-separated variant `fromisoformat` accepts.
///
/// A stamp with no offset is read as UTC, which is what the store's own writer
/// guarantees: it always emits an offset, so a naive value can only come from
/// an older file.
pub fn parse_instant(raw: &str) -> Option<Timestamp> {
    if raw.is_empty() {
        return None;
    }
    let normalized = raw.replacen(' ', "T", 1);
    if let Some(instant) = stamp::parse_instant(&normalized) {
        return Some(instant);
    }
    let naive = stamp::parse_naive(&normalized)?;
    // UTC has no fold and no gap, so this cannot be ambiguous.
    let zoned = naive.to_zoned_datetime(TimeZone::UTC).ok()?;
    Some(zoned.timestamp())
}

fn round1(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

/// `format_duration()` from `xpst/rate_limit_status.py`, branch for branch.
pub fn format_duration(seconds: f64) -> String {
    let total = seconds.max(0.0).round() as i64;
    if total >= 86_400 {
        let days = total / 86_400;
        let rest = total % 86_400;
        return format!("{days}d {}h", rest / 3600);
    }
    if total >= 3_600 {
        let hours = total / 3_600;
        let rest = total % 3_600;
        return format!("{hours}h {}m", rest / 60);
    }
    if total >= 60 {
        let minutes = total / 60;
        let rest = total % 60;
        return format!("{minutes}m {rest}s");
    }
    format!("{total}s")
}

/// Local `YYYY-MM-DD HH:MM:SS` of a stored instant, or the raw string when it
/// cannot be parsed (the Python helper's best-effort behaviour).
fn display_time(value: Option<&str>) -> Option<String> {
    let value = value?;
    if value.is_empty() {
        return None;
    }
    let normalized = value.replacen(' ', "T", 1);
    match stamp::render_in_original_offset(&normalized) {
        Some(rendered) => Some(rendered),
        None => Some(value.to_string()),
    }
}
