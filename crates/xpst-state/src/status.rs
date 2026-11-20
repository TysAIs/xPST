//! Python-parity reads behind `xpst status` and `xpst failures list`.
//!
//! The shipped Python engine answers both from [`StateManager`] after
//! [`StateStore`] has loaded, migrated and key-filled the document
//! (`src/xpst/state_manager.py::get_statistics`,
//! `src/xpst/state_store.py::_load`). This module reproduces the *reading* half
//! of that pipeline — including the tolerant corruption fallback and the
//! in-memory v1 migration — and none of the writing half.
//!
//! ## Deliberate difference: nothing is written
//!
//! The Python loader has side effects: it copies a corrupt document to
//! `backups/corrupted_*` plus `state.json.forensic`, migrates a legacy document
//! and rewrites it, and rotates backups. Those are write-path behaviours owned
//! by the state-writing phase; this reader reports the same numbers and leaves
//! the disk alone. The difference is visible only in the filesystem, never in
//! the payload or the exit code, and it is pinned by
//! `crates/xpst-cli/tests/local_surface.rs`.
//!
//! The summary in [`crate::summary`] stays as it is: it is intentionally a
//! *summary* for doctor, not the statistics payload. This module exists because
//! "0 videos" and "a corrupt document read as 0 videos" must be the same
//! reported fact as Python's, and the types there are deliberately stricter.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::model::SCHEMA_VERSION;
use crate::store::STATE_FILE_NAME;

/// Destinations `by_platform` always reports, in the order Python writes them.
pub const DEFAULT_DESTINATIONS: [&str; 5] = ["youtube", "x", "instagram", "tiktok", "threads"];

/// The sentence the Python payload carries so a reader cannot mistake
/// historical post counters for a live readiness check (defect F7).
pub const PLATFORM_HEALTH_NOTE: &str = "platform_health is historical: it counts local \
     post outcomes, not a live check. Run `xpst doctor` (or `xpst health`) for current \
     auth/readiness.";

/// The effective state document for a config directory.
///
/// Resolution order matches `StateStore._load`: `state.json`, then
/// `backups/state_*.json` newest-first, then an empty v2 document. A document
/// that parses but is not an object is treated as unreadable, exactly like
/// Python's `_try_load_file`.
pub fn effective_state(dir: &Path) -> Value {
    let loaded = load_document(&dir.join(STATE_FILE_NAME)).or_else(|| newest_backup(dir));
    let mut state = match loaded {
        Some(value) => value,
        None => empty_state(),
    };
    migrate_and_ensure(&mut state);
    Value::Object(state)
}

/// `StateStore._empty_state()`.
fn empty_state() -> Map<String, Value> {
    let platforms = DEFAULT_DESTINATIONS
        .iter()
        .map(|name| {
            (
                (*name).to_string(),
                json!({"status": "ok", "last_success": null, "failures": 0}),
            )
        })
        .collect::<Map<_, _>>();
    let mut state = Map::new();
    state.insert("version".to_string(), json!(SCHEMA_VERSION));
    state.insert("posted_videos".to_string(), json!({}));
    state.insert("content_hashes".to_string(), json!({}));
    state.insert(
        "health".to_string(),
        json!({
            "platforms": Value::Object(platforms),
            "sources": {},
            "total_processed": 0,
            "last_check": null,
            "last_wake_check": null,
        }),
    );
    state
}

fn load_document(path: &Path) -> Option<Map<String, Value>> {
    let text = std::fs::read_to_string(path).ok()?;
    match serde_json::from_str::<Value>(&text).ok()? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

fn newest_backup(dir: &Path) -> Option<Map<String, Value>> {
    let entries = std::fs::read_dir(dir.join("backups")).ok()?;
    let mut candidates: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            name.starts_with("state_") && name.ends_with(".json")
        })
        .collect();
    // Python: `sorted(glob, reverse=True)` — newest by name, and the names are
    // nanosecond timestamps, so name order is time order.
    candidates.sort();
    candidates.iter().rev().find_map(|path| load_document(path))
}

