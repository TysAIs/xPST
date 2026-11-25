//! `best-time` payload: recommended posting times from the analytics ledger.
//!
//! Parity target: `src/xpst/best_time.py::BestTimeAnalyzer` as the MCP tool
//! `xpst_best_time` and the CLI `xpst best-time` both render it. The analysis
//! itself lives in `xpst_store::best_time`; this module owns the two payload
//! shapes (one platform, or every platform) so the surfaces cannot disagree.
//!
//! The ledger is `<config dir>/analytics.db` — `config.config_dir` after
//! `XPSTConfig.load`, which is the same directory `XPST_CONFIG_DIR` names.
//!
//! **Divergence, recorded in the ledger note:** a `views`/`likes` column that
//! holds text makes Python raise `TypeError` out of `analyze()` (the `try` only
//! wraps the query), while this reader skips the row. A ledger the shipped
//! writer produced is integer-typed, so only a hand-edited database can observe
//! the difference.

use std::path::Path;

use xpst_config::output::{array, integer, null, number, object, string};
use xpst_config::Value;

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

/// `_handle_best_time`'s payload: `{platform, best_time}` when a platform was
/// named, `{best_times}` otherwise.
pub fn payload(db: &Path, platform: Option<&str>) -> Value {
    let slots = xpst_store::best_time::analyze(db, platform);
    match platform {
        Some(name) => object(vec![
            ("platform", string(name)),
            ("best_time", slots.first().map_or_else(null, slot_value)),
        ]),
        None => {
            // `best_overall()`: top three per platform, platforms in the order
            // the ranked results first mention them.
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
    }
}
