//! `xpst_status` — cross-posting statistics from `state.json`.
//!
//! Ports `StateManager.get_statistics()` field-for-field, including the
//! `historical` / `platform_health_note` honesty markers: those counters are
//! local post history, never a live check, and the Python contract says so in
//! the payload. Reading them as current is how "platform is broken" gets
//! reported from a retired API path's old failures.

use serde_json::{json, Map, Value};

use crate::error::ToolError;

use super::ToolContext;

/// Platforms that carry their own counter in `by_platform`.
const COUNTED_PLATFORMS: &[&str] = &["youtube", "x", "instagram", "tiktok", "threads"];

const PLATFORM_HEALTH_NOTE: &str = "platform_health is historical: it counts local post \
     outcomes, not a live check. Run `xpst doctor` (or `xpst health`) for current \
     auth/readiness.";

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let path = ctx.paths.state_file();
    let path_text = path.display().to_string();

    if !path.exists() {
        // A fresh install has no ledger. The statistics *of an empty state* are
        // well defined, and the Python tool answers the same way (its manager
        // holds an empty default state), so this is not an invented payload.
        return Ok(empty_statistics());
    }

    let text = std::fs::read_to_string(&path).map_err(|err| ToolError::Malformed {
        kind: "state file",
        path: path_text.clone(),
        detail: err.to_string(),
    })?;
    let root: Value = serde_json::from_str(&text).map_err(|err| ToolError::Malformed {
        kind: "state file",
        path: path_text.clone(),
        detail: err.to_string(),
    })?;
    let object = root.as_object().ok_or_else(|| ToolError::Malformed {
        kind: "state file",
        path: path_text.clone(),
        detail: "expected a JSON object".to_string(),
    })?;

    Ok(statistics(object))
}

fn statistics(state: &Map<String, Value>) -> Value {
    let videos = state
        .get("posted_videos")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();

    let mut by_platform = Map::new();
    for platform in COUNTED_PLATFORMS {
        by_platform.insert((*platform).to_string(), json!(0));
    }

    let mut cross_posted_count: u64 = 0;
    let mut dead_letter_count: u64 = 0;

    for video in videos.values() {
        if let Some(posted_to) = video.get("posted_to").and_then(Value::as_object) {
            for platform in posted_to.keys() {
                if COUNTED_PLATFORMS.contains(&platform.as_str()) {
                    let current = by_platform
                        .get(platform)
                        .and_then(Value::as_u64)
                        .unwrap_or(0);
                    by_platform.insert(platform.clone(), json!(current + 1));
                }
                cross_posted_count += 1;
            }
        }
        // Dead letters are recorded errors, per destination, resolved or not.
        if let Some(errors) = video.get("errors").and_then(Value::as_object) {
            dead_letter_count += errors.len() as u64;
        }
    }

    let health = state.get("health").cloned().unwrap_or_else(|| json!({}));
    let mut platform_health = Map::new();
    if let Some(platforms) = health.get("platforms").and_then(Value::as_object) {
        for (platform, entry) in platforms {
            platform_health.insert(
                platform.clone(),
                json!({
                    "status": entry.get("status").cloned().unwrap_or_else(|| json!("unknown")),
                    "last_success": entry.get("last_success").cloned().unwrap_or(Value::Null),
                    "failures": entry.get("failures").cloned().unwrap_or_else(|| json!(0)),
                    "historical": true,
                    "source": "state_history",
                }),
            );
        }
    }

    json!({
        "version": state.get("version").cloned().unwrap_or_else(|| json!(1)),
        "total_videos_tracked": videos.len(),
        "total_processed": health.get("total_processed").cloned().unwrap_or_else(|| json!(0)),
        "cross_posted_count": cross_posted_count,
        "by_platform": Value::Object(by_platform),
        "last_check": health.get("last_check").cloned().unwrap_or(Value::Null),
        "last_wake_check": health.get("last_wake_check").cloned().unwrap_or(Value::Null),
        "dead_letter_count": dead_letter_count,
        "platform_health": Value::Object(platform_health),
        "platform_health_note": PLATFORM_HEALTH_NOTE,
    })
}

fn empty_statistics() -> Value {
    statistics(&Map::new())
}
