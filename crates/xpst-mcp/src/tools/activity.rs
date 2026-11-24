//! `xpst_activity` — recorded failures with targeted recovery actions.
//!
//! Ports `_handle_activity()`: every `posted_videos.<id>.errors.<platform>`
//! entry in the state ledger, flattened into one row per (video, platform) with
//! the recovery action an agent should take. `retryable: true` means the
//! uploader classified the failure as transient, so the row says `retry`;
//! anything else (including "not classified") says `review`, because guessing
//! "retry" for an unclassified failure is how a rate limit becomes a ban.
//!
//! The document is **compact** (`json.dumps` with no `indent`), which is the
//! tool's own renderer — `xpst failures list` is a different document.
//!
//! The state reader is `xpst_state::status::effective_state`, the same tolerant
//! one `xpst status` uses: a corrupt document falls back to the newest
//! `backups/state_*.json`, then to the empty document, and a v1 document is
//! migrated in memory. Reading it any other way would report "no failures" for
//! a ledger Python can still read.

use serde_json::{json, Map, Value};

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let state = xpst_state::status::effective_state(ctx.paths.config_dir());

    let mut failures: Vec<Value> = Vec::new();
    if let Some(videos) = state.get("posted_videos").and_then(Value::as_object) {
        for (video_id, video) in videos {
            let Some(video) = video.as_object() else {
                continue;
            };
            let Some(errors) = video.get("errors").and_then(Value::as_object) else {
                continue;
            };
            for (platform, result) in errors {
                let Some(result) = result.as_object() else {
                    continue;
                };
                let retryable = result.get("retryable").cloned().unwrap_or(Value::Null);
                failures.push(json!({
                    "video_id": video_id,
                    "platform": platform,
                    "error": python_str(result.get("error").unwrap_or(&Value::Null)),
                    "retryable": retryable,
                    "post_id": Value::Null,
                    "post_url": Value::Null,
                    "source_url": video.get("source_url").cloned().unwrap_or(Value::Null),
                    "last_attempt": last_attempt(result, video),
                    "action": if retryable == Value::Bool(true) { "retry" } else { "review" },
                }));
            }
        }
    }

    // `failures.sort(key=lambda item: (item.get("last_attempt") or "", item["video_id"],
    // item["platform"]), reverse=True)` — newest first, then id, then platform.
    failures.sort_by(|left, right| {
        let key = |row: &Value| {
            (
                sort_text(row.get("last_attempt")),
                text(row.get("video_id")),
                text(row.get("platform")),
            )
        };
        key(right).cmp(&key(left))
    });

    Ok(json!({
        "failures": failures,
        "count": failures.len(),
    }))
}

/// `result.get("timestamp") or video.get("last_attempt")` — Python's `or`, so a
/// falsy timestamp falls through to the video's own field.
fn last_attempt(result: &Map<String, Value>, video: &Map<String, Value>) -> Value {
    let timestamp = result.get("timestamp").cloned().unwrap_or(Value::Null);
    if truthy(&timestamp) {
        return timestamp;
    }
    video.get("last_attempt").cloned().unwrap_or(Value::Null)
}

/// Python truthiness for a JSON value.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().unwrap_or_default() != 0.0,
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// `item.get("last_attempt") or ""` as a sort key.
///
/// Python compares the raw value, so a non-string `last_attempt` raises
/// `TypeError` there. This renders it as its JSON text instead — a documented
/// divergence for a shape the shipped engine cannot sort at all.
fn sort_text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(value) if truthy(value) => value.to_string(),
        _ => String::new(),
    }
}

fn text(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

/// `str(result.get("error") or "Unknown error")`.
fn python_str(value: &Value) -> String {
    if !truthy(value) {
        return "Unknown error".to_string();
    }
    match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => number.to_string(),
        other => other.to_string(),
    }
}
