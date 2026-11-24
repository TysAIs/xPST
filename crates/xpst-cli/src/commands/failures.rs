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
use crate::output::{object, string, write_python_json, write_python_json_value, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

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
        let _ = write_python_json_value(out, &json!({ "failures": queue }));
    } else if queue.is_empty() {
        let _ = write_text(out, "No failed uploads.");
    } else {
        let _ = write_text(out, &render_text(&queue));
    }
    exit::SUCCESS
}

/// `xpst failures clear [VIDEO_ID]` — empty the dead-letter queue.
///
/// Parity target: `src/xpst/cli.py::failures_clear` →
/// `StateManager.clear_dead_letter_queue`. Without a video id every entry is
/// cleared and the `errors` key is dropped once it is empty; with one, only that
/// video's errors are cleared and the key stays as `{}` (Python assigns an empty
/// dict rather than deleting it). Successful-post history is never touched.
///
/// The document is loaded through [`xpst_state::status::effective_state`] — the
/// same tolerant loader `status` and `failures list` use, including the in-memory
/// v1 migration — and written back with Python's `json.dump` separators and the
/// key order it was read in (`serde_json` is built with `preserve_order`), so the
/// written file is byte-identical to the Python engine's.
pub fn clear(
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
    let video_id = leaf_string(leaf, "arg:video_id").filter(|value| !value.is_empty());

    let mut document = xpst_state::status::effective_state(dir);
    let cleared = clear_dead_letters(&mut document, video_id.as_deref());

    // Python writes only when something changed for the per-video form; the
    // whole-queue form always goes through `_store.update`, which writes.
    let should_write = video_id.is_none() || cleared > 0;
    if should_write {
        if let Err(error) = save_state(dir, &document) {
            let _ = writeln!(err, "xpst failures clear: cannot write state: {error}");
            return exit::GENERAL;
        }
    }

    let scope = video_id.clone().unwrap_or_else(|| "all".to_string());
    if json_mode {
        let payload = object(vec![
            ("ok", crate::output::boolean(true)),
            ("cleared", crate::output::integer(cleared as i64)),
            ("scope", string(scope)),
        ]);
        let _ = write_python_json(out, &payload);
    } else if cleared == 0 {
        let _ = write_text(out, "Nothing to clear.");
    } else {
        let where_ = video_id
            .as_deref()
            .map(|id| format!(" for {id}"))
            .unwrap_or_default();
        let _ = write_text(
            out,
            &format!("Cleared {cleared} failure record(s){where_}."),
        );
    }
    exit::SUCCESS
}

/// `StateManager.clear_dead_letter_queue` over the ordered document.
fn clear_dead_letters(document: &mut Value, video_id: Option<&str>) -> usize {
    let Some(videos) = document
        .get_mut("posted_videos")
        .and_then(Value::as_object_mut)
    else {
        return 0;
    };
    if let Some(id) = video_id {
        let Some(video) = videos.get_mut(id).and_then(Value::as_object_mut) else {
            return 0;
        };
        let Some(errors) = video.get("errors").and_then(Value::as_object) else {
            return 0;
        };
        if errors.is_empty() {
            return 0;
        }
        let cleared = errors.len();
        // Python assigns `{}`; the key stays in the document.
        video.insert("errors".to_string(), json!({}));
        return cleared;
    }
    let mut cleared = 0;
    for video in videos.values_mut() {
        let Some(record) = video.as_object_mut() else {
            continue;
        };
        let Some(errors) = record.get("errors").and_then(Value::as_object) else {
            continue;
        };
        if errors.is_empty() {
            continue;
        }
        cleared += errors.len();
        record.remove("errors");
    }
    cleared
}

/// `StateStore._atomic_write`: back up the current file, keep a forensic copy,
/// rotate to five backups, then replace the document with Python's separators.
fn save_state(dir: &Path, document: &Value) -> Result<(), String> {
    let path = dir.join("state.json");
    if path.exists() {
        let backups = dir.join("backups");
        std::fs::create_dir_all(&backups).map_err(|error| error.to_string())?;
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or_default();
        let _ = std::fs::copy(&path, backups.join(format!("state_{stamp}.json")));
        let mut existing: Vec<std::path::PathBuf> = std::fs::read_dir(&backups)
            .map(|entries| {
                entries
                    .flatten()
                    .map(|entry| entry.path())
                    .filter(|candidate| {
                        let name = candidate
                            .file_name()
                            .and_then(|name| name.to_str())
                            .unwrap_or("");
                        name.starts_with("state_") && name.ends_with(".json")
                    })
                    .collect()
            })
            .unwrap_or_default();
        existing.sort();
        // `MAX_BACKUPS = 5`, newest by name (the names are nanosecond stamps).
        let excess = existing.len().saturating_sub(5);
        for old in existing.iter().take(excess) {
            let _ = std::fs::remove_file(old);
        }
        let _ = std::fs::copy(&path, dir.join("state.json.forensic"));
    }
    let text = xpst_config::python_style_json(&crate::output::from_json(document))
        .map_err(|error| format!("cannot render state: {error}"))?;
    crate::fsutil::write_private(&path, text.as_bytes())
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
