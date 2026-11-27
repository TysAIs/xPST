//! `xpst_analytics` — the local half: the outcome report over the ledger.
//!
//! Parity target: `_handle_analytics()` with `live=false`, which is
//! `AnalyticsCollector.outcome_report()` → `xpst.analytics_outcomes.build_outcome_report()`
//! plus the ownership gate (`_get_owned_platform_ids`) and `_collection_status`.
//! `live=true` runs a real collection against every platform API; that is the
//! network tranche, so the MCP tool refuses it with a typed `not_implemented`
//! rather than answering a document it could not build.
//!
//! The shipped rules this reproduces, each of which a hand-port gets wrong:
//!
//! * **No data is not zero.** A platform with no metric-bearing owned post
//!   reports `totals: null` and `has_data: false`; the handler's own
//!   `per_platform` aggregate starts every metric at `null` and only adds a
//!   value that a snapshot actually reported.
//! * **Ownership fails closed.** YouTube ownership is a network call
//!   (`channels.list` + a paginated `playlistItems` walk), so this build answers
//!   `unverifiable` for it — the same verdict Python reaches when the check
//!   cannot run. X/Instagram/TikTok/Threads are verified from `state.json`,
//!   which is local and ported whole.
//! * **A recorded number from a FAILED collection is visible but not counted.**
//!   `counted_in_totals` is false for a row whose `collection_status` is
//!   `failed`, and the platform note says the numbers are not current.
//! * **`_metrics_from_row` only walks the canonical families.** X's
//!   `quotes`/`bookmarks` are in its capability list but not in
//!   `ANALYTICS_METRIC_FAMILIES`, so they never reach `metrics` — they only
//!   survive in the raw `posts` row (via the `extra` merge). Reproduced.
//! * **`_row_to_dict` merges `extra` over the row** and normalises
//!   `collection_status` *after* the merge, so an `extra` object carrying
//!   `collection_status` is normalised too.
//! * **`latest()` has no `ORDER BY` in Python.** SQLite happens to return the
//!   grouped rows in `(platform, post_id)` order; `xpst_store::Store::latest`
//!   pins that order so the wire body is reproducible instead of a property of
//!   the query plan.
//!
//! Recorded divergences (see `docs/technical/rust-parity-ledger.md`):
//!
//! * YouTube ownership is always `unverifiable` here. On a profile that holds a
//!   YouTube token Python verifies the channel's uploads playlist and shows its
//!   numbers; this build shows none until the network tranche lands. The
//!   `accounts.youtube.token_file` override is not consulted for the same
//!   reason — both branches answer the same verdict.
//! * A `state.json` whose `posted_videos` is not an object makes Python's
//!   `_platform_has_records` raise `AttributeError`; this build treats it as an
//!   empty document.
//! * A ledger that cannot be opened reports the Rust store's sentence rather
//!   than `sqlite3`'s.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use serde_json::{Map, Value as Json};
use xpst_config::output::{array, boolean, integer, null, number, object, string};
use xpst_config::Value;

use crate::isotime::{fromisoformat, Offset, ParsedStamp};

/// `PLATFORM_ORDER` — the render order, including platforms with nothing.
pub const PLATFORM_ORDER: [&str; 5] = ["youtube", "x", "instagram", "tiktok", "threads"];

/// `ANALYTICS_METRIC_FAMILIES` — the canonical metric names, in order.
pub const ANALYTICS_METRIC_FAMILIES: [&str; 6] =
    ["views", "likes", "comments", "shares", "saves", "reposts"];

/// `PLATFORM_METRIC_CAPABILITIES` — what each platform's integration can report.
pub fn platform_metric_capabilities(platform: &str) -> &'static [&'static str] {
    match platform {
        "youtube" => &["views", "likes", "comments"],
        "x" => &[
            "views",
            "likes",
            "comments",
            "shares",
            "reposts",
            "quotes",
            "bookmarks",
        ],
        "tiktok" => &["views", "likes", "comments", "shares"],
        "instagram" => &["views", "likes", "comments", "shares", "saves"],
        "threads" => &["views", "likes", "comments", "shares", "reposts", "quotes"],
        _ => &[],
    }
}

/// `STATE_OWNERSHIP_PLATFORMS` — platforms verified from `state.json`.
const STATE_OWNERSHIP_PLATFORMS: [&str; 4] = ["x", "instagram", "tiktok", "threads"];

/// `POST_ID_KEYS` — the canonical key, then the legacy fallback.
const POST_ID_KEYS: [&str; 2] = ["id", "post_id"];

/// `COUNTED_STATUSES` — a deleted or failed publish stays visible but its stale
/// view count must not keep inflating the aggregate.
const COUNTED_STATUSES: [&str; 2] = ["published", "hidden"];

const SOURCE_LIVE: &str = "live";
const SOURCE_RECORDED: &str = "recorded";

const COLLECTION_STATUS_FAILED: &str = "failed";
const COLLECTION_STATUS_UNKNOWN: &str = "unknown";

const REASON_POST_NOT_FOUND_UPSTREAM: &str = "post_not_found_upstream";
const REASON_TWIKIT_DETAIL_SHAPE: &str = "twikit_detail_shape";

/// A failure the Python handler would have raised, carrying `str(exception)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnalyticsError {
    pub message: String,
}

impl AnalyticsError {
    pub(crate) fn store(detail: impl std::fmt::Display) -> Self {
        Self {
            message: detail.to_string(),
        }
    }
}

// ── Python scalar semantics ─────────────────────────────────────────────────

/// Python truthiness for a JSON value.
pub(crate) fn truthy(value: Option<&Json>) -> bool {
    match value {
        None | Some(Json::Null) => false,
        Some(Json::Bool(flag)) => *flag,
        Some(Json::Number(number)) => number.as_f64().is_some_and(|value| value != 0.0),
        Some(Json::String(text)) => !text.is_empty(),
        Some(Json::Array(items)) => !items.is_empty(),
        Some(Json::Object(map)) => !map.is_empty(),
    }
}

/// `str(value)` for the scalar shapes a ledger row can carry.
///
/// A container renders as its compact JSON rather than Python's `repr`; the
/// only callers are a timestamp, a status and an error string, and a container
/// in any of those is a corrupt row rather than a document anyone parses.
pub(crate) fn python_str(value: &Json) -> String {
    match value {
        Json::Null => "None".to_string(),
        Json::Bool(true) => "True".to_string(),
        Json::Bool(false) => "False".to_string(),
        Json::Number(number) => number.to_string(),
        Json::String(text) => text.clone(),
        other => serde_json::to_string(other).unwrap_or_default(),
    }
}

/// `str(value or "")` — the shape `normalise_collection_status` reads.
pub(crate) fn python_str_or_empty(value: Option<&Json>) -> String {
    match value {
        Some(value) if truthy(Some(value)) => python_str(value),
        _ => String::new(),
    }
}

/// Python's `int(text)` for a string: surrounding whitespace, an optional sign,
/// and single underscores between digits. `None` where Python raises.
fn python_int(text: &str) -> Option<i64> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    let (sign, digits) = match trimmed.as_bytes()[0] {
        b'+' => (1_i64, &trimmed[1..]),
        b'-' => (-1_i64, &trimmed[1..]),
        _ => (1_i64, trimmed),
    };
    if digits.is_empty() {
        return None;
    }
    let mut value: i64 = 0;
    let mut previous_underscore = true;
    for character in digits.chars() {
        if character == '_' {
            if previous_underscore {
                return None;
            }
            previous_underscore = true;
            continue;
        }
        let digit = character.to_digit(10)? as i64;
        value = value.checked_mul(10)?.checked_add(digit)?;
        previous_underscore = false;
    }
    if previous_underscore {
        return None;
    }
    Some(sign * value)
}

/// `_coerce_metric()`: a numeric metric, with `None` preserved for "not
/// reported". A bool is not a number here, and `int("1.5")` raises.
fn coerce_metric(value: Option<&Json>) -> Option<i64> {
    match value? {
        Json::Null | Json::Bool(_) => None,
        Json::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value.trunc() as i64)),
        Json::String(text) => python_int(text),
        _ => None,
    }
}

