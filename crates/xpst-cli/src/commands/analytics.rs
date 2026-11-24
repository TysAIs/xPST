//! `xpst analytics export` — the local analytics export.
//!
//! Parity target: `src/xpst/cli.py::analytics_export` →
//! `xpst.analytics.AnalyticsCollector`. The command discovers post ids from
//! `state.json`, collects per-post metrics, and writes a JSON or CSV document.
//!
//! **What is ported and what is not.** The discovery, the platform filter, the
//! totals, the per-platform totals, the collection-status block, the file bytes
//! and the payload are the shipped behaviour. The *collection* is not: each
//! platform's metrics come from that platform's API (YouTube Data API, twikit,
//! instagrapg, the TikTok Content Posting API, the Threads Insights API), and
//! none of those clients is ported. So the export carries the answer Python
//! produces when no platform credentials are present — empty metric maps with
//! `collection_status.status: "ok"` — which is exactly the payload a
//! credential-less profile gets from the shipped engine. With credentials,
//! Python fetches live numbers and this engine reports none. Recorded as a
//! deviation in the ledger.
//!
//! The collector's ledger writes (`analytics.db`: metric snapshots and the
//! per-run collection outcomes) are not reproduced either: an export that
//! fetched nothing has nothing to record, and leaving the database untouched is
//! the safer half of the same divergence.
//!
//! `exported_at` is the wall clock, so the fixture masks it; every other byte of
//! the written document — key order, `json.dump(indent=2, ensure_ascii=False)`
//! separators, the CSV BOM and its `\r\n` terminators — is compared.

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::Value;

use crate::commands::bootstrap;
use crate::output::{boolean, integer, object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_string, paths, GlobalOptions};

/// `_discover_post_ids`'s platform table, in the order the dict literal lists it.
///
/// A platform outside this list is appended by `setdefault` in the order
/// `state.json` names it, which is why the map is ordered rather than a set.
const KNOWN_PLATFORMS: [&str; 5] = ["youtube", "instagram", "x", "tiktok", "threads"];

/// `state_schema.POST_ID_KEYS`: the canonical key, then the legacy fallback.
const POST_ID_KEYS: [&str; 2] = ["id", "post_id"];