/// `_load`: migrate when the version is not current, then ensure the keys.
fn migrate_and_ensure(state: &mut Map<String, Value>) {
    let version = state.get("version").and_then(Value::as_u64);
    if version.is_none() || version == Some(1) {
        ensure_state_keys(state);
        migrate_legacy_posted_video_ids(state);
        migrate_legacy_cross_posted(state);
        state.insert("version".to_string(), json!(SCHEMA_VERSION));
    }
    ensure_state_keys(state);
}

fn ensure_state_keys(state: &mut Map<String, Value>) {
    state
        .entry("version".to_string())
        .or_insert_with(|| json!(SCHEMA_VERSION));
    state
        .entry("posted_videos".to_string())
        .or_insert_with(|| json!({}));
    state
        .entry("content_hashes".to_string())
        .or_insert_with(|| json!({}));
    // Python only fills `health.sources` when `health` is already an object,
    // and only creates a whole `health` block when it is absent. A `health`
    // that exists but has no `platforms` key crashes the Python statistics
    // call; this reader fills the default block instead (named deviation).
    let health_missing = !state.get("health").is_some_and(Value::is_object);
    if health_missing {
        state.insert(
            "health".to_string(),
            empty_state().remove("health").unwrap_or_else(|| json!({})),
        );
    }
    if let Some(Value::Object(health)) = state.get_mut("health") {
        health
            .entry("sources".to_string())
            .or_insert_with(|| json!({}));
        health
            .entry("platforms".to_string())
            .or_insert_with(default_platform_health);
        health
            .entry("total_processed".to_string())
            .or_insert_with(|| json!(0));
        health
            .entry("last_check".to_string())
            .or_insert(Value::Null);
        health
            .entry("last_wake_check".to_string())
            .or_insert(Value::Null);
    }
}

fn default_platform_health() -> Value {
    Value::Object(
        DEFAULT_DESTINATIONS
            .iter()
            .map(|name| {
                (
                    (*name).to_string(),
                    json!({"status": "ok", "last_success": null, "failures": 0}),
                )
            })
            .collect(),
    )
}

fn migrate_legacy_posted_video_ids(state: &mut Map<String, Value>) {
    let Some(video_ids) = state
        .get("posted_video_ids")
        .and_then(Value::as_array)
        .cloned()
    else {
        return;
    };
    if !state.contains_key("posted_to") {
        return;
    }
    let posted_to_by_platform = state
        .get("posted_to")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let videos = state
        .entry("posted_videos".to_string())
        .or_insert_with(|| json!({}));
    let Some(videos) = videos.as_object_mut() else {
        return;
    };
    for video_id in video_ids.iter().filter_map(Value::as_str) {
        let mut posted_to = Map::new();
        for (platform, ids) in &posted_to_by_platform {
            let listed = ids.as_array().is_some_and(|ids| {
                ids.iter()
                    .any(|candidate| candidate.as_str() == Some(video_id))
            });
            if listed {
                posted_to.insert(
                    platform.clone(),
                    json!({"id": "", "url": "", "timestamp": ""}),
                );
            }
        }
        videos.insert(
            video_id.to_string(),
            json!({
                "source_url": "",
                "source_platform": "unknown",
                "caption": "",
                "posted_to": Value::Object(posted_to),
                "downloaded_at": "",
                "last_attempt": "",
                "content_hash": null,
            }),
        );
    }
}

