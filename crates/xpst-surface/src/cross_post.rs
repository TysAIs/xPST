//! `xpst_cross_post_analytics` — cross-post correlation over the local ledger.
//!
//! Parity target: `xpst.dashboard.analytics.AnalyticsReadModel.get_cross_post_analytics`
//! over `xpst.analytics_store.AnalyticsStore.get_cross_post_groups` /
//! `latest_for_post`. Offline and fast: it reads `<config dir>/analytics.db`
//! and nothing else.
//!
//! The shipped rules this reproduces, each of which a hand-port gets wrong:
//!
//! * **A missing metric is not a zero.** `_accumulate` leaves a total `None`
//!   until at least one member reports a real value, and a group with any
//!   member missing any of the four metrics reports `total_engagement_rate:
//!   null` — a rate computed from a fabricated zero would undercount silently.
//! * **`views == 0` is two different literals.** The per-platform rate is `0`
//!   (an `int`, rendered `0`) because the guard is `if views > 0 ... else 0`.
//!   The group rate is computed *before* its own zero check, so a group whose
//!   every member reported zero views raises `ZeroDivisionError` and the
//!   `if total_views == 0: total_er = 0.0` line below it is dead code.
//!   Reproduced, not fixed: the wire body is `Error: division by zero`.
//! * **`round(x, 1)` is half-to-even on the exact binary value**, and the
//!   rounded value is what the tier is computed from.
//! * **`_engagement_tier(None)` is `"unknown"`**, never `"low"`.
//! * **`p["url"]` is a hard index**, so a group member without a `url` key is
//!   a `KeyError` that escapes the handler as `Error: 'url'` — not a null.
//! * **`_row_to_dict` merges the `extra` JSON over the row**, so an `extra`
//!   object carrying `views` overrides the column. Reproduced, because the
//!   shipped reader does it.

use std::path::Path;

use xpst_config::output::{array, integer, null, number, object, string};
use xpst_config::Value;

/// `get_cross_post_groups()`'s defaults — the MCP tool takes no arguments.
const GROUP_LIMIT: i64 = 50;
const GROUP_OFFSET: i64 = 0;

/// The four metrics the report aggregates, in the shipped order.
const METRICS: [&str; 4] = ["views", "likes", "comments", "shares"];

/// A failure the Python handler would have raised, carrying `str(exception)`.
///
/// The MCP handler serves it as `Error: <message>`, which is exactly what the
/// shipped server sends for an uncaught exception.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CrossPostError {
    pub message: String,
}

impl CrossPostError {
    fn store(detail: impl std::fmt::Display) -> Self {
        Self {
            message: detail.to_string(),
        }
    }

    /// `KeyError('url')` → `str(...)` is `'url'`.
    fn key(name: &str) -> Self {
        Self {
            message: format!("'{name}'"),
        }
    }

