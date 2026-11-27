//! The dashboard read payloads: `/api/summary`, `/state`, `/api/videos`,
//! `/api/videos/{video_id}` and `/api/activity`.
//!
//! Parity target: `AnalyticsReadModel` in `src/xpst/dashboard/analytics.py`
//! (`get_summary_stats`, `get_video_lineup`, `_match_state_video`,
//! `get_engagement_from_snapshots`, `_freshness`) and the `/api/activity`
//! handler in `src/xpst/dashboard/api.py`.
//!
//! Every document is built in Python's insertion order with Python's scalar
//! semantics, so the HTTP surface can serve it byte for byte:
//!
//! * **A missing metric is not a zero.** `engagement_by_platform` starts every
//!   metric at `null` and only adds a value a snapshot actually reported, and
//!   `platform_totals` keeps `SUM`'s NULL for a metric no latest snapshot
//!   carried (the hidden-counts rule).
//! * **`best_platform` is `null`, not a placeholder.** Nothing posted means no
//!   best platform; the fallback to the highest post count only runs when at
//!   least one platform has a post, and ties resolve to the first platform in
//!   `PLATFORM_ORDER` exactly as Python's `max()` does.
//! * **The lineup never drops a tracked post.** A state record with no snapshot
//!   is appended with `collection_status: "not_collected"` and null metrics, so
//!   a locally tracked video cannot disappear from the Library.
//! * **`last_captured` is the newest capture as an instant, re-rendered in
//!   UTC.** `AnalyticsStore.last_captured_at` parses every stored stamp before
//!   choosing the newest, so a lexicographic `MAX()` would pick the wrong row
//!   when offsets differ.
//!
//! Recorded divergences (see `crates/xpst-api/src/lib.rs`):
//!
//! * `/api/activity` reads `state.json` without `StateStore`'s side effects.
//!   Python's `StateStore(config_dir)` creates the config dir, a `backups/`
//!   directory and a `.state.lock` file on construction; this build only reads.
//! * A `state.json` whose root is not an object is treated as the empty
//!   document. Python's `load_state` returns whatever `json.load` produced and
//!   the handler then raises `AttributeError` (a 500).
//! * A `last_attempt` that is not a string sorts by its `str()` form; Python's
//!   tuple comparison raises `TypeError` when a number meets a string.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value as Json};

use crate::analytics::{
    freshness, normalise_collection_status, parse_utc_timestamp, python_str, python_str_or_empty,
    resolve_platform_post_id, row_to_dict, truthy, utc_isoformat,
};
use crate::isotime::fromisoformat;

/// The render order `get_summary_stats` uses for `platform_counts` and
/// `engagement_by_platform`.
///
/// Deliberately NOT `PLATFORM_ORDER` (youtube, x, instagram, …): the summary
/// declares its own dict literal as youtube, instagram, x, tiktok, threads, and
/// the key order is part of the wire body.
const SUMMARY_PLATFORMS: [&str; 5] = ["youtube", "instagram", "x", "tiktok", "threads"];

/// `_parse_ts()`: an aware UTC instant, or `None` for a missing/unparseable
/// stamp. Naive legacy timestamps are read as UTC.
fn parse_ts(value: Option<&Json>) -> Option<jiff::Timestamp> {
    let value = value?;
    if value.is_null() {
        return None;
    }
    let text = python_str(value);
    if text.is_empty() {
        return None;
    }
    parse_utc_timestamp(&text)
}

/// `load_state()`: the raw `state.json`, or Python's empty default.
///
/// The default is the one `load_state` returns for a missing or corrupt file:
/// `{"posted_videos": {}, "health": {"platforms": {}, "total_processed": 0}}`.
fn load_state(config_dir: &Path) -> Json {
    let path = config_dir.join("state.json");
    if !path.exists() {
        return default_state();
    }
    match std::fs::read_to_string(&path) {
        Ok(text) => match serde_json::from_str::<Json>(&text) {
            Ok(value @ Json::Object(_)) => value,
            _ => default_state(),
        },
        Err(_) => default_state(),
    }
}

fn default_state() -> Json {
    serde_json::json!({
        "posted_videos": {},
        "health": {"platforms": {}, "total_processed": 0},
    })
}