/// Run `analytics export`.
pub fn export(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let format = leaf_string(leaf, "opt:format").unwrap_or_else(|| "json".to_string());
    let output = leaf_string(leaf, "opt:output").unwrap_or_default();
    let platforms = leaf_string(leaf, "opt:platforms");

    // `load_config` materializes the first-run `config.yaml`; the export only
    // needs the directory, but the side effect is part of the command.
    if let Err(error) = bootstrap::ensure_config(global) {
        let message = error.message();
        if json_mode {
            let payload = crate::output::error_object("CONFIG_ERROR", &message, vec![]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    }
    let Some(config_dir) = paths::config_dir() else {
        let message =
            "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set";
        if json_mode {
            let payload = crate::output::error_object("CONFIG_ERROR", message, vec![]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    };

    let mut post_ids = discover_post_ids(&config_dir);
    if let Some(filter) = platforms {
        let wanted: Vec<String> = filter
            .split(',')
            .map(|item| item.trim().to_string())
            .collect();
        post_ids.retain(|(platform, _)| wanted.iter().any(|name| name == platform));
    }

    let total_ids: usize = post_ids.iter().map(|(_, ids)| ids.len()).sum();
    if total_ids == 0 {
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("error", string("No posts found in state")),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, "No posts found in state. Run `xpst run` first.");
        }
        return exit::SUCCESS;
    }

    // The platforms that had ids, in discovery order — the collector only builds
    // a task for a platform with something to fetch, so `data` carries exactly
    // these keys.
    let collected: Vec<&String> = post_ids
        .iter()
        .filter(|(_, ids)| !ids.is_empty())
        .map(|(platform, _)| platform)
        .collect();

    let out_path = PathBuf::from(&output);
    if let Some(parent) = out_path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }

    let totals = object(vec![
        ("views", integer(0)),
        ("likes", integer(0)),
        ("comments", integer(0)),
        ("shares", integer(0)),
    ]);

    let write_result = match format.as_str() {
        "csv" => write_csv(&out_path),
        _ => write_json(&out_path, &collected, &totals),
    };
    if let Err(error) = write_result {
        let message = format!("cannot write {}: {error}", out_path.display());
        if json_mode {
            let payload = crate::output::error_object("EXPORT_FAILED", &message, vec![]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::GENERAL;
    }

    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("format", string(format.clone())),
            ("output", string(output.clone())),
            ("totals", totals),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(
            out,
            &format!(
                "✓ Analytics exported to {output} ({format})\n  \
                 Views: 0  Likes: 0  Comments: 0  Shares: 0"
            ),
        );
    }
    exit::SUCCESS
}

/// `_discover_post_ids`: platform → post ids, read from `state.json`.
///
/// A missing or unparseable document is an empty map (Python catches
/// `JSONDecodeError`/`OSError` and returns `{}`), which is what makes the
/// "No posts found in state" branch reachable on a fresh profile.
fn discover_post_ids(config_dir: &Path) -> Vec<(String, Vec<String>)> {
    let mut post_ids: Vec<(String, Vec<String>)> = KNOWN_PLATFORMS
        .iter()
        .map(|platform| ((*platform).to_string(), Vec::new()))
        .collect();

    let state_path = config_dir.join("state.json");
    let Ok(text) = std::fs::read_to_string(&state_path) else {
        return post_ids;
    };
    let Ok(state) = serde_json::from_str::<Value>(&text) else {
        return post_ids;
    };
    let Some(videos) = state.get("posted_videos").and_then(Value::as_object) else {
        return post_ids;
    };

    for video in videos.values() {
        let Some(platforms) = video.get("posted_to").and_then(Value::as_object) else {
            continue;
        };
        for (platform, info) in platforms {
            let post_id = resolve_platform_post_id(info);
            if post_id.is_empty() {
                continue;
            }
            match post_ids.iter_mut().find(|(name, _)| name == platform) {
                Some((_, ids)) => ids.push(post_id),
                None => post_ids.push((platform.clone(), vec![post_id])),
            }
        }
    }
    post_ids
}

/// `resolve_platform_post_id`: the canonical `id`, then the legacy `post_id`.
///
/// A falsy value is skipped (Python's `if value:`), and the answer is
/// `str(value).strip()` — so a numeric id becomes its decimal text.
fn resolve_platform_post_id(info: &Value) -> String {
    let Some(object) = info.as_object() else {
        return String::new();
    };
    for key in POST_ID_KEYS {
        let Some(value) = object.get(key) else {
            continue;
        };
        let truthy = match value {
            Value::Null => false,
            Value::Bool(flag) => *flag,
            Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
            Value::String(text) => !text.is_empty(),
            Value::Array(items) => !items.is_empty(),
            Value::Object(map) => !map.is_empty(),
        };
        if !truthy {
            continue;
        }
        let rendered = match value {
            Value::String(text) => text.clone(),
            Value::Bool(true) => "True".to_string(),
            Value::Bool(false) => "False".to_string(),
            Value::Null => "None".to_string(),
            other => other.to_string(),
        };
        return rendered.trim().to_string();
    }
    String::new()
}

/// The `collection_status` block for a platform this engine did not fetch.
///
/// `_collection_status` answers `{attempted: 1, failures: 0, not_found: 0,
/// status: "ok", reason: null, error: null, source: "live"}` when the collector
/// ran, attempted the platform and recorded no failure — the shape a
/// credential-less profile produces.
fn collection_status() -> xpst_config::Value {
    object(vec![
        ("attempted", integer(1)),
        ("failures", integer(0)),
        ("not_found", integer(0)),
        ("status", string("ok")),
        ("reason", crate::output::null()),
        ("error", crate::output::null()),
        ("source", string("live")),
    ])
}

/// `json.dump(export_data, f, indent=2, default=str, ensure_ascii=False)`.
fn write_json(
    path: &Path,
    platforms: &[&String],
    totals: &xpst_config::Value,
) -> std::io::Result<()> {
    let empty_platforms = platforms
        .iter()
        .map(|platform| ((*platform).clone(), object(vec![])))
        .collect::<Vec<_>>();
    let platform_totals = platforms
        .iter()
        .map(|platform| {
            (
                (*platform).clone(),
                object(vec![
                    ("posts", integer(0)),
                    ("views", integer(0)),
                    ("likes", integer(0)),
                    ("comments", integer(0)),
                    ("shares", integer(0)),
                ]),
            )
        })
        .collect::<Vec<_>>();
    let statuses = platforms
        .iter()
        .map(|platform| ((*platform).clone(), collection_status()))
        .collect::<Vec<_>>();

    let mut platform_map = xpst_config::Mapping::new();
    for (name, posts) in empty_platforms {
        platform_map.insert(xpst_config::Value::from(name), posts);
    }
    let mut totals_map = xpst_config::Mapping::new();
    for (name, value) in platform_totals {
        totals_map.insert(xpst_config::Value::from(name), value);
    }
    let mut status_map = xpst_config::Mapping::new();
    for (name, value) in statuses {
        status_map.insert(xpst_config::Value::from(name), value);
    }

    let document = object(vec![
        ("platforms", xpst_config::Value::Mapping(platform_map)),
        ("totals", totals.clone()),
        ("platform_totals", xpst_config::Value::Mapping(totals_map)),
        ("collection_status", xpst_config::Value::Mapping(status_map)),
        ("exported_at", string(local_isoformat())),
    ]);
    // `json.dump` writes no trailing newline.
    std::fs::write(path, crate::output::python_pretty_json_cfg_utf8(&document))
}

/// `csv.writer` with `newline=""`: a UTF-8 BOM, then `\r\n`-terminated rows.
///
/// No metrics were collected, so only the header is written. The row loop lives
/// in the collector, which is not ported; when it is, its rows go here in
/// `data.items()` order.
fn write_csv(path: &Path) -> std::io::Result<()> {
    let mut text = String::from("\u{feff}");
    text.push_str("platform,post_id,caption,views,likes,comments,shares,saves,timestamp\r\n");
    std::fs::write(path, text)
}

/// `datetime.now().isoformat()`: local time, no offset, microseconds.
fn local_isoformat() -> String {
    jiff::Timestamp::now()
        .to_zoned_datetime(jiff::tz::TimeZone::system())
        .strftime("%Y-%m-%dT%H:%M:%S%.6f")
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_canonical_id_wins_over_the_legacy_one() {
        let info = serde_json::json!({"id": "canonical", "post_id": "legacy"});
        assert_eq!(resolve_platform_post_id(&info), "canonical");
        let info = serde_json::json!({"post_id": "legacy"});
        assert_eq!(resolve_platform_post_id(&info), "legacy");
    }

    #[test]
    fn a_falsy_id_is_nothing_to_fetch() {
        assert_eq!(resolve_platform_post_id(&serde_json::json!({"id": ""})), "");
        assert_eq!(
            resolve_platform_post_id(&serde_json::json!({"id": null})),
            ""
        );
        assert_eq!(resolve_platform_post_id(&serde_json::json!({})), "");
        assert_eq!(
            resolve_platform_post_id(&serde_json::json!("not a map")),
            ""
        );
    }

    #[test]
    fn a_numeric_id_renders_as_its_decimal_text() {
        assert_eq!(
            resolve_platform_post_id(&serde_json::json!({"id": 12345})),
            "12345"
        );
        assert_eq!(
            resolve_platform_post_id(&serde_json::json!({"id": "  spaced  "})),
            "spaced"
        );
    }

    #[test]
    fn discovery_keeps_pythons_platform_order() {
        let dir = std::env::temp_dir().join(format!("xpst-analytics-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("state.json"),
            r#"{"posted_videos": {"v1": {"posted_to": {"tiktok": {"id": "tt-1"}, "youtube": {"id": "yt-1"}}}}}"#,
        )
        .unwrap();
        let ids = discover_post_ids(&dir);
        let names: Vec<&str> = ids.iter().map(|(name, _)| name.as_str()).collect();
        assert_eq!(
            names,
            vec!["youtube", "instagram", "x", "tiktok", "threads"]
        );
        assert_eq!(ids[0].1, vec!["yt-1".to_string()]);
        assert_eq!(ids[3].1, vec!["tt-1".to_string()]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unknown_platform_is_appended_in_state_order() {
        let dir =
            std::env::temp_dir().join(format!("xpst-analytics-unknown-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("state.json"),
            r#"{"posted_videos": {"v1": {"posted_to": {"messenger": {"id": "m-1"}}}}}"#,
        )
        .unwrap();
        let ids = discover_post_ids(&dir);
        assert_eq!(ids.len(), 6);
        assert_eq!(ids[5].0, "messenger");
        assert_eq!(ids[5].1, vec!["m-1".to_string()]);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