fn migrate_legacy_cross_posted(state: &mut Map<String, Value>) {
    let Some(cross_posted) = state.get("cross_posted").and_then(Value::as_object) else {
        return;
    };
    let already_migrated = state
        .get("posted_videos")
        .and_then(Value::as_object)
        .is_some_and(|videos| !videos.is_empty());
    if already_migrated {
        return;
    }
    let mut migrated = Map::new();
    for (composite_key, platforms) in cross_posted {
        let (source_platform, video_id) = match composite_key.split_once(':') {
            Some((source, id)) => (source.to_string(), id.to_string()),
            None => ("unknown".to_string(), composite_key.clone()),
        };
        let mut posted_to = Map::new();
        let mut first_timestamp = String::new();
        if let Some(platforms) = platforms.as_object() {
            for (platform, info) in platforms {
                let id = info.get("post_id").cloned().unwrap_or_else(|| json!(""));
                let url = info.get("url").cloned().unwrap_or_else(|| json!(""));
                let timestamp = info.get("timestamp").cloned().unwrap_or_else(|| json!(""));
                if first_timestamp.is_empty() {
                    first_timestamp = timestamp.as_str().unwrap_or("").to_string();
                }
                posted_to.insert(
                    platform.clone(),
                    json!({"id": id, "url": url, "timestamp": timestamp}),
                );
            }
        }
        migrated.insert(
            video_id,
            json!({
                "source_url": "",
                "source_platform": source_platform,
                "caption": "",
                "posted_to": Value::Object(posted_to),
                "downloaded_at": first_timestamp,
                "last_attempt": "",
                "content_hash": null,
            }),
        );
    }
    state.insert("posted_videos".to_string(), Value::Object(migrated));
    state.remove("cross_posted");
}

/// Number of recorded failures across every video (`len(get_dead_letter_queue())`).
pub fn dead_letter_count(state: &Value) -> usize {
    state
        .get("posted_videos")
        .and_then(Value::as_object)
        .map(|videos| {
            videos
                .values()
                .map(|video| {
                    video
                        .get("errors")
                        .and_then(Value::as_object)
                        .map_or(0, Map::len)
                })
                .sum()
        })
        .unwrap_or(0)
}

/// The `xpst status --json` payload, field for field with Python's
/// `StateManager.get_statistics()`.
pub fn statistics(state: &Value) -> Value {
    let videos = state
        .get("posted_videos")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();

    let mut by_platform = Map::new();
    for name in DEFAULT_DESTINATIONS {
        by_platform.insert(name.to_string(), json!(0));
    }
    let mut cross_posted_count = 0u64;
    for video in videos.values() {
        if let Some(posted_to) = video.get("posted_to").and_then(Value::as_object) {
            for platform in posted_to.keys() {
                if by_platform.contains_key(platform) {
                    let count = by_platform
                        .get(platform)
                        .and_then(Value::as_u64)
                        .unwrap_or(0);
                    by_platform.insert(platform.clone(), json!(count + 1));
                }
                cross_posted_count += 1;
            }
        }
    }

    let health = state.get("health").cloned().unwrap_or_else(|| json!({}));
    let mut platform_health = Map::new();
    if let Some(platforms) = health.get("platforms").and_then(Value::as_object) {
        for (platform, entry) in platforms {
            let status = entry
                .get("status")
                .cloned()
                .unwrap_or_else(|| json!("unknown"));
            let last_success = entry.get("last_success").cloned().unwrap_or(Value::Null);
            let failures = entry.get("failures").cloned().unwrap_or_else(|| json!(0));
            platform_health.insert(
                platform.clone(),
                json!({
                    "status": status,
                    "last_success": last_success,
                    "failures": failures,
                    "historical": true,
                    "source": "state_history",
                }),
            );
        }
    }

    json!({
        "version": state.get("version").cloned().unwrap_or_else(|| json!(1)),
        "total_videos_tracked": videos.len(),
        "total_processed": health.get("total_processed").cloned().unwrap_or_else(|| json!(0)),
        "cross_posted_count": cross_posted_count,
        "by_platform": Value::Object(by_platform),
        "last_check": health.get("last_check").cloned().unwrap_or(Value::Null),
        "last_wake_check": health.get("last_wake_check").cloned().unwrap_or(Value::Null),
        "dead_letter_count": dead_letter_count(state),
        "platform_health": Value::Object(platform_health),
        "platform_health_note": PLATFORM_HEALTH_NOTE,
    })
}