/// `_iso()`: a state timestamp as a string, or `None` when absent/blank.
fn iso(value: Option<&Json>) -> Option<String> {
    let value = value?;
    if value.is_null() {
        return None;
    }
    let text = python_str(value);
    let trimmed = text.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// `normalise_collection_status()`: an unlabelled row is `unknown`, never `ok`.
pub(crate) fn normalise_collection_status(value: Option<&Json>) -> &'static str {
    match python_str_or_empty(value).trim().to_lowercase().as_str() {
        "ok" => "ok",
        "partial" => "partial",
        "failed" => "failed",
        "unavailable" => "unavailable",
        "not_collected" => "not_collected",
        _ => COLLECTION_STATUS_UNKNOWN,
    }
}

/// `collection_status_label()` — never blank.
fn collection_status_label(status: &str) -> &'static str {
    match normalise_collection_status(Some(&Json::String(status.to_string()))) {
        "ok" => "Collection ok",
        "partial" => "Collection partial (some posts failed)",
        "failed" => "Collection failed",
        "unavailable" => "Collection unavailable",
        "not_collected" => "Not collected",
        _ => "Collection status unknown",
    }
}

/// `_source_label()`.
fn source_label(data_source: Option<&str>, staleness: &str) -> &'static str {
    match data_source {
        None => "No data",
        Some(SOURCE_LIVE) => "Live (fetched now)",
        Some(_) if staleness == "stale" => "Recorded (stale)",
        Some(_) => "Recorded",
    }
}

// ── Ledger rows ─────────────────────────────────────────────────────────────

/// `_row_to_dict()`: the ledger row as Python's `dict(row)` renders it.
///
/// The `extra` JSON is merged over the row (so it can override a column), and
/// `collection_status` is normalised *after* the merge — an `extra` object
/// carrying a status is normalised too.
pub fn row_to_dict(row: &xpst_store::SnapshotRow) -> Map<String, Json> {
    let mut map = Map::new();
    map.insert("platform".into(), Json::String(row.platform.clone()));
    map.insert("post_id".into(), Json::String(row.post_id.clone()));
    map.insert("captured_at".into(), Json::String(row.captured_at.clone()));
    map.insert("views".into(), optional_integer(row.views));
    map.insert("likes".into(), optional_integer(row.likes));
    map.insert("comments".into(), optional_integer(row.comments));
    map.insert("shares".into(), optional_integer(row.shares));
    map.insert("reposts".into(), optional_integer(row.reposts));
    map.insert("saves".into(), optional_integer(row.saves));
    map.insert(
        "collection_status".into(),
        Json::String(row.collection_status.as_str().to_string()),
    );
    map.insert(
        "collection_error".into(),
        row.collection_error
            .clone()
            .map(Json::String)
            .unwrap_or(Json::Null),
    );
    if let Some(extra) = row.extra_json.as_deref() {
        if let Ok(Json::Object(extra)) = serde_json::from_str::<Json>(extra) {
            for (key, value) in extra {
                map.insert(key, value);
            }
        }
    }
    let status = normalise_collection_status(map.get("collection_status")).to_string();
    map.insert("collection_status".into(), Json::String(status));
    if let Some(value) = map.get("collection_error").cloned() {
        if !value.is_null() {
            map.insert("collection_error".into(), Json::String(python_str(&value)));
        }
    }
    map
}

fn optional_integer(value: Option<i64>) -> Json {
    value.map(Json::from).unwrap_or(Json::Null)
}

/// `resolve_platform_post_id()`: the canonical `id`, then the legacy `post_id`.
pub fn resolve_platform_post_id(info: Option<&Map<String, Json>>) -> String {
    let Some(info) = info else {
        return String::new();
    };
    for key in POST_ID_KEYS {
        if let Some(value) = info.get(key) {
            if truthy(Some(value)) {
                return python_str(value).trim().to_string();
            }
        }
    }
    String::new()
}

/// `_outcome_status()`: classify a recorded publish outcome.
fn outcome_status(info: &Map<String, Json>, error: Option<&Json>) -> &'static str {
    if truthy(info.get("deleted")) {
        return "deleted";
    }
    if resolve_platform_post_id(Some(info)).is_empty() && truthy(error) {
        return "failed";
    }
    let visibility = python_str_or_empty(info.get("visibility")).to_lowercase();
    if truthy(info.get("soft_hidden")) || visibility == "private" || visibility == "unlisted" {
        return "hidden";
    }
    "published"
}

/// `_metrics_from_row()`: only the platform's declared capability, and only the
/// canonical families — X's `quotes`/`bookmarks` are capability metrics but not
/// families, so they never reach `metrics`.
fn metrics_from_row(row: &Map<String, Json>, platform: &str) -> Vec<(&'static str, i64)> {
    let allowed = platform_metric_capabilities(platform);
    let mut metrics = Vec::new();
    for key in ANALYTICS_METRIC_FAMILIES {
        if !allowed.contains(&key) {
            continue;
        }
        if let Some(value) = coerce_metric(row.get(key)) {
            metrics.push((key, value));
        }
    }
    metrics
}

/// `_recipient_index()`: `(platform, post_id)` → row, last write wins.
fn recipient_index(rows: &[Map<String, Json>]) -> BTreeMap<(String, String), &Map<String, Json>> {
    let mut index = BTreeMap::new();
    for row in rows {
        let platform = python_str_or_empty(row.get("platform")).to_lowercase();
        let post_id = python_str_or_empty(row.get("post_id"));
        if platform.is_empty() || post_id.is_empty() {
            continue;
        }
        index.insert((platform, post_id), row);
    }
    index
}

// ── State and ownership ─────────────────────────────────────────────────────

/// `_read_state()`: `state.json` as an object, or an empty one when unusable.
pub(crate) fn read_state(config_dir: &Path) -> Json {
    let path = config_dir.join("state.json");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Json::Object(Map::new());
    };
    match serde_json::from_str::<Json>(&text) {
        Ok(value @ Json::Object(_)) => value,
        _ => Json::Object(Map::new()),
    }
}

/// `_read_state_platform_ids()`: `(verified, ids)` from `state.json`.
///
/// A valid state file with no ids is a verified empty set; a missing,
/// malformed or structurally invalid one is unverifiable.
fn read_state_platform_ids(config_dir: &Path, platform: &str) -> (bool, BTreeSet<String>) {
    let path = config_dir.join("state.json");
    if !path.exists() {
        return (false, BTreeSet::new());
    }
    let Ok(text) = std::fs::read_to_string(&path) else {
        return (false, BTreeSet::new());
    };
    let Ok(state) = serde_json::from_str::<Json>(&text) else {
        return (false, BTreeSet::new());
    };
    let Json::Object(state) = state else {
        return (false, BTreeSet::new());
    };
    let posted = match state.get("posted_videos") {
        None => &Json::Object(Map::new()),
        Some(value @ Json::Object(_)) => value,
        Some(_) => return (false, BTreeSet::new()),
    };
    let Json::Object(posted) = posted else {
        return (false, BTreeSet::new());
    };

    let mut ids = BTreeSet::new();
    for data in posted.values() {
        let Json::Object(data) = data else {
            continue;
        };
        let posted_to = match data.get("posted_to") {
            Some(value @ Json::Object(_)) => value,
            _ => continue,
        };
        let Json::Object(posted_to) = posted_to else {
            continue;
        };
        let info = match posted_to.get(platform) {
            Some(value @ Json::Object(_)) => value,
            _ => continue,
        };
        let Json::Object(info) = info else {
            continue;
        };
        let post_id = resolve_platform_post_id(Some(info));
        if !post_id.is_empty() {
            ids.insert(post_id);
        }
    }
    (true, ids)
}

/// `_get_owned_platform_ids()`.
///
/// `None` means ownership cannot be verified — the platform fails closed.
pub(crate) fn owned_platform_ids(
    config_dir: &Path,
    platform: &str,
    state: &Json,
) -> Option<BTreeSet<String>> {
    let platform = platform.to_lowercase();
    if platform == "youtube" {
        return youtube_owned_ids(config_dir);
    }
    if STATE_OWNERSHIP_PLATFORMS.contains(&platform.as_str()) {
        // `_read_state_platform_ids` re-reads the file; `state` is the same
        // document `outcome_report` already parsed, so the verdict is identical.
        let _ = state;
        let (verified, ids) = read_state_platform_ids(config_dir, &platform);
        return if verified { Some(ids) } else { None };
    }
    None
}

