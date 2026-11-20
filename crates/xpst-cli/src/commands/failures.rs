//! `xpst failures list` — the dead-letter queue.
//!
//! Parity target: `src/xpst/cli.py::failures_list`, which prints
//! `{"failures": [...]}` from `StateManager.get_dead_letter_queue()`. Each row
//! says whether the failure is still `open` or was later `resolved` by a
//! successful post, how long ago it happened, and what the platform said — the
//! fields a human or an agent needs to triage it without reading logs.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use serde_json::{json, Value};

use crate::commands::bootstrap;
use crate::output::{write_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// Run `failures list`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let dir = config_path.parent().unwrap_or_else(|| Path::new("."));
    let state = xpst_state::status::effective_state(dir);
    let queue = xpst_state::status::dead_letters(&state, jiff::Timestamp::now());

    if json_mode {
        let _ = write_json(out, &json!({ "failures": queue }));
    } else if queue.is_empty() {
        let _ = write_text(out, "No failed uploads.");
    } else {
        let _ = write_text(out, &render_text(&queue));
    }
    exit::SUCCESS
}

fn render_text(queue: &[Value]) -> String {
    let mut text = String::from("Failed Uploads (dead-letter queue)\n");
    for entry in queue {
        let status = if entry["resolved"].as_bool().unwrap_or(false) {
            "resolved"
        } else {
            "OPEN"
        };
        let age = match entry["age_hours"].as_f64() {
            Some(hours) => format!("{hours:.0}h"),
            None => "?".to_string(),
        };
        text.push_str(&format!(
            "  {} | {} | {status} | {age} | {} | count {}\n",
            entry["video_id"].as_str().unwrap_or(""),
            entry["platform"].as_str().unwrap_or(""),
            entry["error"].as_str().unwrap_or(""),
            entry["count"],
        ));
    }
    text.push_str("Retry one with: xpst failures retry <video_id> --platform <name>\n");
    text
}
