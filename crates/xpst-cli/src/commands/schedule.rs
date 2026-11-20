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
use crate::{exit, flag, paths, GlobalOptions};

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
