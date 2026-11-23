//! `xpst schedule list` — the queue, its pause state and any paused snapshots.
//!
//! Parity target: `src/xpst/cli.py::schedule_list`. The store is resolved the
//! way the Python command resolves it — through the profile directory
//! (`XPST_CONFIG_DIR`, else `~/.xpst`) — and deliberately *not* through the root
//! `--config` flag, which the Python command ignores.
//!
//! Posting is paused on this machine, so the common shape of this command is an
//! empty queue. The payload rules exist so that "empty" cannot be confused with
//! "paused and emptied": once a pause marker or a snapshot exists, the payload
//! becomes an object carrying both.

use std::io::Write;

use clap::ArgMatches;
use serde_json::Value;

use crate::output::{write_json, write_text};
use crate::{exit, flag, paths, string_value, GlobalOptions};

/// Run `schedule list`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let Some(dir) = paths::config_dir() else {
        let message =
            "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set";
        if json_mode {
            let payload =
                crate::output::error_payload_with("CONFIG_ERROR", message, serde_json::json!({}));
            let _ = write_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    };

    let listing = xpst_state::schedule::listing(&dir);
    if let Some(problem) = &listing.problem {
        // Python logs this on stderr and still answers with an empty list.
        let _ = writeln!(err, "ERROR {problem}");
    }

    let payload = xpst_state::schedule::payload(&listing);
    if json_mode {
        let _ = write_json(out, &payload);
    } else {
        let _ = write_text(out, &render_text(&payload, &listing));
    }
    exit::SUCCESS
}

