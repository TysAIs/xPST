//! `xpst best-time` — recommended posting times from the analytics ledger.
//!
//! Parity target: `src/xpst/cli.py::best_time` over
//! `src/xpst/best_time.py::BestTimeAnalyzer`. The analysis lives in
//! `xpst_store::best_time` and the payload shape in `xpst_surface::best_time`
//! (the MCP tool `xpst_best_time` serves the same document); this module
//! resolves the profile, picks the ledger and renders.
//!
//! The ledger is `<config dir>/analytics.db` — `config.config_dir` after
//! `XPSTConfig.load`, which is the same directory `XPST_CONFIG_DIR` names. The
//! command loads the config first (so a fresh profile gets its default
//! `config.yaml`, exactly like Python) and then reads the database.
//!
//! Rendering is `json.dumps(..., indent=2, default=str)` with Python's default
//! `ensure_ascii=True`, so a non-ASCII platform name comes out escaped and the
//! two engines can be diffed with `cmp`.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;

use crate::commands::bootstrap;
use crate::output::{python_pretty_json_cfg, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// Run `best-time`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let platform = leaf_string(leaf, "opt:platform");

    // `load_config()` first: a fresh profile materializes `config.yaml` before
    // the ledger is touched, and a broken config is an error, not an empty
    // answer.
    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| Path::new(".").to_path_buf());
    let db = dir.join("analytics.db");
    let payload = xpst_surface::best_time::payload(&db, platform.as_deref());

    if json_mode {
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let slots = xpst_store::best_time::analyze(&db, platform.as_deref());
    let mut text = String::new();
    match &platform {
        Some(name) => match slots.first() {
            Some(slot) => {
                text.push_str(&format!(
                    "\nBest time for {name}: {} at {}:00 (avg {}% engagement, {} posts)\n",
                    slot.day_name(),
                    slot.hour,
                    slot.avg_engagement_rate,
                    slot.post_count
                ));
                if slot.avg_engagement_rate <= 0.0 {
                    text.push_str(
                        "  ⚠ No engagement data behind this yet — treat it as a placeholder, not a \
                         real recommendation. Post videos with metrics tracking enabled to train \
                         it.\n",
                    );
                }
            }
            None => text.push_str(&format!("\nNo data yet for {name}.\n")),
        },
        None => {
            if slots.is_empty() {
                text.push_str(
                    "\nNo posting history yet. Post a few videos to get recommendations.\n",
                );
            } else {
                text.push_str("\nBest times to post:\n\n");
                let mut seen: Vec<String> = Vec::new();
                for slot in &slots {
                    if seen.contains(&slot.platform) {
                        continue;
                    }
                    seen.push(slot.platform.clone());
                    text.push_str(&format!(
                        "  {}: {} at {}:00 (avg {}% engagement)\n",
                        slot.platform,
                        slot.day_name(),
                        slot.hour,
                        slot.avg_engagement_rate
                    ));
                }
                text.push('\n');
            }
        }
    }
    let _ = write_text(out, text.trim_end());
    let _ = err;
    exit::SUCCESS
}