fn posted_videos(state: &Json) -> Map<String, Json> {
    state
        .get("posted_videos")
        .and_then(Json::as_object)
        .cloned()
        .unwrap_or_default()
}

fn health_block(state: &Json) -> Map<String, Json> {
    state
        .get("health")
        .and_then(Json::as_object)
        .cloned()
        .unwrap_or_default()
}

/// `_relative_time()`: `"2h ago"` style, `"—"` when there is no stamp.
fn relative_time(value: Option<&str>, now: jiff::Timestamp) -> String {
    let Some(text) = value else {
        return "—".to_string();
    };
    if text.is_empty() {
        return "—".to_string();
    }
    let Some(instant) = parse_utc_timestamp(text) else {
        return text.chars().take(10).collect();
    };
    let seconds = (now.as_nanosecond() - instant.as_nanosecond()) as f64 / 1_000_000_000.0;
    if seconds < 60.0 {
        "just now".to_string()
    } else if seconds < 3600.0 {
        format!("{}m ago", (seconds / 60.0) as i64)
    } else if seconds < 86_400.0 {
        format!("{}h ago", (seconds / 3600.0) as i64)
    } else {
        format!("{}d ago", (seconds / 86_400.0) as i64)
    }
}

/// `_accumulate()`: add a possibly-missing metric into a rollup total.
///
/// `None` means "no snapshot reported this metric", so the total stays `None`
/// until at least one real value is added; a reported zero sums normally.
fn accumulate(total: Option<i64>, value: Option<i64>) -> Option<i64> {
    match value {
        None => total,
        Some(value) => Some(total.unwrap_or(0) + value),
    }
}

/// `AnalyticsStore.last_captured_at()`: the newest capture, re-rendered in UTC.
///
/// A store that cannot be opened is "no snapshots" — Python's `_freshness`
/// catches the failure and reports `unknown` rather than failing the page.
fn last_captured_at(config_dir: &Path) -> Option<String> {
    let store = xpst_store::Store::open(config_dir.join("analytics.db")).ok()?;
    let values = store.captured_at_values(None).ok()?;
    let mut newest: Option<jiff::Timestamp> = None;
    for value in values {
        let Some(instant) = parse_utc_timestamp(&value) else {
            continue;
        };
        if newest.is_none_or(|current| instant > current) {
            newest = Some(instant);
        }
    }
    newest.map(utc_isoformat)
}

/// One platform's engagement rollup (`get_engagement_from_snapshots`).
#[derive(Debug, Clone, Default)]
struct Engagement {
    posts: i64,
    views: Option<i64>,
    likes: Option<i64>,
    comments: Option<i64>,
    shares: Option<i64>,
}