/// The dead-letter queue as `xpst failures list --json` reports it.
///
/// `now` is injected so the payload is testable: Python computes `age_hours`
/// from the wall clock at call time.
pub fn dead_letters(state: &Value, now: jiff::Timestamp) -> Vec<Value> {
    let Some(videos) = state.get("posted_videos").and_then(Value::as_object) else {
        return Vec::new();
    };
    let mut queue = Vec::new();
    for (video_id, video) in videos {
        let Some(errors) = video.get("errors").and_then(Value::as_object) else {
            continue;
        };
        for (platform, error) in errors {
            let status = dlq_status(video, platform);
            queue.push(json!({
                "video_id": video_id,
                "platform": platform,
                "error": error.get("error").cloned().unwrap_or_else(|| json!("Unknown")),
                "timestamp": error.get("timestamp").cloned().unwrap_or(Value::Null),
                "count": error.get("count").cloned().unwrap_or_else(|| json!(1)),
                "source_url": video.get("source_url").cloned().unwrap_or(Value::Null),
                "status": status,
                "resolved": status == "resolved",
                "age_hours": age_hours(error.get("timestamp"), now),
            }));
        }
    }
    queue
}

/// `StateManager._dlq_status`: resolved only when the same video later posted
/// there and that post is not marked deleted.
fn dlq_status(video: &Value, platform: &str) -> &'static str {
    let entry = video
        .get("posted_to")
        .and_then(Value::as_object)
        .and_then(|posted_to| posted_to.get(platform));
    let Some(entry) = entry else {
        return "open";
    };
    if entry.get("deleted").and_then(Value::as_bool) == Some(true) {
        return "open";
    }
    let post_id = entry
        .get(crate::model::CANONICAL_POST_ID_KEY)
        .or_else(|| entry.get(crate::model::LEGACY_POST_ID_KEY))
        .and_then(Value::as_str)
        .unwrap_or("");
    if post_id.is_empty() {
        "open"
    } else {
        "resolved"
    }
}

/// `_age_hours`: hours since the recorded failure, rounded to two decimals.
///
/// `None` — not zero — when the timestamp is missing or unparseable: a
/// fabricated age is worse than an unknown one, which is the rule the Python
/// helper states in its own docstring.
pub fn age_hours(timestamp: Option<&Value>, now: jiff::Timestamp) -> Option<f64> {
    let raw = timestamp?.as_str()?;
    let parsed = parse_instant(raw)?;
    let hours = now.as_second() as f64 + f64::from(now.subsec_nanosecond()) / 1e9
        - (parsed.as_second() as f64 + f64::from(parsed.subsec_nanosecond()) / 1e9);
    let hours = hours / 3600.0;
    if hours < 0.0 {
        return Some(0.0);
    }
    Some((hours * 100.0).round() / 100.0)
}