/// `_get_owned_youtube_ids()` — a network call, so this build fails closed.
///
/// Python verifies ownership against the authenticated channel's uploads
/// playlist (`channels.list` → `playlistItems`, fully paginated). Both branches
/// here answer `None`, which is exactly what Python answers when the check
/// cannot run: no token, or a token whose verification failed. The token file is
/// still probed so the branch is explicit and a test can prove it.
fn youtube_owned_ids(config_dir: &Path) -> Option<BTreeSet<String>> {
    let token = config_dir.join("credentials").join("youtube_token.json");
    if token.exists() {
        // A token is present, so Python would call the API here. The network
        // tranche owns that; until then the honest answer is "unverifiable".
        return None;
    }
    None
}

/// `_platform_has_records()`: is there anything worth verifying ownership for?
pub(crate) fn platform_has_records(
    platform: &str,
    state: &Json,
    snapshot_rows: &[Map<String, Json>],
    live_rows: &[Map<String, Json>],
) -> bool {
    if let Some(Json::Object(posted)) = state.get("posted_videos") {
        for data in posted.values() {
            let Json::Object(data) = data else {
                continue;
            };
            if let Some(Json::Object(posted_to)) = data.get("posted_to") {
                if matches!(posted_to.get(platform), Some(Json::Object(_))) {
                    return true;
                }
            }
        }
    }
    snapshot_rows
        .iter()
        .chain(live_rows.iter())
        .any(|row| python_str_or_empty(row.get("platform")).to_lowercase() == platform)
}

// ── Collection status ───────────────────────────────────────────────────────

/// `_persisted_collection_status()`: the ledger's last recorded outcome.
fn persisted_collection_status(
    platform: &str,
    runs: &BTreeMap<String, xpst_store::CollectionRunRow>,
) -> Option<Json> {
    let recorded = runs.get(&platform.trim().to_lowercase())?;
    let mut block = Map::new();
    block.insert("attempted".into(), optional_integer(recorded.attempted));
    block.insert("failures".into(), optional_integer(recorded.failures));
    block.insert("requested".into(), optional_integer(recorded.requested));
    block.insert("collected".into(), optional_integer(recorded.collected));
    // `not_found: None` — the ledger has no per-id record.
    block.insert("not_found".into(), Json::Null);
    block.insert(
        "status".into(),
        Json::String(recorded.status.as_str().to_string()),
    );
    block.insert(
        "reason".into(),
        recorded
            .reason
            .clone()
            .map(Json::String)
            .unwrap_or(Json::Null),
    );
    block.insert(
        "detail".into(),
        recorded
            .detail
            .clone()
            .map(Json::String)
            .unwrap_or(Json::Null),
    );
    block.insert(
        "error".into(),
        recorded
            .error
            .clone()
            .map(Json::String)
            .unwrap_or(Json::Null),
    );
    block.insert(
        "recorded_at".into(),
        Json::String(recorded.captured_at.clone()),
    );
    block.insert("source".into(), Json::String(SOURCE_RECORDED.to_string()));
    Some(Json::Object(block))
}

/// `_collection_status()` for a process that has not collected anything.
///
/// With no live attempt, no live failure and no unavailability, the answer comes
/// from the persisted ledger; a platform with neither reports `unknown` — never
/// a silent `ok`.
pub(crate) fn collection_status(
    platform: &str,
    runs: &BTreeMap<String, xpst_store::CollectionRunRow>,
) -> Json {
    if let Some(persisted) = persisted_collection_status(platform, runs) {
        return persisted;
    }
    let mut block = Map::new();
    block.insert("attempted".into(), Json::from(0));
    block.insert("failures".into(), Json::from(0));
    block.insert("not_found".into(), Json::from(0));
    block.insert(
        "status".into(),
        Json::String(COLLECTION_STATUS_UNKNOWN.to_string()),
    );
    block.insert("reason".into(), Json::Null);
    block.insert("error".into(), Json::Null);
    block.insert("source".into(), Json::Null);
    Json::Object(block)
}

/// `_derive_collection_status()`: `(status, source, error, recorded_at)`.
fn derive_collection_status(
    status_block: Option<&Json>,
    collection_error: Option<&Json>,
    unavailable: Option<&Json>,
) -> (String, Option<String>, Option<String>, Option<String>) {
    if let Some(Json::Object(block)) = status_block {
        if truthy(block.get("status")) {
            let status = normalise_collection_status(block.get("status")).to_string();
            let mut source = match block.get("source") {
                Some(value) if truthy(Some(value)) => Some(python_str(value)),
                _ => None,
            };
            if source.is_none() && status != COLLECTION_STATUS_UNKNOWN {
                source = Some(if collection_error.is_some() {
                    SOURCE_LIVE.to_string()
                } else {
                    SOURCE_RECORDED.to_string()
                });
            }
            if status == COLLECTION_STATUS_UNKNOWN && collection_error.is_none() {
                source = None;
            }
            let error = block
                .get("error")
                .filter(|value| truthy(Some(value)))
                .map(python_str);
            let recorded_at = iso(block
                .get("recorded_at")
                .filter(|value| truthy(Some(value)))
                .or_else(|| block.get("captured_at")));
            return (status, source, error, recorded_at);
        }
    }
    if let Some(Json::Object(error)) = collection_error {
        if truthy(error.get("failures")) {
            let requested = coerce_metric(error.get("requested")).unwrap_or(0);
            let collected = coerce_metric(error.get("collected")).unwrap_or(0);
            let status = if requested != 0 && collected != 0 {
                "partial"
            } else {
                COLLECTION_STATUS_FAILED
            };
            let message = error
                .get("error")
                .filter(|value| truthy(Some(value)))
                .map(python_str);
            return (
                status.to_string(),
                Some(SOURCE_LIVE.to_string()),
                message,
                None,
            );
        }
    }
    if unavailable.is_some() {
        return (
            "unavailable".to_string(),
            Some(SOURCE_LIVE.to_string()),
            None,
            None,
        );
    }
    (COLLECTION_STATUS_UNKNOWN.to_string(), None, None, None)
}

// ── Freshness ───────────────────────────────────────────────────────────────

/// `_parse_utc_timestamp()`: an aware UTC instant, or `None`.
pub(crate) fn parse_utc_timestamp(value: &str) -> Option<jiff::Timestamp> {
    let stamp: ParsedStamp = fromisoformat(&value.replace('Z', "+00:00")).ok()?;
    // A naive stamp is UTC: `_parse_utc_timestamp` and `_parse_ts` both do
    // `parsed.replace(tzinfo=timezone.utc)`, and legacy state files carry
    // naive timestamps.
    let offset = stamp.offset.unwrap_or(Offset {
        seconds: 0,
        micros: 0,
    });
    let offset = jiff::tz::Offset::from_seconds(offset.seconds as i32).ok()?;
    let zone = jiff::tz::TimeZone::fixed(offset);
    let zoned = stamp.civil.to_zoned_datetime(zone).ok()?;
    Some(zoned.timestamp())
}

/// `_freshness_metadata()`: `(last_captured, staleness_hours, staleness)`.
pub(crate) fn freshness(
    timestamp: Option<&str>,
    now: jiff::Timestamp,
) -> (Option<String>, Option<f64>, &'static str) {
    let Some(timestamp) = timestamp else {
        return (None, None, "unknown");
    };
    let Some(captured) = parse_utc_timestamp(timestamp) else {
        return (None, None, "unknown");
    };
    let seconds = (now.as_nanosecond() - captured.as_nanosecond()) as f64 / 1_000_000_000.0;
    let hours = (seconds / 3600.0).max(0.0);
    let rounded: f64 = format!("{hours:.3}").parse().unwrap_or(hours);
    let staleness = if hours < 24.0 { "fresh" } else { "stale" };
    (Some(timestamp.to_string()), Some(rounded), staleness)
}

// ── The platform note ───────────────────────────────────────────────────────