    fn type_error(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

/// `_engagement_tier()`.
pub fn engagement_tier(rate: Option<f64>) -> &'static str {
    match rate {
        None => "unknown",
        Some(rate) if rate >= 5.0 => "high",
        Some(rate) if rate >= 1.0 => "medium",
        Some(_) => "low",
    }
}

/// `_accumulate()`: a missing value never turns a missing total into a zero.
fn accumulate(total: Option<i64>, value: Option<i64>) -> Option<i64> {
    match value {
        None => total,
        Some(number) => Some(total.unwrap_or(0) + number),
    }
}

/// `round(value, 1)` — half-to-even on the exact binary value, which is what
/// `format!("{:.1}")` does too.
fn round1(value: f64) -> f64 {
    format!("{value:.1}").parse().unwrap_or(value)
}

/// The metric a snapshot row reports for `key`, after `_row_to_dict`'s
/// `extra` merge.
fn metric(row: &xpst_store::SnapshotRow, key: &str) -> Option<i64> {
    if let Some(extra) = row.extra_json.as_deref() {
        if let Ok(serde_json::Value::Object(map)) = serde_json::from_str(extra) {
            if let Some(value) = map.get(key) {
                return value.as_i64().or_else(|| {
                    // `int(value)` on a float truncates toward zero; a string
                    // that is not a number raises, and `_accumulate` swallows
                    // that into "missing".
                    value.as_f64().map(|float| float as i64)
                });
            }
        }
    }
    match key {
        "views" => row.views,
        "likes" => row.likes,
        "comments" => row.comments,
        "shares" => row.shares,
        _ => None,
    }
}

/// One group member as the ledger stored it: `{platform, post_id, url}`.
struct Member {
    platform: String,
    post_id: String,
    /// Kept as the raw JSON the ledger stored, so a non-string `url` survives
    /// the round trip exactly as Python's `p["url"]` would.
    url: serde_json::Value,
}

/// `_group_row_to_dict()`'s `platforms` field, then the handler's `p[...]`
/// indexing.
fn members(group: &xpst_store::CrossPostGroup) -> Result<Vec<Member>, CrossPostError> {
    let parsed: serde_json::Value = match serde_json::from_str(&group.platforms_json) {
        Ok(value) => value,
        // `_group_row_to_dict` swallows a parse failure into an empty list.
        Err(_) => return Ok(Vec::new()),
    };
    let entries = match parsed {
        serde_json::Value::Array(entries) => entries,
        // `for p in platforms` over a non-list iterates keys (a dict) or
        // raises (a scalar); both end in a subscript failure.
        serde_json::Value::Object(map) => {
            if map.is_empty() {
                return Ok(Vec::new());
            }
            return Err(CrossPostError::type_error(
                "string indices must be integers, not 'str'",
            ));
        }
        serde_json::Value::Null => return Ok(Vec::new()),
        _ => return Err(CrossPostError::type_error("'int' object is not iterable")),
    };

    let mut members = Vec::with_capacity(entries.len());
    for entry in entries {
        let map = match entry {
            serde_json::Value::Object(map) => map,
            serde_json::Value::String(_) => {
                return Err(CrossPostError::type_error(
                    "string indices must be integers, not 'str'",
                ))
            }
            _ => {
                return Err(CrossPostError::type_error(
                    "'int' object is not subscriptable",
                ))
            }
        };
        let platform = map
            .get("platform")
            .ok_or_else(|| CrossPostError::key("platform"))?;
        let post_id = map
            .get("post_id")
            .ok_or_else(|| CrossPostError::key("post_id"))?;
        let url = map.get("url").ok_or_else(|| CrossPostError::key("url"))?;
        members.push(Member {
            platform: platform.as_str().unwrap_or_default().to_string(),
            post_id: post_id.as_str().unwrap_or_default().to_string(),
            url: url.clone(),
        });
    }
    Ok(members)
}

/// `get_cross_post_analytics()` — the list of groups, in the ledger's order.
pub fn groups(db: &Path) -> Result<Vec<Value>, CrossPostError> {
    let store = xpst_store::Store::open(db).map_err(CrossPostError::store)?;
    let rows = store
        .get_cross_post_groups(GROUP_LIMIT, GROUP_OFFSET)
        .map_err(CrossPostError::store)?;

    let mut result = Vec::with_capacity(rows.len());
    for group in &rows {
        let members = members(group)?;

        let mut totals: [Option<i64>; 4] = [None; 4];
        let mut group_missing = false;
        let mut platform_metrics = xpst_config::Mapping::new();

        for member in &members {
            let snapshots = store
                .latest_for_post(&member.platform, &member.post_id)
                .map_err(CrossPostError::store)?;
            // `metrics = snapshots[-1] if snapshots else {}`
            let latest = snapshots.last();

            let values: [Option<i64>; 4] =
                std::array::from_fn(|index| latest.and_then(|row| metric(row, METRICS[index])));
            for value in values {
                if value.is_none() {
                    group_missing = true;
                }
            }
            for (index, value) in values.iter().enumerate() {
                totals[index] = accumulate(totals[index], *value);
            }

            let [views, likes, comments, shares] = values;
            // An engagement rate is only honest when EVERY component was
            // reported; otherwise it is `null` ("unknown"), never a low number
            // computed from a fabricated zero.
            let rate: Option<f64> = match (views, likes, comments, shares) {
                (Some(views), Some(likes), Some(comments), Some(shares)) => {
                    if views > 0 {
                        Some(round1(
                            (likes + comments + shares) as f64 / views as f64 * 100.0,
                        ))
                    } else {
                        // Python's `else 0` is an int: the wire says `0`.
                        Some(0.0)
                    }
                }
                _ => None,
            };
            let rate_value = match (views, likes, comments, shares) {
                (Some(views), Some(_), Some(_), Some(_)) if views <= 0 => integer(0),
                (Some(_), Some(_), Some(_), Some(_)) => number(rate.unwrap_or_default()),
                _ => null(),
            };

            platform_metrics.insert(
                xpst_config::Value::String(member.platform.clone()),
                object(vec![
                    ("post_id", string(member.post_id.clone())),
                    ("url", xpst_config::output::from_json(&member.url)),
                    ("views", optional_integer(views)),
                    ("likes", optional_integer(likes)),
                    ("comments", optional_integer(comments)),
                    ("shares", optional_integer(shares)),
                    ("engagement_rate", rate_value),
                    ("engagement_tier", string(engagement_tier(rate))),
                ]),
            );
        }

        let [total_views, total_likes, total_comments, total_shares] = totals;
        let total_rate: Option<f64> = match (total_views, total_likes, total_comments, total_shares)
        {
            (Some(views), Some(likes), Some(comments), Some(shares)) if !group_missing => {
                // Python computes `(likes + comments + shares) / total_views *
                // 100` BEFORE the `if total_views == 0: total_er = 0.0` line, so
                // a group whose every member reported zero views raises
                // `ZeroDivisionError` and the zero branch is dead code. The
                // handler does not catch it, so the wire body is
                // `Error: division by zero`. Reproduced, not fixed.
                if views == 0 {
                    return Err(CrossPostError::type_error("division by zero"));
                }
                Some(round1(
                    (likes + comments + shares) as f64 / views as f64 * 100.0,
                ))
            }
            _ => None,
        };
        let total_rate_value = match total_rate {
            Some(rate) => number(rate),
            None => null(),
        };

        result.push(object(vec![
            ("content_hash", string(group.content_hash.clone())),
            ("video_id", string(group.video_id.clone())),
            ("caption", optional_string(group.caption.clone())),
            ("source_url", optional_string(group.source_url.clone())),
            ("created_at", string(group.created_at.clone())),
            ("platforms", xpst_config::Value::Mapping(platform_metrics)),
            ("total_views", optional_integer(total_views)),
            ("total_likes", optional_integer(total_likes)),
            ("total_comments", optional_integer(total_comments)),
            ("total_shares", optional_integer(total_shares)),
            ("total_engagement_rate", total_rate_value),
            ("engagement_tier", string(engagement_tier(total_rate))),
        ]));
    }
    Ok(result)
}

/// `_handle_cross_post_analytics()`'s payload.
pub fn payload(db: &Path) -> Result<Value, CrossPostError> {
    let groups = groups(db)?;
    let count = groups.len() as i64;
    Ok(object(vec![
        ("cross_post_groups", array(groups)),
        ("group_count", integer(count)),
    ]))
}

fn optional_integer(value: Option<i64>) -> Value {
    match value {
        Some(value) => integer(value),
        None => null(),
    }
}

fn optional_string(value: Option<String>) -> Value {
    match value {
        Some(value) => string(value),
        None => null(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_metric_never_becomes_a_zero() {
        assert_eq!(accumulate(None, None), None);
        assert_eq!(accumulate(None, Some(0)), Some(0));
        assert_eq!(accumulate(Some(5), None), Some(5));
        assert_eq!(accumulate(Some(5), Some(3)), Some(8));
    }

    #[test]
    fn the_tier_is_unknown_for_a_missing_rate() {
        assert_eq!(engagement_tier(None), "unknown");
        assert_eq!(engagement_tier(Some(5.0)), "high");
        assert_eq!(engagement_tier(Some(4.9)), "medium");
        assert_eq!(engagement_tier(Some(1.0)), "medium");
        assert_eq!(engagement_tier(Some(0.9)), "low");
        assert_eq!(engagement_tier(Some(0.0)), "low");
    }

    #[test]
    fn round_one_is_half_to_even() {
        assert_eq!(round1(2.25), 2.2);
        assert_eq!(round1(2.35), 2.4);
        assert_eq!(round1(0.05), 0.1);
        assert_eq!(round1(1.0 / 3.0 * 100.0), 33.3);
    }
}