fn render_text(payload: &Value, listing: &xpst_state::schedule::ScheduleListing) -> String {
    let mut text = String::new();
    if listing.pause["paused"].as_bool().unwrap_or(false) {
        let reason = listing.pause["reason"].as_str().unwrap_or("");
        let suffix = if reason.is_empty() {
            String::new()
        } else {
            format!(" — {reason}")
        };
        text.push_str(&format!(
            "Queue paused{suffix}. Entries will not fire until `xpst schedule resume`.\n"
        ));
    }

    let entries = payload.get("entries").and_then(Value::as_array);
    let empty = entries.is_none_or(Vec::is_empty);
    if empty {
        text.push_str("No scheduled posts.\n");
        for snapshot in &listing.snapshots {
            let count = match &snapshot["entries"] {
                Value::Null => String::new(),
                other => format!(" ({other} entries)"),
            };
            text.push_str(&format!(
                "Note: pause snapshot found at {}{count} — the queue may have been paused, not emptied.\n",
                snapshot["file"].as_str().unwrap_or("")
            ));
        }
        return text;
    }

    text.push_str("Scheduled Posts\n");
    for entry in entries.into_iter().flatten() {
        let file = std::path::Path::new(entry["video_path"].as_str().unwrap_or(""))
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| "(text post)".to_string());
        let platforms = entry["platforms"]
            .as_array()
            .map(|list| {
                list.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .filter(|joined| !joined.is_empty())
            .unwrap_or_else(|| "all".to_string());
        let scheduled = entry["scheduled_time_local"]
            .as_str()
            .or_else(|| entry["scheduled_time"].as_str())
            .unwrap_or("")
            .replace('T', " ");
        let scheduled = scheduled.split('.').next().unwrap_or("");
        let scheduled: String = scheduled.chars().take(16).collect();
        text.push_str(&format!(
            "  {} | {} | {} | {} | {}\n",
            entry["id"].as_str().unwrap_or("?"),
            file,
            scheduled,
            platforms,
            entry["status"].as_str().unwrap_or(""),
        ));
    }
    text
}

// ---------------------------------------------------------------------------
// `schedule pause` / `schedule resume`
// ---------------------------------------------------------------------------
//
// Parity target: `src/xpst/schedule_manager.py::ScheduleManager.pause/resume`
// and the two Click commands that wrap them. Both are local writes: the marker
// is a sibling file (`schedule.paused.json`), never a field inside the store,
// because every reader iterates the store as a bare list.
//
// The payload is built from `xpst_config::Value` so the key order and the
// separators match `json.dumps` byte for byte — `paused` first, then
// `already_paused`, then the marker's own fields, exactly as Python's
// `{"paused": True, "already_paused": ..., **state}` merges them.

/// Resolve the profile directory both pause commands write into.
fn profile_dir(
    json_mode: bool,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> Result<std::path::PathBuf, i32> {
    match paths::config_dir() {
        Some(dir) => Ok(dir),
        None => {
            let message =
                "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set";
            if json_mode {
                let payload = crate::output::error_payload_with(
                    "CONFIG_ERROR",
                    message,
                    serde_json::json!({}),
                );
                let _ = write_json(out, &payload);
            } else {
                let _ = writeln!(err, "{message}");
            }
            Err(exit::CONFIG_ERROR)
        }
    }
}

/// Report a failed marker write the way the Python command would surface an
/// `OSError`: a message on stderr and a general-error exit, never a success.
fn report_write_failure(err: &mut dyn Write, action: &str, problem: &str) -> i32 {
    let _ = writeln!(err, "xpst schedule {action}: {problem}");
    exit::GENERAL
}

/// `xpst schedule pause` — create the pause marker.
pub fn pause(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let reason = leaf
        .and_then(|m| string_value(m, "opt:reason"))
        .unwrap_or_default();

    let dir = match profile_dir(json_mode, out, err) {
        Ok(dir) => dir,
        Err(code) => return code,
    };

    let created = match xpst_state::schedule::pause(&dir, &reason, jiff::Timestamp::now()) {
        Ok(created) => created,
        Err(error) => return report_write_failure(err, "pause", &error.to_string()),
    };
    let state = xpst_state::schedule::pause_state(&dir);

    if json_mode {
        let payload = crate::output::object(vec![
            ("paused", crate::output::boolean(true)),
            ("already_paused", crate::output::boolean(!created)),
            ("paused_at", json_to_config_value(&state["paused_at"])),
            (
                "reason",
                crate::output::string(state["reason"].as_str().unwrap_or("")),
            ),
        ]);
        let _ = crate::output::write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    if created {
        let _ = write_text(
            out,
            "Queue paused. No scheduled post will fire until resume.",
        );
    } else {
        let _ = write_text(out, "Queue was already paused.");
    }
    exit::SUCCESS
}

/// `xpst schedule resume` — remove the pause marker.
pub fn resume(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

    let dir = match profile_dir(json_mode, out, err) {
        Ok(dir) => dir,
        Err(code) => return code,
    };

    let was_paused = match xpst_state::schedule::resume(&dir) {
        Ok(was_paused) => was_paused,
        Err(error) => return report_write_failure(err, "resume", &error.to_string()),
    };

    if json_mode {
        let payload = crate::output::object(vec![
            ("paused", crate::output::boolean(false)),
            ("was_paused", crate::output::boolean(was_paused)),
        ]);
        let _ = crate::output::write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    if was_paused {
        let _ = write_text(out, "Queue resumed. Due entries can fire again.");
    } else {
        let _ = write_text(out, "Queue was not paused.");
    }
    exit::SUCCESS
}

/// Carry a `serde_json` scalar into the ordered value type without losing its
/// type (a `null` `paused_at` must stay `null`, not become `""`).
fn json_to_config_value(value: &Value) -> xpst_config::Value {
    match value {
        Value::Null => crate::output::null(),
        Value::Bool(flag) => crate::output::boolean(*flag),
        Value::Number(number) => match number.as_i64() {
            Some(integer) => crate::output::integer(integer),
            None => xpst_config::Value::Number(
                serde_json::Number::from_f64(number.as_f64().unwrap_or_default())
                    .unwrap_or_else(|| serde_json::Number::from(0)),
            ),
        },
        Value::String(text) => crate::output::string(text.clone()),
        other => crate::output::string(other.to_string()),
    }
}