/// `_platform_note()`: one honest sentence for a platform row.
#[allow(clippy::too_many_arguments)]
fn platform_note(
    has_data: bool,
    checked: bool,
    verified: bool,
    outcome_count: usize,
    collection_error: Option<&Json>,
    unavailable: Option<&Json>,
    collection_status: &str,
    collection_status_source: Option<&str>,
    recorded_error: Option<&str>,
) -> Option<String> {
    if let Some(Json::Object(error)) = collection_error {
        if truthy(error.get("failures")) {
            let message = error
                .get("error")
                .filter(|value| truthy(Some(value)))
                .map(python_str)
                .unwrap_or_else(|| "collection error".to_string());
            let failed = coerce_metric(error.get("requested"))
                .or_else(|| coerce_metric(error.get("failures")))
                .unwrap_or(0);
            let kept = if has_data {
                " Keeping previously recorded numbers."
            } else {
                ""
            };
            return Some(format!(
                "Collection failed for {failed} post(s): {message}.{kept}"
            ));
        }
    }
    if collection_status == COLLECTION_STATUS_FAILED && collection_status_source == Some("recorded")
    {
        let message = recorded_error.unwrap_or("collection error");
        if has_data {
            return Some(format!(
                "Recorded numbers are not current: the last collection attempt failed ({message})."
            ));
        }
        return Some(format!("Collection failed on the last attempt: {message}."));
    }
    if let Some(Json::Object(unavailable)) = unavailable {
        let reason = unavailable
            .get("reason")
            .filter(|value| truthy(Some(value)))
            .map(python_str)
            .unwrap_or_else(|| "unavailable".to_string());
        if has_data {
            if reason == REASON_POST_NOT_FOUND_UPSTREAM {
                let posts = unavailable.get("posts").map(python_str).unwrap_or_default();
                return Some(format!(
                    "Kept recorded numbers — {posts} recorded post(s) no longer exist upstream \
                     ({reason}), so this run had nothing to refresh"
                ));
            }
            return Some(format!(
                "Kept recorded numbers — collection unavailable ({reason})"
            ));
        }
        if reason == REASON_POST_NOT_FOUND_UPSTREAM {
            let posts = match unavailable.get("posts") {
                Some(Json::Null) | None => String::new(),
                Some(value) => python_str(value),
            };
            return Some(
                format!(
                    "{posts} recorded post(s) no longer exist upstream ({reason}) — nothing to \
                     collect, not a collection failure"
                )
                .trim()
                .to_string(),
            );
        }
        if reason == REASON_TWIKIT_DETAIL_SHAPE {
            return Some(format!(
                "X's TweetDetail response shape is unreadable ({reason}) — analytics unavailable \
                 until the parser is updated"
            ));
        }
        let detail = unavailable
            .get("detail")
            .map(python_str)
            .unwrap_or_default()
            .trim()
            .to_string();
        return Some(format!(
            "Collection unavailable ({reason}){}",
            if detail.is_empty() {
                String::new()
            } else {
                format!(": {detail}")
            }
        ));
    }
    if has_data {
        if collection_status == COLLECTION_STATUS_UNKNOWN {
            return Some(
                "Recorded numbers carry no collection status (unknown) — whether the capture \
                 completed cannot be confirmed."
                    .to_string(),
            );
        }
        return None;
    }
    if checked && !verified {
        return Some(
            "Ownership could not be verified — no numbers are shown for this platform".to_string(),
        );
    }
    if outcome_count > 0 {
        return Some("Posts are recorded, but no metrics have been captured yet".to_string());
    }
    Some("No posts recorded for this platform".to_string())
}

// ── The report ──────────────────────────────────────────────────────────────

/// Everything `build_outcome_report()` reads.
pub struct ReportRequest<'a> {
    pub state: &'a Json,
    pub snapshots: &'a [Map<String, Json>],
    pub live_rows: &'a [Map<String, Json>],
    /// `{platform: verified ids}`; a platform present with `None` was checked
    /// and failed, so it fails closed.
    pub owned_ids: &'a BTreeMap<String, Option<BTreeSet<String>>>,
    pub collection_status: &'a BTreeMap<String, Json>,
    pub now: jiff::Timestamp,
}

