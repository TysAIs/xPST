//! The read-only cooldown view (`xpst.rate_limit_status.cooldown_report`).
//!
//! The durable cooldown state machine persists an `until` instant per platform
//! in `rate_cooldowns.json` so a restart cannot forget that a platform is still
//! cooling. This module is the ONE renderer of that file: `xpst doctor`,
//! `xpst health` and the MCP tool `xpst_rate_limit_status` all call
//! [`report`], so every surface answers with the same data instead of each
//! re-deriving the format.
//!
//! Read-only by construction: one file read, no sleep, no platform probe, no
//! write. A missing or unreadable file is reported as idle/unknown rather than
//! raised — an operator asking for health must never get a traceback because a
//! cooldown file is corrupt.
//!
//! Note on the two ledgers: this reads the **Python-format** file
//! (`rate_cooldowns.json`, per-platform ISO instants). [`crate::store`] owns the
//! newer per-account ledger (`rate_limits.json`, epoch milliseconds). They do
//! not share a schema yet, and this module deliberately does not pretend they
//! do.

use jiff::tz::{Offset, TimeZone};
use jiff::Timestamp;
use serde_json::{Map, Value};

/// The file name the cooldown store persists under the config dir.
pub const COOLDOWN_FILENAME: &str = "rate_cooldowns.json";

/// One sentence every surface can repeat verbatim: a cooldown delays a post, it
/// never loses one.
pub const COOLDOWN_SEMANTICS: &str = "A cooldown defers posts to the affected platform; it \
     does not fail them. xPST resumes automatically when the pause expires.";

/// `format_duration()`: compact human duration (`3m 20s`).
#[must_use]
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

/// `cooldown_report()` for one cooldown file at a given instant.
///
/// `path` is `None` when no config directory could be resolved, which the
/// payload reports as `state: "unknown"`.
#[must_use]
pub fn report(path: Option<&std::path::Path>, now: Timestamp) -> Value {
    let Some(path) = path else {
        return serde_json::json!({
            "state": "unknown",
            "active": [],
            "cooldowns": {},
            "source": null,
            "file_present": false,
            "note": COOLDOWN_SEMANTICS,
        });
    };
    let path_text = path.display().to_string();
    let file_present = path.is_file();

    let mut records: Map<String, Value> = Map::new();
    if file_present {
        if let Ok(text) = std::fs::read_to_string(path) {
            if !text.trim().is_empty() {
                // A corrupt cooldown file must not take the whole status
                // surface down; the Python store tolerates it and reports idle.
                if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&text) {
                    records = map;
                }
            }
        }
    }

    let mut cooldowns = Map::new();
    let mut active: Vec<String> = Vec::new();
    let mut platforms: Vec<&String> = records.keys().collect();
    platforms.sort();

    for platform in platforms {
        let record = records
            .get(platform)
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new()));
        let strikes = record.get("strikes").and_then(Value::as_i64).unwrap_or(0);
        let remaining = remaining_seconds(&record, now);
        if strikes == 0 && remaining <= 0.0 {
            continue;
        }
        let mut entry = match record {
            Value::Object(map) => map,
            _ => Map::new(),
        };
        entry.insert(
            "remaining_s".to_string(),
            serde_json::json!(round1(remaining)),
        );
        entry.insert(
            "remaining_human".to_string(),
            Value::String(format_duration(remaining)),
        );
        entry.insert(
            "until_display".to_string(),
            display_time(entry.get("until_iso").and_then(Value::as_str))
                .map_or(Value::Null, Value::String),
        );
        if remaining > 0.0 {
            active.push(platform.clone());
        }
        cooldowns.insert(platform.clone(), Value::Object(entry));
    }

    serde_json::json!({
        "state": if active.is_empty() { "idle" } else { "cooling" },
        "active": active,
        "cooldowns": Value::Object(cooldowns),
        "source": path_text,
        "file_present": file_present,
        "note": COOLDOWN_SEMANTICS,
    })
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
#[must_use]
pub fn parse_instant(raw: &str) -> Option<Timestamp> {
    if raw.is_empty() {
        return None;
    }
    let normalized = raw.replacen(' ', "T", 1);
    // The shape check is explicit rather than trusting the time library's
    // parser, which is deliberately more lenient than this store is: it accepts
    // a bare date and an `HH:MM`, and silently discards a trailing offset. For
    // a display field that difference is a confidently wrong timestamp instead
    // of the `null` that says "cannot tell".
    if !fixed_part(&normalized) {
        return None;
    }
    let rest = &normalized[FIXED_PART.len()..];
    let tail = match rest.strip_prefix('.') {
        Some(after_dot) => {
            let digits = after_dot.bytes().take_while(u8::is_ascii_digit).count();
            if digits == 0 || digits > 9 {
                return None;
            }
            &after_dot[digits..]
        }
        None => rest,
    };
    if tail.is_empty() {
        // No offset: a naive wall clock, not an instant.
        let naive = normalized.parse::<jiff::civil::DateTime>().ok()?;
        // UTC has no fold and no gap, so this cannot be ambiguous.
        let zoned = naive.to_zoned_datetime(TimeZone::UTC).ok()?;
        return Some(zoned.timestamp());
    }
    normalized.parse::<Timestamp>().ok()
}

