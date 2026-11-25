//! `xpst followers` — follower counts and growth from the local ledger.
//!
//! Parity target: `followers()` in `src/xpst/cli.py` (lines 7610-7660).
//!
//! The payload is built from `AnalyticsStore.latest_followers()` and
//! `follower_history()`; the live fetch is best-effort and every failure is
//! swallowed (`cli.py:7632-7644`, and each uploader's `get_followers` returns
//! `0` on error — `platforms/youtube.py:680-693`). So a run with no network
//! still reports counts and growth from the ledger, which is what this module
//! ports.
//!
//! **Divergences, recorded in the ledger note:**
//!
//! * The live fetch is not ported: `live_counts` is always empty, so `count` is
//!   the stored snapshot and `growth` is measured against the stored history.
//!   On a credential-less profile that is exactly the shipped answer; on a
//!   connected profile Python upgrades the number and records a new snapshot.
//! * Python constructs `CrossPostEngine(config)` for the fetch, which rewrites
//!   `quotas.json` on a fresh profile and runs crash recovery. This engine
//!   reads the ledger directly, so the profile is untouched.
//!
//! Rendering is `json.dumps(..., indent=2, default=str)` — pretty, insertion
//! order, `ensure_ascii=True` — the same renderer `best-time` uses.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use xpst_config::Value as YamlValue;

use crate::commands::bootstrap;
use crate::output::{array, integer, object, python_pretty_json_cfg, string, write_text};
use crate::{exit, flag, GlobalOptions};

/// `follower_history(p, limit=7)`.
const HISTORY_LIMIT: u32 = 7;

/// Run `xpst followers`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

    // `load_config()` first: a fresh profile materializes `config.yaml` before
    // the ledger is touched, exactly like `best-time`.
    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| Path::new(".").to_path_buf());
    let db = dir.join("analytics.db");

    // `AnalyticsStore()` creates the file and applies the schema on
    // construction, so `Store::open` (which does the same) is the right reader
    // here — unlike `best-time`, whose analyzer opens the ledger bare.
    let store = match xpst_store::Store::open(&db) {
        Ok(store) => store,
        Err(error) => {
            let message = format!("could not open {}: {error}", db.display());
            if json_mode {
                let payload = crate::output::error_payload("ANALYTICS_STORE_ERROR", &message);
                let _ = crate::output::write_python_json_value(out, &payload);
            } else {
                let _ = writeln!(err, "{message}");
            }
            return exit::GENERAL;
        }
    };

    let latest = store.latest_followers().unwrap_or_default();
    let mut platforms: Vec<String> = latest.keys().cloned().collect();
    platforms.sort();

    let mut rows: Vec<YamlValue> = Vec::with_capacity(platforms.len());
    let mut total: i64 = 0;
    for platform in &platforms {
        let stored = latest.get(platform).map(|(count, _)| *count).unwrap_or(0);
        // `count = live or stored` with no live counts.
        let count = stored;
        let history = store
            .follower_history(platform, HISTORY_LIMIT)
            .unwrap_or_default();
        let growth = if history.len() >= 2 {
            count - history[0].0
        } else {
            0
        };
        total += count;
        rows.push(object(vec![
            ("platform", string(platform.clone())),
            ("count", integer(count)),
            ("growth", integer(growth)),
            (
                "history",
                array(
                    history
                        .iter()
                        .map(|(count, captured_at)| {
                            object(vec![
                                ("count", integer(*count)),
                                ("captured_at", string(captured_at.clone())),
                            ])
                        })
                        .collect(),
                ),
            ),
        ]));
    }

    let payload = object(vec![
        ("total_followers", integer(total)),
        ("platforms", array(rows)),
    ]);

    if json_mode {
        let _ = write_text(out, &python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let mut rendered = format!("\nTotal followers: {total}\n\n");
    if let Some(items) = payload
        .get("platforms")
        .and_then(|value| value.as_sequence())
    {
        for row in items {
            let platform = row.get("platform").and_then(|v| v.as_str()).unwrap_or("");
            let count = row.get("count").and_then(|v| v.as_i64()).unwrap_or(0);
            let growth = row.get("growth").and_then(|v| v.as_i64()).unwrap_or(0);
            let growth_text = if growth > 0 {
                format!(" (+{growth})")
            } else {
                String::new()
            };
            rendered.push_str(&format!("  {platform}: {count}{growth_text}\n"));
        }
    }
    rendered.push('\n');
    let _ = write_text(out, &rendered);
    exit::SUCCESS
}
