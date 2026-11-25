//! `xpst best-time` — recommended posting times from the analytics ledger.
//!
//! Parity target: `src/xpst/cli.py::best_time` over
//! `src/xpst/best_time.py::BestTimeAnalyzer`. The analysis itself lives in
//! `xpst_store::best_time`; this module resolves the profile, picks the ledger
//! and renders the payload.
//!
//! The ledger is `<config dir>/analytics.db` — `config.config_dir` after
//! `XPSTConfig.load`, which is the same directory `XPST_CONFIG_DIR` names. The
//! command loads the config first (so a fresh profile gets its default
//! `config.yaml`, exactly like Python) and then reads the database.
//!
//! Rendering is `json.dumps(..., indent=2, default=str)` with Python's default
//! `ensure_ascii=True`, so a non-ASCII platform name comes out escaped and the
//! two engines can be diffed with `cmp`.
//!
//! **Divergence, recorded in the ledger note:** a `views`/`likes` column that
//! holds text makes Python raise `TypeError` out of `analyze()` (the `try` only
//! wraps the query), while this reader skips the row. A ledger the shipped
//! writer produced is integer-typed, so only a hand-edited database can observe
//! the difference.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use xpst_config::Value;

use crate::commands::bootstrap;
use crate::output::{
    array, integer, null, number, object, python_pretty_json_cfg, string, write_text,
};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// One slot as Python's dict, in `analyze()`'s key order.
fn slot_value(slot: &xpst_store::best_time::Slot) -> Value {
    object(vec![
        ("platform", string(slot.platform.clone())),
        ("day_of_week", integer(i64::from(slot.day_of_week))),
        ("day_name", string(slot.day_name())),
        ("hour", integer(i64::from(slot.hour))),
        ("avg_engagement_rate", number(slot.avg_engagement_rate)),
        ("post_count", integer(slot.post_count as i64)),
    ])
}

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
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| Path::new(".").to_path_buf());
    let slots = xpst_store::best_time::analyze(&dir.join("analytics.db"), platform.as_deref());

    if json_mode {
        let payload = match &platform {
            Some(name) => object(vec![
                ("platform", string(name.clone())),
                ("best_time", slots.first().map_or_else(null, slot_value)),
            ]),
            None => {
                // `best_overall()`: top three per platform, platforms in the
                // order the ranked results first mention them.
                let mut order: Vec<String> = Vec::new();
                for slot in &slots {
                    if !order.contains(&slot.platform) {
                        order.push(slot.platform.clone());
                    }
                }
                let mut best_times = xpst_config::Mapping::new();
                for name in order {
                    let top: Vec<Value> = slots
                        .iter()
                        .filter(|slot| slot.platform == name)
                        .take(3)
                        .map(slot_value)
                        .collect();
                    best_times.insert(Value::from(name.as_str()), array(top));
                }
                object(vec![("best_times", Value::Mapping(best_times))])
            }
        };
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

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