/// `YYYY-MM-DDTHH:MM:SS` — the fixed-width part every stored stamp shares.
const FIXED_PART: &[u8] = b"dddd-dd-ddTdd:dd:dd";

/// True when the first nineteen bytes are the stamp's fixed part.
fn fixed_part(text: &str) -> bool {
    text.len() >= FIXED_PART.len()
        && text
            .as_bytes()
            .iter()
            .zip(FIXED_PART)
            .all(|(byte, expected)| {
                if *expected == b'd' {
                    byte.is_ascii_digit()
                } else {
                    byte == expected
                }
            })
}

fn round1(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

/// Local `YYYY-MM-DD HH:MM:SS` of a stored instant, or the raw string when it
/// cannot be parsed (the Python helper's best-effort behaviour).
fn display_time(value: Option<&str>) -> Option<String> {
    let value = value?;
    if value.is_empty() {
        return None;
    }
    let normalized = value.replacen(' ', "T", 1);
    let instant = normalized.parse::<Timestamp>().ok();
    let offset = jiff::fmt::temporal::DateTimeParser::new()
        .parse_pieces(&normalized)
        .ok()
        .and_then(|pieces| pieces.to_numeric_offset())
        .unwrap_or(Offset::UTC);
    match instant {
        Some(instant) => {
            let zoned = instant.to_zoned_datetime(TimeZone::fixed(offset));
            Some(zoned.strftime("%Y-%m-%d %H:%M:%S").to_string())
        }
        None => Some(value.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir(name: &str) -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("xpst-cooldowns-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_missing_file_is_idle_not_an_error() {
        let dir = tempdir("missing");
        let payload = report(Some(&dir.join(COOLDOWN_FILENAME)), Timestamp::now());
        assert_eq!(payload["state"], "idle");
        assert_eq!(payload["file_present"], false);
        assert_eq!(payload["active"], serde_json::json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unresolved_config_dir_is_unknown() {
        let payload = report(None, Timestamp::now());
        assert_eq!(payload["state"], "unknown");
        assert_eq!(payload["source"], Value::Null);
    }

    #[test]
    fn an_active_cooldown_is_reported_with_a_human_duration() {
        let dir = tempdir("active");
        let now = "2026-01-01T00:00:00+00:00".parse::<Timestamp>().unwrap();
        std::fs::write(
            dir.join(COOLDOWN_FILENAME),
            r#"{"x": {"strikes": 2, "until_iso": "2026-01-01T00:03:20+00:00", "last_hit_iso": "2026-01-01T00:00:00+00:00", "last_reason": "429", "clean_publishes_since_hit": 0}}"#,
        )
        .unwrap();
        let payload = report(Some(&dir.join(COOLDOWN_FILENAME)), now);
        assert_eq!(payload["state"], "cooling");
        assert_eq!(payload["active"], serde_json::json!(["x"]));
        assert_eq!(payload["cooldowns"]["x"]["remaining_human"], "3m 20s");
        assert_eq!(payload["cooldowns"]["x"]["remaining_s"], 200.0);
        assert_eq!(
            payload["cooldowns"]["x"]["until_display"],
            "2026-01-01 00:03:20"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_corrupt_file_reports_idle() {
        let dir = tempdir("corrupt");
        std::fs::write(dir.join(COOLDOWN_FILENAME), "{not json").unwrap();
        let payload = report(Some(&dir.join(COOLDOWN_FILENAME)), Timestamp::now());
        assert_eq!(payload["state"], "idle");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn duration_formatting_matches_the_python_helper() {
        assert_eq!(format_duration(45.0), "45s");
        assert_eq!(format_duration(200.0), "3m 20s");
        assert_eq!(format_duration(3_900.0), "1h 5m");
        assert_eq!(format_duration(90_000.0), "1d 1h");
    }
}