/// `build_outcome_report()`.
pub fn build_outcome_report(request: &ReportRequest<'_>) -> Value {
    let current = request.now;
    let recorded = recipient_index(request.snapshots);
    let live = recipient_index(request.live_rows);

    let posted = match request.state.get("posted_videos") {
        Some(Json::Object(posted)) => posted.clone(),
        _ => Map::new(),
    };

    let mut platforms_out = xpst_config::Mapping::new();
    let mut foreign_dropped: Vec<String> = Vec::new();
    let mut unverified: Vec<String> = Vec::new();
    let mut collection_failed: Vec<String> = Vec::new();
    let unavailable_platforms = xpst_config::Mapping::new();
    let mut unknown_collection_platforms: Vec<String> = Vec::new();
    let mut untrusted_recorded_platforms: Vec<String> = Vec::new();
    let mut any_live = false;
    let mut any_data = false;

    for platform in PLATFORM_ORDER {
        let checked = request.owned_ids.contains_key(platform);
        let owned = request.owned_ids.get(platform).and_then(|ids| ids.as_ref());
        let verified = checked && owned.is_some();
        if checked && !verified {
            unverified.push(platform.to_string());
        }

        let mut outcomes: Vec<Value> = Vec::new();
        // `dropped_foreign` is a COUNTER in the shipped report; the list of ids
        // lives in `diagnostics.unowned_ids_excluded`.
        let mut dropped_foreign = 0_usize;
        let mut dropped_unverified = 0_usize;

        for (video_id, video_data) in &posted {
            let Json::Object(video_data) = video_data else {
                continue;
            };
            let posted_to = match video_data.get("posted_to") {
                Some(Json::Object(posted_to)) => posted_to,
                _ => continue,
            };
            let info = match posted_to.get(platform) {
                Some(Json::Object(info)) => info,
                _ => continue,
            };
            let error = match video_data.get("errors") {
                Some(Json::Object(errors)) => errors.get(platform),
                _ => None,
            };
            let post_id = resolve_platform_post_id(Some(info));
            let status = outcome_status(info, error);
            if !verified {
                dropped_unverified += 1;
                continue;
            }
            let owned = owned.expect("verified implies an owned set");
            if post_id.is_empty() || !owned.contains(&post_id) {
                dropped_foreign += 1;
                foreign_dropped.push(format!(
                    "{platform}:{}",
                    if post_id.is_empty() {
                        video_id.as_str()
                    } else {
                        post_id.as_str()
                    }
                ));
                continue;
            }

            let live_row = live.get(&(platform.to_string(), post_id.clone())).copied();
            let recorded_row = recorded
                .get(&(platform.to_string(), post_id.clone()))
                .copied();
            let row = live_row.or(recorded_row);

            let mut metric_source: Option<&'static str> = None;
            let mut metrics: Option<Vec<(&'static str, i64)>> = None;
            let mut captured_at: Option<String> = None;
            let mut row_collection_status: Option<&'static str> = None;
            if let Some(row) = row {
                let candidate = metrics_from_row(row, platform);
                if !candidate.is_empty() {
                    metrics = Some(candidate);
                    captured_at = iso(row
                        .get("captured_at")
                        .filter(|value| truthy(Some(value)))
                        .or_else(|| row.get("timestamp")));
                    metric_source = Some(if live_row.is_some() {
                        SOURCE_LIVE
                    } else {
                        SOURCE_RECORDED
                    });
                    row_collection_status = Some(if live_row.is_some() {
                        "ok"
                    } else {
                        normalise_collection_status(row.get("collection_status"))
                    });
                    if metric_source == Some(SOURCE_LIVE) {
                        any_live = true;
                    }
                }
            }

            let counted = COUNTED_STATUSES.contains(&status)
                && metrics.is_some()
                && row_collection_status != Some(COLLECTION_STATUS_FAILED);

            outcomes.push(object(vec![
                ("post_id", string(post_id.clone())),
                ("video_id", string(video_id.clone())),
                (
                    "url",
                    optional_string(
                        info.get("url")
                            .filter(|value| truthy(Some(value)))
                            .map(python_str),
                    ),
                ),
                (
                    "published_at",
                    optional_string(iso(info
                        .get("timestamp")
                        .filter(|value| truthy(Some(value)))
                        .or_else(|| video_data.get("downloaded_at")))),
                ),
                ("status", string(status)),
                ("outcome_source", string("publish_record")),
                ("counted_in_totals", boolean(counted)),
                (
                    "metric_source",
                    optional_string(metric_source.map(str::to_string)),
                ),
                (
                    "collection_status",
                    optional_string(row_collection_status.map(str::to_string)),
                ),
                ("captured_at", optional_string(captured_at)),
                ("metrics", metrics_value(metrics.as_deref())),
            ]));
        }

        // Owned posts with a snapshot but no local publish record: still proven
        // owned, so their recorded metrics belong in the report.
        let mut seen: BTreeSet<String> = outcomes
            .iter()
            .filter_map(|outcome| outcome.get("post_id"))
            .filter_map(|value| value.as_str())
            .map(str::to_string)
            .collect();
        for (source_rows, source) in [(&live, SOURCE_LIVE), (&recorded, SOURCE_RECORDED)] {
            for ((row_platform, row_post_id), row) in source_rows {
                if row_platform != platform || seen.contains(row_post_id) {
                    continue;
                }
                let owned = match owned {
                    Some(owned) if verified && owned.contains(row_post_id) => owned,
                    _ => continue,
                };
                let _ = owned;
                let candidate = metrics_from_row(row, platform);
                if candidate.is_empty() {
                    continue;
                }
                seen.insert(row_post_id.clone());
                if source == SOURCE_LIVE {
                    any_live = true;
                }
                let outcome_status = if source == SOURCE_LIVE {
                    "ok"
                } else {
                    normalise_collection_status(row.get("collection_status"))
                };
                outcomes.push(object(vec![
                    ("post_id", string(row_post_id.clone())),
                    ("video_id", string(row_post_id.clone())),
                    (
                        "url",
                        optional_string(
                            row.get("url")
                                .filter(|value| truthy(Some(value)))
                                .map(python_str),
                        ),
                    ),
                    ("published_at", null()),
                    ("status", string("published")),
                    ("outcome_source", string("snapshot")),
                    (
                        "counted_in_totals",
                        boolean(outcome_status != COLLECTION_STATUS_FAILED),
                    ),
                    ("metric_source", string(source)),
                    ("collection_status", string(outcome_status)),
                    (
                        "captured_at",
                        optional_string(iso(row
                            .get("captured_at")
                            .filter(|value| truthy(Some(value)))
                            .or_else(|| row.get("timestamp")))),
                    ),
                    ("metrics", metrics_value(Some(&candidate))),
                ]));
            }
        }

        // `sort(key=..., reverse=True)` is stable: ties keep their order.
        outcomes.sort_by(|left, right| {
            let key = |value: &Value| {
                value
                    .get("published_at")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string()
            };
            key(right).cmp(&key(left))
        });

        let mut totals: Vec<(&'static str, i64)> = Vec::new();
        let mut captures: Vec<String> = Vec::new();
        for outcome in &outcomes {
            if !outcome
                .get("counted_in_totals")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                continue;
            }
            if let Some(Value::Mapping(metrics)) = outcome.get("metrics") {
                for (key, value) in metrics.iter() {
                    let Some(key) = key.as_str() else { continue };
                    // The metric keys are always canonical families, so the
                    // static name is available and `totals` stays `'static`.
                    let Some(key) = ANALYTICS_METRIC_FAMILIES
                        .iter()
                        .copied()
                        .find(|family| *family == key)
                    else {
                        continue;
                    };
                    let Some(value) = value.as_i64() else {
                        continue;
                    };
                    match totals.iter_mut().find(|(name, _)| *name == key) {
                        Some(entry) => entry.1 += value,
                        None => totals.push((key, value)),
                    }
                }
            }
            if let Some(Value::String(captured)) = outcome.get("captured_at") {
                captures.push(captured.clone());
            }
        }

        let has_data = !totals.is_empty();
        if has_data {
            any_data = true;
        }
        let last_captured = captures.into_iter().max();
        let (fresh_last, fresh_hours, staleness) = freshness(last_captured.as_deref(), current);
        let capability = platform_metric_capabilities(platform);
        let data_source = if has_data {
            let live_outcome = outcomes.iter().any(|outcome| {
                outcome.get("metric_source").and_then(Value::as_str) == Some(SOURCE_LIVE)
            });
            Some(if live_outcome {
                SOURCE_LIVE
            } else {
                SOURCE_RECORDED
            })
        } else {
            None
        };

        let collection_error: Option<&Json> = None;
        let platform_unavailable: Option<&Json> = None;
        let status_block = request.collection_status.get(platform);
        let (platform_status, status_source, recorded_error, last_attempt_at) =
            derive_collection_status(status_block, collection_error, platform_unavailable);
        if platform_status == COLLECTION_STATUS_FAILED
            && status_source.as_deref() == Some(SOURCE_LIVE)
            && !collection_failed.iter().any(|name| name == platform)
        {
            collection_failed.push(platform.to_string());
        }
        if platform_status == COLLECTION_STATUS_UNKNOWN {
            unknown_collection_platforms.push(platform.to_string());
        } else if platform_status == COLLECTION_STATUS_FAILED
            && status_source.as_deref() == Some(SOURCE_RECORDED)
        {
            untrusted_recorded_platforms.push(platform.to_string());
        }

        let note = platform_note(
            has_data,
            checked,
            verified,
            outcomes.len(),
            collection_error,
            platform_unavailable,
            &platform_status,
            status_source.as_deref(),
            recorded_error.as_deref(),
        );

        let mut available: Vec<&str> = capability.to_vec();
        available.sort_unstable();
        let mut missing: Vec<&str> = ANALYTICS_METRIC_FAMILIES
            .iter()
            .copied()
            .filter(|family| !capability.contains(family))
            .collect();
        missing.sort_unstable();

        let mut entry = xpst_config::Mapping::new();
        entry.insert(Value::String("platform".into()), string(platform));
        entry.insert(
            Value::String("ownership_verified".into()),
            boolean(verified),
        );
        entry.insert(Value::String("ownership_checked".into()), boolean(checked));
        entry.insert(Value::String("has_data".into()), boolean(has_data));
        entry.insert(
            Value::String("data_source".into()),
            optional_string(data_source.map(str::to_string)),
        );
        entry.insert(
            Value::String("data_source_label".into()),
            string(source_label(data_source, staleness)),
        );
        entry.insert(Value::String("note".into()), optional_string(note));
        entry.insert(
            Value::String("collection_status".into()),
            string(platform_status.clone()),
        );
        entry.insert(
            Value::String("collection_status_label".into()),
            string(collection_status_label(&platform_status)),
        );
        entry.insert(
            Value::String("collection_status_source".into()),
            optional_string(status_source.clone()),
        );
        entry.insert(
            Value::String("last_collection_attempt_at".into()),
            optional_string(last_attempt_at.clone()),
        );
        entry.insert(
            Value::String("recorded_collection_error".into()),
            optional_string(recorded_error.clone()),
        );
        entry.insert(Value::String("collection_error".into()), null());
        entry.insert(Value::String("unavailable".into()), null());
        entry.insert(
            Value::String("posts_recorded".into()),
            integer(outcomes.len() as i64),
        );
        entry.insert(
            Value::String("posts_with_metrics".into()),
            integer(
                outcomes
                    .iter()
                    .filter(|outcome| {
                        outcome
                            .get("metrics")
                            .map(|value| !value.is_null())
                            .unwrap_or(false)
                    })
                    .count() as i64,
            ),
        );
        entry.insert(
            Value::String("metrics_available".into()),
            array(available.into_iter().map(string).collect()),
        );
        entry.insert(
            Value::String("metrics_missing".into()),
            array(missing.into_iter().map(string).collect()),
        );
        entry.insert(
            Value::String("totals".into()),
            if has_data {
                let mut map = xpst_config::Mapping::new();
                for (key, value) in &totals {
                    map.insert(Value::String((*key).into()), integer(*value));
                }
                Value::Mapping(map)
            } else {
                null()
            },
        );
        entry.insert(
            Value::String("last_captured".into()),
            optional_string(fresh_last),
        );
        entry.insert(
            Value::String("staleness_hours".into()),
            match fresh_hours {
                Some(hours) => number(hours),
                None => null(),
            },
        );
        entry.insert(Value::String("staleness".into()), string(staleness));
        entry.insert(Value::String("freshness".into()), string(staleness));
        entry.insert(Value::String("outcomes".into()), array(outcomes));
        let mut dropped = xpst_config::Mapping::new();
        dropped.insert(
            Value::String("unowned".into()),
            integer(dropped_foreign as i64),
        );
        dropped.insert(
            Value::String("ownership_unverified".into()),
            integer(dropped_unverified as i64),
        );
        entry.insert(Value::String("dropped".into()), Value::Mapping(dropped));
        platforms_out.insert(Value::String(platform.to_string()), Value::Mapping(entry));
    }

    let mut diagnostics = xpst_config::Mapping::new();
    diagnostics.insert(
        Value::String("unowned_ids_excluded".into()),
        array(foreign_dropped.into_iter().map(string).collect()),
    );
    diagnostics.insert(
        Value::String("ownership_unverified_platforms".into()),
        array(unverified.into_iter().map(string).collect()),
    );
    diagnostics.insert(
        Value::String("collection_failed_platforms".into()),
        array(collection_failed.into_iter().map(string).collect()),
    );
    diagnostics.insert(
        Value::String("unknown_collection_status_platforms".into()),
        array(
            unknown_collection_platforms
                .into_iter()
                .map(string)
                .collect(),
        ),
    );
    diagnostics.insert(
        Value::String("untrusted_recorded_platforms".into()),
        array(
            untrusted_recorded_platforms
                .into_iter()
                .map(string)
                .collect(),
        ),
    );
    diagnostics.insert(
        Value::String("unavailable_platforms".into()),
        Value::Mapping(unavailable_platforms),
    );

    object(vec![
        ("generated_at", string(utc_isoformat(current))),
        ("live", boolean(any_live)),
        (
            "data_source",
            optional_string(
                if any_live {
                    Some(SOURCE_LIVE)
                } else if any_data {
                    Some(SOURCE_RECORDED)
                } else {
                    None
                }
                .map(str::to_string),
            ),
        ),
        ("platforms", Value::Mapping(platforms_out)),
        ("diagnostics", Value::Mapping(diagnostics)),
    ])
}

/// `datetime.now(timezone.utc).isoformat()` — six digits when the microsecond
/// field is non-zero, none when it is zero.
pub(crate) fn utc_isoformat(instant: jiff::Timestamp) -> String {
    let zoned = instant.to_zoned_datetime(jiff::tz::TimeZone::UTC);
    if zoned.subsec_nanosecond() / 1_000 == 0 {
        zoned.strftime("%Y-%m-%dT%H:%M:%S%:z").to_string()
    } else {
        zoned.strftime("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
    }
}

fn metrics_value(metrics: Option<&[(&'static str, i64)]>) -> Value {
    match metrics {
        None => null(),
        Some(metrics) => {
            let mut map = xpst_config::Mapping::new();
            for (key, value) in metrics {
                map.insert(Value::String((*key).into()), integer(*value));
            }
            Value::Mapping(map)
        }
    }
}

/// Python truthiness for an insertion-ordered value.
fn truthy_cfg(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().is_some_and(|value| value != 0.0),
        Some(Value::String(text)) | Some(Value::Timestamp(text)) => !text.is_empty(),
        Some(Value::Sequence(items)) => !items.is_empty(),
        Some(Value::Mapping(map)) => !map.is_empty(),
        Some(Value::NonFinite(_)) => true,
    }
}

fn optional_string(value: Option<String>) -> Value {
    match value {
        Some(value) => string(value),
        None => null(),
    }
}

// ── The handler payload ─────────────────────────────────────────────────────

/// Which rows the `posts` array and the `platforms` aggregate cover.
///
/// Python passes `arguments.get("platform")` straight into
/// `AnalyticsStore.latest(platform)`, where it becomes a SQLite bind parameter:
/// a falsy value means "no filter", a string filters, and a truthy non-string
/// (a number, `true`) matches no TEXT column. A truthy list/dict raises
/// `sqlite3.InterfaceError`; this build answers the empty document instead
/// (recorded divergence).
#[derive(Debug, Clone, Copy)]
pub enum PlatformFilter<'a> {
    All,
    Named(&'a str),
    Unmatchable,
}

/// `AnalyticsCollector.outcome_report()` with no live collection.
///
/// The recorded-mode report every surface renders: the ledger's latest rows,
/// the persisted collection outcomes, the ownership gate and
/// [`build_outcome_report`]. A ledger that cannot be opened yields an empty
/// report rather than an error — Python's `outcome_report` catches the store
/// failure and reports "no data", because a broken db must not 500 the UI.
///
/// YouTube ownership is a network call in Python (`channels.list` plus a
/// paginated `playlistItems` walk), so this build answers `unverifiable` for
/// it — the same verdict Python reaches when the check cannot run.
pub fn outcome_report(config_dir: &Path, now: jiff::Timestamp) -> Value {
    let db = config_dir.join("analytics.db");
    let (all_rows, runs) = match xpst_store::Store::open(&db) {
        Ok(store) => {
            let rows = store
                .latest(None)
                .map(|rows| rows.iter().map(row_to_dict).collect::<Vec<_>>())
                .unwrap_or_default();
            let runs = store.latest_collection_runs().unwrap_or_default();
            (rows, runs)
        }
        Err(_) => (Vec::new(), BTreeMap::new()),
    };

    let state = read_state(config_dir);
    let live_rows: Vec<Map<String, Json>> = Vec::new();

    let mut owned_ids: BTreeMap<String, Option<BTreeSet<String>>> = BTreeMap::new();
    for name in PLATFORM_ORDER {
        if !platform_has_records(name, &state, &all_rows, &live_rows) {
            continue;
        }
        owned_ids.insert(
            name.to_string(),
            owned_platform_ids(config_dir, name, &state),
        );
    }

    let mut statuses: BTreeMap<String, Json> = BTreeMap::new();
    for name in PLATFORM_ORDER {
        statuses.insert(name.to_string(), collection_status(name, &runs));
    }

    build_outcome_report(&ReportRequest {
        state: &state,
        snapshots: &all_rows,
        live_rows: &live_rows,
        owned_ids: &owned_ids,
        collection_status: &statuses,
        now,
    })
}

/// `_handle_analytics()` with `live=false`.
///
/// `platform` filters the raw `posts` array and the `platforms` aggregate; the
/// outcome report always covers every platform in [`PLATFORM_ORDER`], because
/// that is the document the UI renders.
pub fn payload(config_dir: &Path, filter: PlatformFilter<'_>) -> Result<Value, AnalyticsError> {
    let db = config_dir.join("analytics.db");
    let store = xpst_store::Store::open(&db).map_err(AnalyticsError::store)?;

    let latest_rows = match filter {
        PlatformFilter::Unmatchable => Vec::new(),
        PlatformFilter::All => store
            .latest(None)
            .map_err(AnalyticsError::store)?
            .iter()
            .map(row_to_dict)
            .collect::<Vec<_>>(),
        PlatformFilter::Named(name) => store
            .latest(Some(name))
            .map_err(AnalyticsError::store)?
            .iter()
            .map(row_to_dict)
            .collect::<Vec<_>>(),
    };
    let snapshot_count = store.snapshot_count().map_err(AnalyticsError::store)?;

    // `outcome_report()` reads the WHOLE ledger, not the filtered view.
    let report = outcome_report(config_dir, jiff::Timestamp::now());

    // `per_platform` from the (possibly filtered) `latest` rows.
    let mut per_platform = xpst_config::Mapping::new();
    for row in &latest_rows {
        let name = python_str_or_empty(row.get("platform"));
        let entry = match per_platform.get(&name) {
            Some(Value::Mapping(map)) => map.clone(),
            _ => {
                let mut map = xpst_config::Mapping::new();
                map.insert(Value::String("posts".into()), integer(0));
                for key in ["views", "likes", "comments", "shares"] {
                    map.insert(Value::String(key.into()), null());
                }
                map
            }
        };
        let mut entry = entry;
        let posts = entry
            .get("posts")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        entry.insert(Value::String("posts".into()), integer(posts + 1));
        for key in ["views", "likes", "comments", "shares"] {
            if let Some(value) = coerce_metric(row.get(key)) {
                let current = entry.get(key).and_then(Value::as_i64).unwrap_or(0);
                entry.insert(Value::String(key.into()), integer(current + value));
            }
        }
        per_platform.insert(Value::String(name), Value::Mapping(entry));
    }

    // The report's per-platform status, merged onto the aggregate (issue #297).
    let report_platforms = match report.get("platforms") {
        Some(Value::Mapping(map)) => map.clone(),
        _ => xpst_config::Mapping::new(),
    };
    let names: Vec<String> = per_platform
        .iter()
        .filter_map(|(key, _)| key.as_str().map(str::to_string))
        .collect();
    for name in names {
        let entry = match report_platforms.get(&name) {
            Some(Value::Mapping(map)) => map.clone(),
            _ => xpst_config::Mapping::new(),
        };
        let recorded_error = entry
            .get("recorded_collection_error")
            .and_then(Value::as_str)
            .map(str::to_string);
        let mut aggregate = match per_platform.get(&name) {
            Some(Value::Mapping(map)) => map.clone(),
            _ => xpst_config::Mapping::new(),
        };
        aggregate.insert(
            Value::String("collection_status".into()),
            string(
                entry
                    .get("collection_status")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown"),
            ),
        );
        aggregate.insert(
            Value::String("collection_status_label".into()),
            entry
                .get("collection_status_label")
                .cloned()
                .unwrap_or_else(null),
        );
        aggregate.insert(
            Value::String("collection_status_source".into()),
            entry
                .get("collection_status_source")
                .cloned()
                .unwrap_or_else(null),
        );
        // `entry.get("collection_error") or ({error: recorded} if recorded else None)`
        let collection_error = match entry.get("collection_error") {
            Some(value) if truthy_cfg(Some(value)) => value.clone(),
            _ => match &recorded_error {
                Some(message) => {
                    let mut map = xpst_config::Mapping::new();
                    map.insert(Value::String("error".into()), string(message.clone()));
                    Value::Mapping(map)
                }
                None => null(),
            },
        };
        aggregate.insert(Value::String("collection_error".into()), collection_error);
        aggregate.insert(
            Value::String("recorded_collection_error".into()),
            optional_string(recorded_error),
        );
        aggregate.insert(
            Value::String("last_collection_attempt_at".into()),
            entry
                .get("last_collection_attempt_at")
                .cloned()
                .unwrap_or_else(null),
        );
        let mut row_statuses: BTreeSet<String> = BTreeSet::new();
        for row in &latest_rows {
            if python_str_or_empty(row.get("platform")) != name {
                continue;
            }
            let status = match row.get("collection_status") {
                Some(value) if truthy(Some(value)) => python_str(value),
                _ => COLLECTION_STATUS_UNKNOWN.to_string(),
            };
            row_statuses.insert(status);
        }
        if !row_statuses.is_empty() {
            aggregate.insert(
                Value::String("captured_row_statuses".into()),
                array(row_statuses.into_iter().map(string).collect()),
            );
        }
        per_platform.insert(Value::String(name), Value::Mapping(aggregate));
    }

    // `collection_status` — the report's per-platform verdict, in render order.
    let mut collection_statuses = xpst_config::Mapping::new();
    for name in PLATFORM_ORDER {
        let entry = match report_platforms.get(name) {
            Some(Value::Mapping(map)) => map.clone(),
            _ => continue,
        };
        let mut block = xpst_config::Mapping::new();
        block.insert(
            Value::String("status".into()),
            string(
                entry
                    .get("collection_status")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown"),
            ),
        );
        block.insert(
            Value::String("source".into()),
            entry
                .get("collection_status_source")
                .cloned()
                .unwrap_or_else(null),
        );
        let error = match entry.get("recorded_collection_error") {
            Some(value) if truthy_cfg(Some(value)) => value.clone(),
            _ => entry.get("collection_error").cloned().unwrap_or_else(null),
        };
        block.insert(Value::String("error".into()), error);
        block.insert(
            Value::String("last_attempt_at".into()),
            entry
                .get("last_collection_attempt_at")
                .cloned()
                .unwrap_or_else(null),
        );
        collection_statuses.insert(Value::String(name.to_string()), Value::Mapping(block));
    }

    Ok(object(vec![
        ("live", boolean(false)),
        ("snapshot_count", integer(snapshot_count)),
        ("platforms", Value::Mapping(per_platform)),
        ("collection_status", Value::Mapping(collection_statuses)),
        (
            "posts",
            array(
                latest_rows
                    .iter()
                    .map(|row| xpst_config::output::from_json(&Json::Object(row.clone())))
                    .collect(),
            ),
        ),
        ("outcome_report", report),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_matches_the_shipped_conversions() {
        assert_eq!(python_int("5"), Some(5));
        assert_eq!(python_int(" 5 "), Some(5));
        assert_eq!(python_int("+5"), Some(5));
        assert_eq!(python_int("-5"), Some(-5));
        assert_eq!(python_int("1_000"), Some(1000));
        assert_eq!(python_int("1__0"), None);
        assert_eq!(python_int("_1"), None);
        assert_eq!(python_int("1_"), None);
        assert_eq!(python_int("1.5"), None);
        assert_eq!(python_int(""), None);
        assert_eq!(python_int("+"), None);
    }

    #[test]
    fn a_missing_metric_is_not_a_zero() {
        assert_eq!(coerce_metric(None), None);
        assert_eq!(coerce_metric(Some(&Json::Null)), None);
        assert_eq!(coerce_metric(Some(&Json::Bool(true))), None);
        assert_eq!(coerce_metric(Some(&Json::from(0))), Some(0));
        assert_eq!(coerce_metric(Some(&Json::from(5.9))), Some(5));
        assert_eq!(coerce_metric(Some(&Json::from("7"))), Some(7));
        assert_eq!(coerce_metric(Some(&Json::from("x"))), None);
    }

    #[test]
    fn an_unlabelled_status_is_unknown_never_ok() {
        assert_eq!(normalise_collection_status(None), "unknown");
        assert_eq!(normalise_collection_status(Some(&Json::Null)), "unknown");
        assert_eq!(
            normalise_collection_status(Some(&Json::from(""))),
            "unknown"
        );
        assert_eq!(normalise_collection_status(Some(&Json::from("OK"))), "ok");
        assert_eq!(
            normalise_collection_status(Some(&Json::from(" partial "))),
            "partial"
        );
        assert_eq!(
            normalise_collection_status(Some(&Json::from("weird"))),
            "unknown"
        );
    }

    #[test]
    fn the_post_id_falls_back_to_the_legacy_key() {
        let mut info = Map::new();
        info.insert("post_id".into(), Json::from("legacy-1"));
        assert_eq!(resolve_platform_post_id(Some(&info)), "legacy-1");
        info.insert("id".into(), Json::from("canonical-1"));
        assert_eq!(resolve_platform_post_id(Some(&info)), "canonical-1");
        info.insert("id".into(), Json::from(""));
        assert_eq!(resolve_platform_post_id(Some(&info)), "legacy-1");
        assert_eq!(resolve_platform_post_id(None), "");
    }

    #[test]
    fn quotes_and_bookmarks_never_reach_metrics() {
        // X declares them as capability metrics, but `_metrics_from_row` only
        // walks the canonical families.
        let mut row = Map::new();
        row.insert("views".into(), Json::from(10));
        row.insert("quotes".into(), Json::from(3));
        row.insert("bookmarks".into(), Json::from(7));
        row.insert("saves".into(), Json::from(2));
        assert_eq!(metrics_from_row(&row, "x"), vec![("views", 10)]);
    }

    #[test]
    fn the_metric_capability_table_is_the_shipped_one() {
        assert_eq!(
            platform_metric_capabilities("youtube"),
            &["views", "likes", "comments"]
        );
        assert_eq!(platform_metric_capabilities("nope"), &[] as &[&str]);
    }

    fn snapshot(platform: &str, post_id: &str, views: i64, status: &str) -> Map<String, Json> {
        let mut row = Map::new();
        row.insert("platform".into(), Json::from(platform));
        row.insert("post_id".into(), Json::from(post_id));
        row.insert(
            "captured_at".into(),
            Json::from("2026-11-20T00:00:00+00:00"),
        );
        row.insert("views".into(), Json::from(views));
        row.insert("collection_status".into(), Json::from(status));
        row
    }

    fn report(
        state: &Json,
        snapshots: &[Map<String, Json>],
        owned: &BTreeMap<String, Option<BTreeSet<String>>>,
        statuses: &BTreeMap<String, Json>,
    ) -> Value {
        build_outcome_report(&ReportRequest {
            state,
            snapshots,
            live_rows: &[],
            owned_ids: owned,
            collection_status: statuses,
            now: jiff::Timestamp::from_second(1_800_000_000).expect("a fixed instant"),
        })
    }

    /// The snapshot-only branch: an owned id with a snapshot but no publish
    /// record. Unreachable from the wire fixture, because the state-backed
    /// ownership set is built FROM the publish records — only YouTube's
    /// channel-playlist check can own an id the state file never recorded.
    #[test]
    fn a_snapshot_only_outcome_is_labelled_snapshot() {
        let snapshots = vec![snapshot("youtube", "yt-9", 10, "ok")];
        let mut owned = BTreeMap::new();
        owned.insert(
            "youtube".to_string(),
            Some(BTreeSet::from(["yt-9".to_string()])),
        );
        let state = Json::Object(Map::new());
        let report = report(&state, &snapshots, &owned, &BTreeMap::new());
        let youtube = report
            .get("platforms")
            .and_then(|p| p.get("youtube"))
            .unwrap();
        let outcomes = youtube
            .get("outcomes")
            .and_then(Value::as_sequence)
            .unwrap();
        assert_eq!(outcomes.len(), 1);
        assert_eq!(
            outcomes[0].get("outcome_source").and_then(Value::as_str),
            Some("snapshot")
        );
        assert_eq!(
            outcomes[0].get("video_id").and_then(Value::as_str),
            Some("yt-9")
        );
        assert_eq!(
            youtube
                .get("totals")
                .and_then(|totals| totals.get("views"))
                .and_then(Value::as_i64),
            Some(10)
        );
    }

    /// A snapshot whose id is NOT in the verified set is dropped — silently.
    /// The shipped loop `continue`s without recording the id, so
    /// `diagnostics.unowned_ids_excluded` only ever names a dropped PUBLISH
    /// record, never a dropped snapshot row.
    #[test]
    fn an_unowned_snapshot_is_dropped_silently() {
        let snapshots = vec![snapshot("youtube", "yt-foreign", 10, "ok")];
        let mut owned = BTreeMap::new();
        owned.insert("youtube".to_string(), Some(BTreeSet::new()));
        let state = Json::Object(Map::new());
        let report = report(&state, &snapshots, &owned, &BTreeMap::new());
        let youtube = report
            .get("platforms")
            .and_then(|p| p.get("youtube"))
            .unwrap();
        assert_eq!(
            youtube
                .get("outcomes")
                .and_then(Value::as_sequence)
                .unwrap()
                .len(),
            0
        );
        assert_eq!(youtube.get("totals"), Some(&Value::Null));
        let excluded = report
            .get("diagnostics")
            .and_then(|d| d.get("unowned_ids_excluded"))
            .and_then(Value::as_sequence)
            .unwrap();
        assert!(
            excluded.is_empty(),
            "a dropped snapshot row is not named in the diagnostics"
        );
    }

    /// A platform whose ownership check failed admits nothing and says so.
    #[test]
    fn an_unverifiable_platform_fails_closed() {
        let mut state = Map::new();
        let mut posted = Map::new();
        let mut video = Map::new();
        let mut posted_to = Map::new();
        posted_to.insert("youtube".into(), serde_json::json!({"id": "yt-1"}));
        video.insert("posted_to".into(), Json::Object(posted_to));
        posted.insert("vid-1".into(), Json::Object(video));
        state.insert("posted_videos".into(), Json::Object(posted));
        let state = Json::Object(state);

        let mut owned = BTreeMap::new();
        owned.insert("youtube".to_string(), None);
        let report = report(&state, &[], &owned, &BTreeMap::new());
        let youtube = report
            .get("platforms")
            .and_then(|p| p.get("youtube"))
            .unwrap();
        assert_eq!(
            youtube.get("ownership_verified").and_then(Value::as_bool),
            Some(false)
        );
        assert_eq!(
            youtube.get("ownership_checked").and_then(Value::as_bool),
            Some(true)
        );
        assert_eq!(
            youtube.get("note").and_then(Value::as_str),
            Some("Ownership could not be verified — no numbers are shown for this platform")
        );
        assert_eq!(
            youtube
                .get("dropped")
                .and_then(|d| d.get("ownership_unverified"))
                .and_then(Value::as_i64),
            Some(1)
        );
    }

    #[test]
    fn a_live_failure_is_a_failure_not_a_zero() {
        let error = serde_json::json!({"failures": 2, "requested": 2, "error": "boom"});
        assert_eq!(
            platform_note(
                false,
                true,
                true,
                0,
                Some(&error),
                None,
                "failed",
                Some("live"),
                None
            )
            .as_deref(),
            Some("Collection failed for 2 post(s): boom.")
        );
        assert_eq!(
            platform_note(
                true,
                true,
                true,
                1,
                Some(&error),
                None,
                "failed",
                Some("live"),
                None
            )
            .as_deref(),
            Some("Collection failed for 2 post(s): boom. Keeping previously recorded numbers.")
        );
    }

    #[test]
    fn a_recorded_failure_keeps_the_numbers_but_says_so() {
        assert_eq!(
            platform_note(
                true,
                true,
                true,
                1,
                None,
                None,
                "failed",
                Some("recorded"),
                Some("quota exceeded")
            )
            .as_deref(),
            Some(
                "Recorded numbers are not current: the last collection attempt failed (quota \
                 exceeded)."
            )
        );
        assert_eq!(
            platform_note(
                false,
                true,
                true,
                0,
                None,
                None,
                "failed",
                Some("recorded"),
                None
            )
            .as_deref(),
            Some("Collection failed on the last attempt: collection error.")
        );
    }

    #[test]
    fn an_unavailable_answer_is_not_a_failure() {
        let unavailable = serde_json::json!({
            "reason": REASON_POST_NOT_FOUND_UPSTREAM,
            "posts": 2,
        });
        assert_eq!(
            platform_note(
                false,
                true,
                true,
                0,
                None,
                Some(&unavailable),
                "unavailable",
                Some("live"),
                None
            )
            .as_deref(),
            Some(
                "2 recorded post(s) no longer exist upstream (post_not_found_upstream) — nothing \
                 to collect, not a collection failure"
            )
        );
        let shape = serde_json::json!({"reason": REASON_TWIKIT_DETAIL_SHAPE});
        assert_eq!(
            platform_note(
                false,
                true,
                true,
                0,
                None,
                Some(&shape),
                "unavailable",
                Some("live"),
                None
            )
            .as_deref(),
            Some(
                "X's TweetDetail response shape is unreadable (twikit_detail_shape) — analytics \
                 unavailable until the parser is updated"
            )
        );
    }

    #[test]
    fn a_live_status_block_outranks_the_persisted_one() {
        let block = serde_json::json!({"status": "ok", "source": "live"});
        let (status, source, error, at) = derive_collection_status(Some(&block), None, None);
        assert_eq!(status, "ok");
        assert_eq!(source.as_deref(), Some("live"));
        assert_eq!(error, None);
        assert_eq!(at, None);

        // No source and a live failure: the source is `live`.
        let block = serde_json::json!({"status": "failed"});
        let failure = serde_json::json!({"failures": 1});
        let (status, source, _, _) = derive_collection_status(Some(&block), Some(&failure), None);
        assert_eq!(status, "failed");
        assert_eq!(source.as_deref(), Some("live"));

        // An unlabelled platform stays unknown with no source.
        let block = serde_json::json!({"status": "unknown"});
        let (status, source, _, _) = derive_collection_status(Some(&block), None, None);
        assert_eq!(status, "unknown");
        assert_eq!(source, None);
    }

    #[test]
    fn youtube_ownership_fails_closed_with_and_without_a_token() {
        let dir = std::env::temp_dir().join("xpst-analytics-youtube-ownership");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch dir");
        assert_eq!(youtube_owned_ids(&dir), None, "no token");
        std::fs::create_dir_all(dir.join("credentials")).expect("credentials dir");
        std::fs::write(dir.join("credentials").join("youtube_token.json"), "{}").expect("token");
        assert_eq!(
            youtube_owned_ids(&dir),
            None,
            "a token still needs the network check"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn freshness_is_unknown_for_a_missing_or_unparseable_stamp() {
        let now = jiff::Timestamp::from_second(1_800_000_000).expect("a fixed instant");
        assert_eq!(freshness(None, now), (None, None, "unknown"));
        assert_eq!(freshness(Some("not-a-time"), now), (None, None, "unknown"));
        let (last, hours, staleness) = freshness(Some("2026-11-20T00:00:00+00:00"), now);
        assert_eq!(last.as_deref(), Some("2026-11-20T00:00:00+00:00"));
        assert!(hours.unwrap() > 24.0);
        assert_eq!(staleness, "stale");
    }
}