/// `get_engagement_from_snapshots()`: post counts from state, metrics from the
/// persisted snapshots only (no network).
fn engagement_from_snapshots(
    config_dir: &Path,
    posted: &Map<String, Json>,
) -> Vec<(&'static str, Engagement)> {
    let mut engagement: Vec<(&'static str, Engagement)> = SUMMARY_PLATFORMS
        .iter()
        .map(|platform| (*platform, Engagement::default()))
        .collect();

    for video_data in posted.values() {
        let Some(posted_to) = video_data.get("posted_to").and_then(Json::as_object) else {
            continue;
        };
        for platform in posted_to.keys() {
            if let Some((_, entry)) = engagement.iter_mut().find(|(name, _)| name == platform) {
                entry.posts += 1;
            }
        }
    }

    if let Ok(store) = xpst_store::Store::open(config_dir.join("analytics.db")) {
        if let Ok(totals) = store.platform_totals() {
            for aggregate in totals {
                let Some((_, entry)) = engagement
                    .iter_mut()
                    .find(|(name, _)| *name == aggregate.platform)
                else {
                    continue;
                };
                entry.views = accumulate(entry.views, aggregate.views);
                entry.likes = accumulate(entry.likes, aggregate.likes);
                entry.comments = accumulate(entry.comments, aggregate.comments);
                entry.shares = accumulate(entry.shares, aggregate.shares);
            }
        }
    }

    engagement
}

/// `GET /api/summary` and `GET /state` — the same document, as in Python.
///
/// Infallible on purpose: every read it makes is already defensive in Python
/// (a missing `state.json` is the empty default, an unreadable ledger is "no
/// snapshots"), so a failure here would be a divergence, not a 500.
pub fn summary(config_dir: &Path, now: jiff::Timestamp) -> Json {
    let state = load_state(config_dir);
    let posted = posted_videos(&state);
    let health = health_block(&state);

    let mut platform_counts: Vec<(&'static str, i64)> = SUMMARY_PLATFORMS
        .iter()
        .map(|platform| (*platform, 0))
        .collect();
    let mut total_platform_posts = 0_i64;
    for video_data in posted.values() {
        let Some(posted_to) = video_data.get("posted_to").and_then(Json::as_object) else {
            continue;
        };
        for platform in posted_to.keys() {
            if let Some((_, count)) = platform_counts
                .iter_mut()
                .find(|(name, _)| name == platform)
            {
                *count += 1;
                total_platform_posts += 1;
            }
        }
    }

    // Posts this week: `downloaded_at` at or after `now - 7 days`.
    let week_ns: i128 = 7 * 24 * 60 * 60 * 1_000_000_000;
    let mut posts_this_week = 0_i64;
    for video_data in posted.values() {
        let Some(instant) = parse_ts(video_data.get("downloaded_at")) else {
            continue;
        };
        if now.as_nanosecond() - instant.as_nanosecond() <= week_ns {
            posts_this_week += 1;
        }
    }

    let last_captured = last_captured_at(config_dir);
    let (fresh_last, fresh_hours, staleness) = freshness(last_captured.as_deref(), now);
    let last_captured_relative = relative_time(last_captured.as_deref(), now);

    let engagement = engagement_from_snapshots(config_dir, &posted);

    // Best platform by engagement (views + likes + comments + shares), then by
    // post count when nothing has metrics. Ties keep the first in render order.
    let mut best_platform: Option<&'static str> = None;
    let mut max_engagement = 0_i64;
    for (platform, entry) in &engagement {
        if entry.posts > 0 {
            let total = entry.views.unwrap_or(0)
                + entry.likes.unwrap_or(0)
                + entry.comments.unwrap_or(0)
                + entry.shares.unwrap_or(0);
            if total > max_engagement {
                max_engagement = total;
                best_platform = Some(platform);
            }
        }
    }
    if best_platform.is_none() && platform_counts.iter().any(|(_, count)| *count > 0) {
        let mut best_count = -1_i64;
        for (platform, count) in &platform_counts {
            if *count > best_count {
                best_count = *count;
                best_platform = Some(platform);
            }
        }
    }

    let mut counts = Map::new();
    for (platform, count) in &platform_counts {
        counts.insert((*platform).to_string(), Json::from(*count));
    }
    let mut engagement_out = Map::new();
    for (platform, entry) in &engagement {
        let mut row = Map::new();
        row.insert("posts".into(), Json::from(entry.posts));
        row.insert("views".into(), optional_integer(entry.views));
        row.insert("likes".into(), optional_integer(entry.likes));
        row.insert("comments".into(), optional_integer(entry.comments));
        row.insert("shares".into(), optional_integer(entry.shares));
        engagement_out.insert((*platform).to_string(), Json::Object(row));
    }

    let mut out = Map::new();
    out.insert("total_posts".into(), Json::from(posted.len() as i64));
    out.insert(
        "total_processed".into(),
        health
            .get("total_processed")
            .cloned()
            .unwrap_or(Json::from(0)),
    );
    out.insert("platform_counts".into(), Json::Object(counts));
    out.insert(
        "platform_health".into(),
        health
            .get("platforms")
            .cloned()
            .unwrap_or_else(|| Json::Object(Map::new())),
    );
    out.insert(
        "last_check".into(),
        health.get("last_check").cloned().unwrap_or(Json::Null),
    );
    out.insert("posts_this_week".into(), Json::from(posts_this_week));
    out.insert(
        "best_platform".into(),
        best_platform.map_or(Json::Null, Json::from),
    );
    out.insert(
        "total_platform_posts".into(),
        Json::from(total_platform_posts),
    );
    out.insert(
        "engagement_by_platform".into(),
        Json::Object(engagement_out),
    );
    out.insert(
        "last_captured".into(),
        fresh_last.map_or(Json::Null, Json::from),
    );
    out.insert(
        "staleness_hours".into(),
        fresh_hours.map_or(Json::Null, |hours| {
            serde_json::Number::from_f64(hours).map_or(Json::Null, Json::Number)
        }),
    );
    out.insert("staleness".into(), Json::from(staleness));
    out.insert("freshness".into(), Json::from(staleness));
    out.insert(
        "last_captured_relative".into(),
        Json::from(last_captured_relative),
    );
    Json::Object(out)
}

fn optional_integer(value: Option<i64>) -> Json {
    value.map_or(Json::Null, Json::from)
}

// ── The video lineup ────────────────────────────────────────────────────────

/// `AnalyticsStore.post_links()`: the links derivable from a platform post id.
///
/// YouTube is the only provider with a reliable embed and thumbnail URL, so the
/// others return empty strings rather than a guessed one.
fn post_links(platform: &str, post_id: &str) -> (String, String, String) {
    let pid = post_id.trim();
    if pid.is_empty() {
        return (String::new(), String::new(), String::new());
    }
    match platform.to_lowercase().as_str() {
        "youtube" => (
            format!("https://www.youtube.com/watch?v={pid}"),
            format!("https://www.youtube.com/embed/{pid}"),
            format!("https://i.ytimg.com/vi/{pid}/hqdefault.jpg"),
        ),
        "x" => (
            format!("https://x.com/i/status/{pid}"),
            String::new(),
            String::new(),
        ),
        "instagram" => (
            format!("https://www.instagram.com/p/{pid}/"),
            String::new(),
            String::new(),
        ),
        "tiktok" => (
            format!("https://www.tiktok.com/video/{pid}"),
            String::new(),
            String::new(),
        ),
        "threads" => (
            format!("https://www.threads.net/post/{pid}"),
            String::new(),
            String::new(),
        ),
        _ => (String::new(), String::new(), String::new()),
    }
}

/// The local state record that owns a `(platform, post_id)` pair.
struct StateVideo {
    video_id: String,
    caption: String,
    video_path: String,
    thumbnail: String,
    status: &'static str,
    downloaded_at: String,
}

/// `_find_local_download()`: the first playable local file for a video id.
fn find_local_download(config_dir: &Path, video_id: &str) -> Option<PathBuf> {
    let downloads = config_dir.join("downloads");
    if !downloads.is_dir() {
        return None;
    }
    for extension in [".mp4", ".mov", ".m4v", ".webm", ".mkv"] {
        let candidate = downloads.join(format!("{video_id}{extension}"));
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// `_match_state_video()`: the state record owning `(platform, post_id)`.
fn match_state_video(
    config_dir: &Path,
    state: &Json,
    platform: &str,
    post_id: &str,
) -> Option<StateVideo> {
    let posted = posted_videos(state);
    for (video_id, data) in &posted {
        let Some(data) = data.as_object() else {
            continue;
        };
        let Some(posted_to) = data.get("posted_to").and_then(Json::as_object) else {
            continue;
        };
        for (candidate_platform, info) in posted_to {
            if candidate_platform.to_lowercase() != platform {
                continue;
            }
            let info_object = info.as_object();
            let candidate = resolve_platform_post_id(info_object);
            if candidate.is_empty() || candidate != post_id {
                continue;
            }
            let local_raw = {
                let explicit = data.get("local_path").filter(|value| truthy(Some(value)));
                match explicit {
                    Some(value) => python_str(value),
                    None => python_str_or_empty(data.get("video_path")),
                }
            };
            let video_path = if !local_raw.is_empty() {
                local_raw
            } else {
                find_local_download(config_dir, video_id)
                    .map(|path| path.display().to_string())
                    .unwrap_or_default()
            };
            let downloaded_at = {
                let explicit = data
                    .get("downloaded_at")
                    .filter(|value| truthy(Some(value)));
                match explicit {
                    Some(value) => python_str(value),
                    None => python_str_or_empty(info_object.and_then(|info| info.get("timestamp"))),
                }
            };
            return Some(StateVideo {
                video_id: video_id.clone(),
                caption: python_str_or_empty(data.get("caption")),
                video_path,
                thumbnail: python_str_or_empty(data.get("thumbnail")),
                status: "posted",
                downloaded_at,
            });
        }
    }
    None
}

/// `get_video_lineup()`: every tracked platform post, newest capture first.
fn video_lineup(config_dir: &Path, store: Option<&xpst_store::Store>) -> Vec<Json> {
    let state = load_state(config_dir);
    // Python's `get_video_lineup` wraps the snapshot read in `try/except` and
    // serves the state-only rows when the ledger is unreadable.
    let rows = store
        .and_then(|store| store.latest(None).ok())
        .unwrap_or_default();

    let mut lineup: Vec<Json> = Vec::new();
    let mut seen: BTreeSet<(String, String)> = BTreeSet::new();

    for row in &rows {
        let row = row_to_dict(row);
        let platform = python_str_or_empty(row.get("platform")).to_lowercase();
        let post_id = python_str_or_empty(row.get("post_id"));
        if platform.is_empty() || post_id.is_empty() {
            continue;
        }
        seen.insert((platform.clone(), post_id.clone()));

        let mut entry = Map::new();
        for key in [
            "platform",
            "post_id",
            "captured_at",
            "views",
            "likes",
            "comments",
            "shares",
            "reposts",
            "saves",
            "collection_status",
            "collection_error",
        ] {
            entry.insert(key.to_string(), row.get(key).cloned().unwrap_or(Json::Null));
        }
        let (url, embed_url, thumbnail_url) = post_links(&platform, &post_id);
        entry.insert("url".into(), Json::from(url));
        entry.insert("embed_url".into(), Json::from(embed_url));
        entry.insert("thumbnail_url".into(), Json::from(thumbnail_url));

        match match_state_video(config_dir, &state, &platform, &post_id) {
            Some(video) => {
                entry.insert("video_id".into(), Json::from(video.video_id));
                entry.insert("caption".into(), Json::from(video.caption));
                entry.insert("video_path".into(), Json::from(video.video_path));
                entry.insert("thumbnail".into(), Json::from(video.thumbnail));
                entry.insert("status".into(), Json::from(video.status));
                entry.insert("downloaded_at".into(), Json::from(video.downloaded_at));
            }
            None => {
                let thumbnail = python_str_or_empty(entry.get("thumbnail_url"));
                let downloaded_at = python_str_or_empty(entry.get("captured_at"));
                entry.insert("video_id".into(), Json::from(post_id.clone()));
                entry.insert("caption".into(), Json::from(""));
                entry.insert("video_path".into(), Json::from(""));
                entry.insert("thumbnail".into(), Json::from(thumbnail));
                entry.insert("status".into(), Json::from("tracked"));
                entry.insert("downloaded_at".into(), Json::from(downloaded_at));
            }
        }
        lineup.push(Json::Object(entry));
    }

    // State-only videos: tracked locally, no snapshot yet. They stay visible as
    // pending/source items with null metrics and an explicit `not_collected`.
    let posted = posted_videos(&state);
    for (video_id, data) in &posted {
        let Some(data) = data.as_object() else {
            continue;
        };
        let Some(posted_to) = data.get("posted_to").and_then(Json::as_object) else {
            continue;
        };
        for (platform, info) in posted_to {
            let plat = platform.to_lowercase();
            let resolved = resolve_platform_post_id(info.as_object());
            let pid = if resolved.is_empty() {
                video_id.clone()
            } else {
                resolved
            };
            if seen.contains(&(plat.clone(), pid.clone())) {
                continue;
            }
            seen.insert((plat.clone(), pid.clone()));

            let timestamp = {
                let explicit = info
                    .as_object()
                    .and_then(|info| info.get("timestamp"))
                    .filter(|value| truthy(Some(value)));
                match explicit {
                    Some(value) => python_str(value),
                    None => python_str_or_empty(data.get("downloaded_at")),
                }
            };

            let mut entry = Map::new();
            entry.insert("platform".into(), Json::from(plat.clone()));
            entry.insert("post_id".into(), Json::from(pid.clone()));
            entry.insert("video_id".into(), Json::from(video_id.clone()));
            entry.insert(
                "caption".into(),
                Json::from(python_str_or_empty(data.get("caption"))),
            );
            entry.insert("video_path".into(), Json::from(""));
            entry.insert(
                "thumbnail".into(),
                Json::from(python_str_or_empty(data.get("thumbnail"))),
            );
            entry.insert("status".into(), Json::from("posted"));
            entry.insert("captured_at".into(), Json::from(timestamp.clone()));
            entry.insert("downloaded_at".into(), Json::from(timestamp));
            for key in ["views", "likes", "comments", "shares", "reposts", "saves"] {
                entry.insert(key.to_string(), Json::Null);
            }
            entry.insert("collection_status".into(), Json::from("not_collected"));
            entry.insert("collection_error".into(), Json::Null);
            let (url, embed_url, thumbnail_url) = post_links(&plat, &pid);
            entry.insert("url".into(), Json::from(url));
            entry.insert("embed_url".into(), Json::from(embed_url));
            entry.insert("thumbnail_url".into(), Json::from(thumbnail_url));
            lineup.push(Json::Object(entry));
        }
    }

    // `sort(key=captured_at, reverse=True)` is stable: ties keep their order.
    lineup.sort_by(|left, right| {
        let key = |value: &Json| python_str_or_empty(value.get("captured_at"));
        key(right).cmp(&key(left))
    });
    lineup
}

/// `platform_totals()` as the wire document: one row per platform, in the
/// column order Python's `dict(row)` renders.
fn platform_totals_json(rows: &[xpst_store::PlatformTotals]) -> Json {
    let mut out = Map::new();
    for row in rows {
        let mut entry = Map::new();
        entry.insert("platform".into(), Json::from(row.platform.clone()));
        entry.insert("views".into(), optional_integer(row.views));
        entry.insert("likes".into(), optional_integer(row.likes));
        entry.insert("comments".into(), optional_integer(row.comments));
        entry.insert("shares".into(), optional_integer(row.shares));
        entry.insert("reposts".into(), optional_integer(row.reposts));
        entry.insert("saves".into(), optional_integer(row.saves));
        out.insert(row.platform.clone(), Json::Object(entry));
    }
    Json::Object(out)
}

/// `latest_collection_runs()` as the wire document.
fn collection_runs_json(
    runs: &std::collections::BTreeMap<String, xpst_store::CollectionRunRow>,
) -> Json {
    let mut out = Map::new();
    for (platform, run) in runs {
        let mut entry = Map::new();
        entry.insert("platform".into(), Json::from(run.platform.clone()));
        entry.insert("captured_at".into(), Json::from(run.captured_at.clone()));
        entry.insert("status".into(), Json::from(run.status.as_str()));
        entry.insert(
            "error".into(),
            run.error.clone().map_or(Json::Null, Json::from),
        );
        entry.insert(
            "reason".into(),
            run.reason.clone().map_or(Json::Null, Json::from),
        );
        entry.insert(
            "detail".into(),
            run.detail.clone().map_or(Json::Null, Json::from),
        );
        entry.insert("attempted".into(), optional_integer(run.attempted));
        entry.insert("failures".into(), optional_integer(run.failures));
        entry.insert("requested".into(), optional_integer(run.requested));
        entry.insert("collected".into(), optional_integer(run.collected));
        out.insert(platform.clone(), Json::Object(entry));
    }
    Json::Object(out)
}

/// `GET /api/videos` — the lineup plus the per-platform totals and the
/// collection outcome behind them.
pub fn videos(config_dir: &Path) -> Json {
    let store = xpst_store::Store::open(config_dir.join("analytics.db")).ok();
    let lineup = video_lineup(config_dir, store.as_ref());
    // Python wraps both reads in `try/except` and serves `{}` on failure: a
    // broken aggregate must not hide the lineup.
    let totals = store
        .as_ref()
        .and_then(|store| store.platform_totals().ok())
        .map(|rows| platform_totals_json(&rows))
        .unwrap_or_else(|| Json::Object(Map::new()));
    let collection_status = store
        .as_ref()
        .and_then(|store| store.latest_collection_runs().ok())
        .map(|runs| collection_runs_json(&runs))
        .unwrap_or_else(|| Json::Object(Map::new()));
    let count = lineup.len();
    serde_json::json!({
        "videos": lineup,
        "count": count,
        "platform_totals": totals,
        "collection_status": collection_status,
    })
}

/// `GET /api/videos/{video_id}` — one video's posts and their snapshot series.
///
/// `None` means the id is not in the lineup; the caller answers Python's
/// `404 {"detail": "Unknown video: <id>"}`.
pub fn video_detail(config_dir: &Path, video_id: &str) -> Option<Json> {
    let store = xpst_store::Store::open(config_dir.join("analytics.db")).ok();
    let lineup = video_lineup(config_dir, store.as_ref());
    let posts: Vec<Json> = lineup
        .into_iter()
        .filter(|entry| entry.get("video_id").and_then(Json::as_str) == Some(video_id))
        .collect();
    if posts.is_empty() {
        return None;
    }

    let mut metrics = Map::new();
    for post in &posts {
        let platform = python_str_or_empty(post.get("platform"));
        let post_id = python_str_or_empty(post.get("post_id"));
        if platform.is_empty() || post_id.is_empty() {
            continue;
        }
        let (latest, series) = match store.as_ref().map(|store| {
            (
                store.latest_for_post(&platform, &post_id),
                store.history(&platform, &post_id, 100),
            )
        }) {
            Some((Ok(latest), Ok(series))) => (
                latest.iter().map(row_to_dict).collect::<Vec<_>>(),
                series.iter().map(row_to_dict).collect::<Vec<_>>(),
            ),
            // Python resets BOTH lists when either read raises.
            _ => (Vec::new(), Vec::new()),
        };
        metrics.insert(
            format!("{platform}:{post_id}"),
            serde_json::json!({ "latest": latest, "series": series }),
        );
    }

    let collection_status = store
        .as_ref()
        .and_then(|store| store.latest_collection_runs().ok())
        .map(|runs| collection_runs_json(&runs))
        .unwrap_or_else(|| Json::Object(Map::new()));

    Some(serde_json::json!({
        "video_id": video_id,
        "posts": posts,
        "metrics": metrics,
        "collection_status": collection_status,
    }))
}

/// `GET /api/activity` — recorded failures with truthful recovery metadata.
pub fn activity(config_dir: &Path) -> Json {
    let state = load_state(config_dir);
    let posted = posted_videos(&state);

    let mut failures: Vec<Json> = Vec::new();
    for (video_id, video) in &posted {
        let Some(video) = video.as_object() else {
            continue;
        };
        let Some(errors) = video.get("errors").and_then(Json::as_object) else {
            continue;
        };
        for (platform, result) in errors {
            let Some(result) = result.as_object() else {
                continue;
            };
            let retryable = result.get("retryable").cloned().unwrap_or(Json::Null);
            let error = match result.get("error").filter(|value| truthy(Some(value))) {
                Some(value) => python_str(value),
                None => "Unknown error".to_string(),
            };
            let last_attempt = match result.get("timestamp").filter(|value| truthy(Some(value))) {
                Some(value) => value.clone(),
                None => video.get("last_attempt").cloned().unwrap_or(Json::Null),
            };
            let action = if retryable == Json::Bool(true) {
                "retry"
            } else {
                "review"
            };
            let mut entry = Map::new();
            entry.insert("video_id".into(), Json::from(video_id.clone()));
            entry.insert("platform".into(), Json::from(platform.clone()));
            entry.insert("error".into(), Json::from(error));
            entry.insert("retryable".into(), retryable);
            entry.insert("post_id".into(), Json::Null);
            entry.insert("post_url".into(), Json::Null);
            entry.insert(
                "source_url".into(),
                video.get("source_url").cloned().unwrap_or(Json::Null),
            );
            entry.insert("last_attempt".into(), last_attempt);
            entry.insert("action".into(), Json::from(action));
            failures.push(Json::Object(entry));
        }
    }

    // `sort(key=(last_attempt or "", video_id, platform), reverse=True)`.
    failures.sort_by(|left, right| {
        let key = |value: &Json| {
            (
                python_str_or_empty(value.get("last_attempt")),
                python_str_or_empty(value.get("video_id")),
                python_str_or_empty(value.get("platform")),
            )
        };
        key(right).cmp(&key(left))
    });

    let count = failures.len();
    serde_json::json!({
        "failures": failures,
        "count": count,
    })
}

/// `normalise_collection_status` re-exported for the HTTP layer's tests.
#[must_use]
pub fn normalised_status(value: Option<&Json>) -> &'static str {
    normalise_collection_status(value)
}

/// `datetime.fromisoformat` re-exported so a caller can prove the parser is the
/// one the rest of the surface uses.
#[must_use]
pub fn parse_isoformat(raw: &str) -> Option<String> {
    fromisoformat(raw).ok().map(|stamp| stamp.isoformat())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn instant(text: &str) -> jiff::Timestamp {
        parse_utc_timestamp(text).expect("a parseable stamp")
    }

    #[test]
    fn a_missing_metric_never_becomes_a_zero() {
        assert_eq!(accumulate(None, None), None);
        assert_eq!(accumulate(Some(5), None), Some(5));
        assert_eq!(accumulate(None, Some(0)), Some(0));
        assert_eq!(accumulate(Some(5), Some(3)), Some(8));
    }

    #[test]
    fn relative_time_is_honest_about_a_missing_or_broken_stamp() {
        let now = instant("2026-11-20T12:00:00+00:00");
        assert_eq!(relative_time(None, now), "—");
        assert_eq!(relative_time(Some(""), now), "—");
        assert_eq!(relative_time(Some("not-a-time"), now), "not-a-time");
        assert_eq!(
            relative_time(Some("2026-11-20T11:59:30+00:00"), now),
            "just now"
        );
        assert_eq!(
            relative_time(Some("2026-11-20T11:30:00+00:00"), now),
            "30m ago"
        );
        assert_eq!(
            relative_time(Some("2026-11-20T00:00:00+00:00"), now),
            "12h ago"
        );
        assert_eq!(
            relative_time(Some("2026-11-18T12:00:00+00:00"), now),
            "2d ago"
        );
    }

    #[test]
    fn a_naive_stamp_is_read_as_utc() {
        let naive = parse_ts(Some(&Json::from("2026-11-20T12:00:00"))).expect("naive parses");
        assert_eq!(naive, instant("2026-11-20T12:00:00+00:00"));
        assert_eq!(parse_ts(Some(&Json::Null)), None);
        assert_eq!(parse_ts(None), None);
    }

    #[test]
    fn post_links_are_derived_only_where_they_are_reliable() {
        assert_eq!(
            post_links("youtube", "fic-yt-1"),
            (
                "https://www.youtube.com/watch?v=fic-yt-1".to_string(),
                "https://www.youtube.com/embed/fic-yt-1".to_string(),
                "https://i.ytimg.com/vi/fic-yt-1/hqdefault.jpg".to_string(),
            )
        );
        assert_eq!(
            post_links("x", "fic-x-1"),
            (
                "https://x.com/i/status/fic-x-1".to_string(),
                String::new(),
                String::new(),
            )
        );
        assert_eq!(
            post_links("nope", "fic-1"),
            (String::new(), String::new(), String::new())
        );
        assert_eq!(
            post_links("youtube", "  "),
            (String::new(), String::new(), String::new())
        );
    }

    /// The newest capture is chosen by INSTANT, not by string order: two rows
    /// can carry different offsets for the same wall clock.
    #[test]
    fn last_captured_at_compares_instants_not_strings() {
        let dir = std::env::temp_dir().join(format!(
            "xpst-dashboard-last-captured-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch dir");
        {
            let store = xpst_store::Store::open(dir.join("analytics.db")).expect("open");
            store
                .record_snapshots(&[
                    // Lexicographically the largest string, but the EARLIER instant.
                    xpst_store::NewSnapshot::new("youtube", "fic-a", "2026-11-20T01:00:00+05:00"),
                    // Lexicographically smaller, but the LATER instant.
                    xpst_store::NewSnapshot::new("youtube", "fic-b", "2026-11-19T21:00:00+00:00"),
                ])
                .expect("record");
        }
        assert_eq!(
            last_captured_at(&dir).as_deref(),
            Some("2026-11-19T21:00:00+00:00")
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_state_file_is_pythons_empty_default() {
        let dir =
            std::env::temp_dir().join(format!("xpst-dashboard-empty-state-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch dir");
        let state = load_state(&dir);
        assert_eq!(state["posted_videos"], serde_json::json!({}));
        assert_eq!(state["health"]["platforms"], serde_json::json!({}));
        assert_eq!(state["health"]["total_processed"], serde_json::json!(0));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
