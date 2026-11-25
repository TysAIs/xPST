//! `xpst_followers` — follower counts per platform from the stored snapshots.
//!
//! Parity target: `_handle_followers()` over
//! `xpst.analytics_store.AnalyticsStore.latest_followers` /
//! `follower_history`. The ledger is `<config dir>/analytics.db` — the same
//! SQLite file `best-time` reads, never a hardcoded home path.
//!
//! `AnalyticsStore.__init__` creates the file and applies the schema, so a
//! fresh profile answers `{total_followers: 0, platform_count: 0, platforms: []}`
//! rather than an error; `Store::open` does the same here.
//!
//! `growth` is `count - history[0].count` over the last seven snapshots, and it
//! is `0` when fewer than two exist — "no history" is not "no growth", but the
//! shipped handler reports both as `0`, so this does too.

use std::path::Path;

use xpst_config::output::{array, integer, object, string};
use xpst_config::Value;

/// The number of snapshots the handler keeps per platform.
const HISTORY_LIMIT: u32 = 7;

/// `_handle_followers()`'s payload.
pub fn payload(db: &Path) -> Result<Value, String> {
    let store = xpst_store::Store::open(db).map_err(|error| error.to_string())?;
    let latest = store
        .latest_followers()
        .map_err(|error| error.to_string())?;

    let mut platforms: Vec<Value> = Vec::new();
    let mut total: i64 = 0;
    // `sorted(latest.items())` — the map is already ordered by platform.
    for (platform, (count, captured_at)) in latest {
        let history = store
            .follower_history(&platform, HISTORY_LIMIT)
            .map_err(|error| error.to_string())?;
        let growth = if history.len() >= 2 {
            count - history[0].0
        } else {
            0
        };
        total += count;
        platforms.push(object(vec![
            ("platform", string(platform)),
            ("count", integer(count)),
            ("growth", integer(growth)),
            ("last_updated", string(captured_at)),
            (
                "history",
                array(
                    history
                        .into_iter()
                        .map(|(count, captured_at)| {
                            object(vec![
                                ("count", integer(count)),
                                ("captured_at", string(captured_at)),
                            ])
                        })
                        .collect(),
                ),
            ),
        ]));
    }

    Ok(object(vec![
        ("total_followers", integer(total)),
        ("platform_count", integer(platforms.len() as i64)),
        ("platforms", array(platforms)),
    ]))
}
