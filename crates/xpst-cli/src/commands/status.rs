//! `xpst status` — the local health/statistics view.
//!
//! Parity target: `src/xpst/cli.py::status` →
//! `StateManager.get_statistics()`. The `--json` payload is field-for-field the
//! Python one, including the F7 note that says the platform counters are
//! historical post history rather than a live check.
//!
//! Not reproduced, and named here so nobody reads the absence as an oversight:
//!
//! * the Rich table layout (the JSON payload is the contract; the text form is
//!   labelled `Rust engine` and carries the same numbers);
//! * the quota table, which needs `QuotaManager` — a different read path.
//!
//! Nothing is written: `status` reads the state document and (only when the
//! profile has never been used) the first-run config.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use serde_json::Value;

use crate::commands::bootstrap;
use crate::output::write_python_json_value;
use crate::{exit, flag, GlobalOptions};

/// Run `status`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let dir = config_path.parent().unwrap_or_else(|| Path::new("."));
    let state = xpst_state::status::effective_state(dir);
    let stats = xpst_state::status::statistics(&state);

    if json_mode {
        let _ = write_python_json_value(out, &stats);
    } else {
        let _ = write!(out, "{}", render_text(&stats));
    }
    exit::SUCCESS
}

/// Plain-text rendering of the same payload.
fn render_text(stats: &Value) -> String {
    let mut text = String::from("xPST Health Status\n\nStatistics\n");
    text.push_str(&format!(
        "  Total Videos Tracked: {}\n",
        scalar(&stats["total_videos_tracked"])
    ));
    text.push_str(&format!(
        "  Total Processed: {}\n",
        scalar(&stats["total_processed"])
    ));
    text.push_str(&format!(
        "  Last Check: {}\n",
        stats["last_check"].as_str().unwrap_or("Never")
    ));
    if let Some(platforms) = stats["by_platform"].as_object() {
        for (platform, count) in platforms {
            text.push_str(&format!("  {platform}: {}\n", scalar(count)));
        }
    }

    text.push_str(
        "\nPlatform Health (historical — local post history; run `xpst doctor` for live readiness):\n",
    );
    if let Some(health) = stats["platform_health"].as_object() {
        for (platform, entry) in health {
            let ok = entry["status"].as_str() == Some("ok");
            let mark = if ok { "✅" } else { "❌" };
            text.push_str(&format!(
                "  {mark} {platform}: {}\n",
                entry["status"].as_str().unwrap_or("unknown")
            ));
            if entry["failures"].as_u64().unwrap_or(0) > 0 {
                text.push_str(&format!("     Failures: {}\n", scalar(&entry["failures"])));
            }
            if let Some(last) = entry["last_success"].as_str() {
                text.push_str(&format!("     Last success: {last}\n"));
            }
        }
    }

    let dead_letters = stats["dead_letter_count"].as_u64().unwrap_or(0);
    if dead_letters > 0 {
        text.push_str(&format!(
            "\nDead Letter Queue ({dead_letters} items): run `xpst failures list` for detail\n"
        ));
    }
    text
}

fn scalar(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "null".to_string(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn text_rendering_names_the_historical_caveat() {
        let stats = json!({
            "total_videos_tracked": 2,
            "total_processed": 5,
            "last_check": null,
            "by_platform": {"youtube": 1},
            "platform_health": {
                "youtube": {"status": "ok", "failures": 0, "last_success": null},
                "x": {"status": "error", "failures": 3, "last_success": "2026-01-01T00:00:00"}
            },
            "dead_letter_count": 1
        });
        let text = render_text(&stats);
        assert!(text.contains("historical"));
        assert!(text.contains("Last Check: Never"));
        assert!(text.contains("❌ x: error"));
        assert!(text.contains("Failures: 3"));
        assert!(text.contains("Dead Letter Queue (1 items)"));
    }
}