/// Parse the ISO-8601 shapes the Python engine persists and accepts.
///
/// `datetime.fromisoformat` is more permissive than RFC 3339; this covers the
/// forms xPST writes (naive local/UTC wall clock, or an explicit `+HH:MM`/`Z`
/// offset) and returns `None` for everything else rather than guessing.
pub fn parse_instant(raw: &str) -> Option<jiff::Timestamp> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    if text.ends_with('Z') || text.ends_with('z') {
        return text.parse::<jiff::Timestamp>().ok();
    }
    // An offset appears after the time part; a bare date/time has none.
    if let Some((_, tail)) = text.split_once('T') {
        if tail.contains('+') || tail[1..].contains('-') {
            return text.parse::<jiff::Timestamp>().ok();
        }
        let civil: jiff::civil::DateTime = text.parse().ok()?;
        return civil
            .to_zoned_datetime(jiff::tz::TimeZone::UTC)
            .ok()
            .map(|z| z.timestamp());
    }
    // Date only: Python reads it as midnight.
    let date: jiff::civil::Date = text.parse().ok()?;
    date.at(0, 0, 0, 0)
        .to_zoned_datetime(jiff::tz::TimeZone::UTC)
        .ok()
        .map(|z| z.timestamp())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_state() -> Value {
        json!({
            "version": 2,
            "posted_videos": {
                "v1": {
                    "posted_to": {
                        "youtube": {"id": "a", "deleted": true},
                        "x": {"id": "b"},
                        "bluesky": {"id": "c"}
                    },
                    "errors": {"instagram": {"error": "boom", "timestamp": null, "count": 2}}
                },
                "v2": {"posted_to": {}}
            },
            "health": {
                "total_processed": 5,
                "last_check": "2026-01-02T03:04:05",
                "platforms": {"x": {"status": "error", "failures": 3}}
            }
        })
    }

    #[test]
    fn statistics_matches_the_python_payload_shape() {
        let stats = statistics(&sample_state());
        assert_eq!(stats["version"], json!(2));
        assert_eq!(stats["total_videos_tracked"], json!(2));
        assert_eq!(stats["total_processed"], json!(5));
        // Every recorded destination counts, including ones outside
        // `by_platform`; only the five known names are bucketed.
        assert_eq!(stats["cross_posted_count"], json!(3));
        assert_eq!(stats["by_platform"]["youtube"], json!(1));
        assert_eq!(stats["by_platform"]["instagram"], json!(0));
        assert_eq!(stats["last_check"], json!("2026-01-02T03:04:05"));
        assert_eq!(stats["last_wake_check"], Value::Null);
        assert_eq!(stats["dead_letter_count"], json!(1));
        assert_eq!(stats["platform_health"]["x"]["status"], json!("error"));
        assert_eq!(stats["platform_health"]["x"]["failures"], json!(3));
        assert_eq!(stats["platform_health"]["x"]["historical"], json!(true));
        assert_eq!(
            stats["platform_health"]["x"]["source"],
            json!("state_history")
        );
    }

    #[test]
    fn missing_state_reads_as_the_empty_document() {
        let stats = statistics(&Value::Object(empty_state()));
        assert_eq!(stats["version"], json!(2));
        assert_eq!(stats["total_videos_tracked"], json!(0));
        assert_eq!(stats["platform_health"]["youtube"]["status"], json!("ok"));
    }

    #[test]
    fn corrupt_state_file_falls_back_to_empty_not_an_error() {
        let dir = std::env::temp_dir().join(format!("xpst-status-corrupt-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(STATE_FILE_NAME), "{not json").unwrap();
        let state = effective_state(&dir);
        assert_eq!(statistics(&state)["total_videos_tracked"], json!(0));
        // Read-only: the corrupt document is left exactly as found.
        assert_eq!(
            std::fs::read_to_string(dir.join(STATE_FILE_NAME)).unwrap(),
            "{not json"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_newer_backup_is_used_when_the_main_file_is_unreadable() {
        let dir = std::env::temp_dir().join(format!("xpst-status-backup-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("backups")).unwrap();
        std::fs::write(dir.join(STATE_FILE_NAME), "[]").unwrap();
        std::fs::write(
            dir.join("backups/state_100.json"),
            json!({"version": 2, "posted_videos": {"a": {}}}).to_string(),
        )
        .unwrap();
        std::fs::write(
            dir.join("backups/state_200.json"),
            json!({"version": 2, "posted_videos": {"a": {}, "b": {}}}).to_string(),
        )
        .unwrap();
        let state = effective_state(&dir);
        assert_eq!(statistics(&state)["total_videos_tracked"], json!(2));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn legacy_v1_documents_are_migrated_in_memory() {
        let legacy = json!({
            "version": 1,
            "posted_video_ids": ["vid1"],
            "posted_to": {"youtube": ["vid1"], "x": ["other"]}
        });
        let mut map = legacy.as_object().unwrap().clone();
        migrate_and_ensure(&mut map);
        let state = Value::Object(map);
        assert_eq!(state["version"], json!(2));
        assert_eq!(
            state["posted_videos"]["vid1"]["posted_to"]["youtube"]["id"],
            json!("")
        );
        assert!(state["posted_videos"]["vid1"]["posted_to"]
            .get("x")
            .is_none());
    }

    #[test]
    fn cross_posted_documents_are_migrated_in_memory() {
        let legacy = json!({
            "version": 1,
            "cross_posted": {"tiktok:abc": {"youtube": {"post_id": "p1", "url": "u", "timestamp": "t"}}}
        });
        let mut map = legacy.as_object().unwrap().clone();
        migrate_and_ensure(&mut map);
        let state = Value::Object(map);
        assert_eq!(
            state["posted_videos"]["abc"]["source_platform"],
            json!("tiktok")
        );
        assert_eq!(
            state["posted_videos"]["abc"]["posted_to"]["youtube"]["id"],
            json!("p1")
        );
        assert_eq!(state["posted_videos"]["abc"]["downloaded_at"], json!("t"));
    }

    #[test]
    fn dead_letters_reports_status_and_unknown_age() {
        let now: jiff::Timestamp = "2026-01-01T00:00:00Z".parse().unwrap();
        let queue = dead_letters(&sample_state(), now);
        assert_eq!(queue.len(), 1);
        assert_eq!(queue[0]["status"], json!("open"));
        assert_eq!(queue[0]["resolved"], json!(false));
        assert_eq!(queue[0]["age_hours"], Value::Null);
        assert_eq!(queue[0]["count"], json!(2));
        assert_eq!(queue[0]["source_url"], Value::Null);
    }

    #[test]
    fn a_resolved_failure_is_reported_as_resolved() {
        let state = json!({
            "posted_videos": {
                "v": {
                    "posted_to": {"x": {"id": "live"}},
                    "errors": {"x": {"error": "old", "timestamp": "2025-12-31T22:00:00"}}
                }
            }
        });
        let now: jiff::Timestamp = "2026-01-01T00:00:00Z".parse().unwrap();
        let queue = dead_letters(&state, now);
        assert_eq!(queue[0]["status"], json!("resolved"));
        assert_eq!(queue[0]["resolved"], json!(true));
        assert_eq!(queue[0]["age_hours"], json!(2.0));
    }

    #[test]
    fn a_deleted_post_is_open_even_with_an_id() {
        let state = json!({
            "posted_videos": {
                "v": {
                    "posted_to": {"x": {"id": "gone", "deleted": true}},
                    "errors": {"x": {"error": "old"}}
                }
            }
        });
        let now: jiff::Timestamp = "2026-01-01T00:00:00Z".parse().unwrap();
        assert_eq!(dead_letters(&state, now)[0]["status"], json!("open"));
    }

    #[test]
    fn age_hours_parses_offsets_and_rejects_garbage() {
        let now: jiff::Timestamp = "2026-01-01T12:00:00Z".parse().unwrap();
        assert_eq!(
            age_hours(Some(&json!("2026-01-01T10:00:00Z")), now),
            Some(2.0)
        );
        assert_eq!(
            age_hours(Some(&json!("2026-01-01T05:00:00-05:00")), now),
            Some(2.0)
        );
        assert_eq!(age_hours(Some(&json!("not a time")), now), None);
        assert_eq!(age_hours(Some(&json!(1234)), now), None);
        assert_eq!(age_hours(None, now), None);
        // A future timestamp clamps to zero instead of reporting negative age.
        assert_eq!(
            age_hours(Some(&json!("2026-01-02T12:00:00Z")), now),
            Some(0.0)
        );
    }
}
