//! Behaviour of the ported read-only tools, driven from fixture stores.
//!
//! Each test writes the same JSON/YAML shape the Python engine writes and
//! asserts the payload the tool returns. Nothing here touches the network or a
//! real profile directory.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use xpst_mcp::registry::registry;
use xpst_mcp::{StorePaths, ToolContext};

fn profile(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create fixture profile");
    dir
}

fn ctx(dir: &Path) -> ToolContext {
    ToolContext::new(StorePaths::at(dir))
}

fn call(ctx: &ToolContext, tool: &str, args: Value) -> Value {
    registry()
        .dispatch(ctx, tool, &args)
        .unwrap_or_else(|err| panic!("{tool} failed: {err}"))
}

#[test]
fn config_show_masks_secrets_and_keeps_the_python_projection() {
    let dir = profile("config_show");
    std::fs::write(
        dir.join("config.yaml"),
        "version: 4\n\
         accounts:\n  youtube:\n    enabled: false\n    username: alice\n    client_secrets: /home/someone/secrets.json\n\
         \x20 x:\n    cookies_file: /home/someone/cookies.json\n\
         video:\n  download_dir: /tmp/videos\n  cleanup_after_post: true\n\
         monitoring:\n  log_level: DEBUG\n  dashboard_password_hash: hunter2\n\
         schedule:\n  check_interval: 120\n\
         unrelated_section:\n  secret_key: also-hidden\n",
    )
    .expect("write config");

    let payload = call(&ctx(&dir), "xpst_config_show", json!({}));

    // The file's values win where it has them...
    assert_eq!(payload["accounts"]["youtube"]["enabled"], json!(false));
    assert_eq!(payload["accounts"]["youtube"]["username"], json!("alice"));
    assert_eq!(payload["video"]["download_dir"], json!("/tmp/videos"));
    assert_eq!(payload["video"]["cleanup_after_post"], json!(true));
    assert_eq!(payload["monitoring"]["log_level"], json!("DEBUG"));
    assert_eq!(payload["schedule"]["check_interval"], json!(120));
    // ...and the model's defaults fill the rest, which is what Python serves.
    assert_eq!(payload["accounts"]["youtube"]["video_mode"], json!("auto"));
    assert_eq!(payload["accounts"]["tiktok"]["provider"], json!("local"));
    assert_eq!(payload["monitoring"]["healthcheck_port"], json!(8080));
    // Secrets are masked, empty strings stay empty.
    assert_eq!(
        payload["accounts"]["youtube"]["client_secrets"],
        json!("***")
    );
    assert_eq!(payload["accounts"]["x"]["cookies_file"], json!("***"));
    assert_eq!(
        payload["monitoring"]["dashboard_password_hash"],
        json!("***")
    );
    assert_eq!(payload["accounts"]["youtube"]["channel_id"], json!(""));
    // Nested dataclasses render as Python `repr()` strings.
    assert!(
        payload["video"]["encoding_youtube"]
            .as_str()
            .unwrap_or_default()
            .starts_with("EncodingConfig(resolution="),
        "{}",
        payload["video"]["encoding_youtube"]
    );
    assert!(
        payload["accounts"]["tiktok"]["aggregator"]
            .as_str()
            .unwrap_or_default()
            .starts_with("TikTokAggregatorConfig("),
        "{}",
        payload["accounts"]["tiktok"]["aggregator"]
    );
    assert!(
        payload.get("unrelated_section").is_none(),
        "the projection must not leak sections Python does not return"
    );

    let rendered = serde_json::to_string(&payload).expect("serialise");
    assert!(!rendered.contains("hunter2"));
    assert!(!rendered.contains("also-hidden"));
}

#[test]
fn config_show_answers_the_model_defaults_when_the_profile_is_fresh() {
    // `XPSTConfig.load()` materializes the defaults, so the shipped tool
    // answers a full projection on a profile that has never been used. A
    // file-only projection would report `accounts.youtube.enabled` as absent
    // while the engine actually runs with `true`.
    let dir = profile("config_show_missing");
    let payload = call(&ctx(&dir), "xpst_config_show", json!({}));
    assert_eq!(payload["accounts"]["youtube"]["enabled"], json!(true));
    assert_eq!(payload["accounts"]["threads"]["enabled"], json!(false));
    assert_eq!(payload["monitoring"]["log_level"], json!("INFO"));
    assert_eq!(payload["schedule"]["check_interval"], json!(900));
    assert!(payload["video"]["download_dir"]
        .as_str()
        .unwrap_or_default()
        .ends_with("/.xpst/downloads"));
}

#[test]
fn status_counts_match_the_python_statistics_contract() {
    let dir = profile("status");
    std::fs::write(
        dir.join("state.json"),
        serde_json::to_string_pretty(&json!({
            "version": 2,
            "posted_videos": {
                "a": {"posted_to": {"youtube": {}, "x": {}}},
                "b": {"posted_to": {"youtube": {}}},
                "c": {"posted_to": {"bluesky": {}}},
                "d": {"errors": {"tiktok": {"error": "boom"}, "x": {"error": "nope"}}}
            },
            "health": {
                "total_processed": 7,
                "last_check": "2026-11-16T10:00:00",
                "last_wake_check": null,
                "platforms": {
                    "youtube": {"status": "healthy", "last_success": "2026-11-16T09:59:00", "failures": 0}
                }
            }
        }))
        .expect("serialise state"),
    )
    .expect("write state");

    let payload = call(&ctx(&dir), "xpst_status", json!({}));

    assert_eq!(payload["total_videos_tracked"], json!(4));
    assert_eq!(payload["total_processed"], json!(7));
    assert_eq!(payload["cross_posted_count"], json!(4));
    assert_eq!(payload["by_platform"]["youtube"], json!(2));
    assert_eq!(payload["by_platform"]["x"], json!(1));
    assert_eq!(payload["by_platform"]["threads"], json!(0));
    assert_eq!(payload["dead_letter_count"], json!(2));
    assert_eq!(
        payload["platform_health"]["youtube"]["historical"],
        json!(true)
    );
    assert_eq!(
        payload["platform_health"]["youtube"]["source"],
        json!("state_history")
    );
    assert!(payload["platform_health_note"]
        .as_str()
        .expect("note")
        .contains("not a live check"));
}

#[test]
fn status_of_a_fresh_install_is_the_empty_ledger_not_an_error() {
    let dir = profile("status_empty");
    let payload = call(&ctx(&dir), "xpst_status", json!({}));
    assert_eq!(payload["total_videos_tracked"], json!(0));
    assert_eq!(payload["by_platform"]["youtube"], json!(0));
    assert!(payload["platform_health"]
        .as_object()
        .expect("object")
        .is_empty());
}

#[test]
fn rate_limit_status_reports_active_cooldowns_and_idle_ones() {
    let dir = profile("rate_limit");
    std::fs::write(
        dir.join("rate_cooldowns.json"),
        serde_json::to_string(&json!({
            "instagram": {
                "strikes": 2,
                "until_iso": "2026-11-17T12:03:20+00:00",
                "last_hit_iso": "2026-11-17T12:00:00+00:00",
                "last_reason": "429",
                "clean_publishes_since_hit": 0
            },
            "x": {
                "strikes": 1,
                "until_iso": "2026-11-17T09:00:00+00:00",
                "last_hit_iso": "2026-11-17T08:00:00+00:00",
                "last_reason": "old",
                "clean_publishes_since_hit": 3
            },
            "tiktok": {"strikes": 0, "until_iso": "", "last_hit_iso": "", "last_reason": "", "clean_publishes_since_hit": 0}
        }))
        .expect("serialise"),
    )
    .expect("write cooldowns");

    let path = dir.join("rate_cooldowns.json");
    let now: jiff::Timestamp = "2026-11-17T12:00:00+00:00".parse().expect("instant");
    let payload = xpst_mcp::tools::rate_limit::report(&path, now).expect("report");

    assert_eq!(payload["state"], json!("cooling"));
    assert_eq!(payload["active"], json!(["instagram"]));
    assert_eq!(
        payload["cooldowns"]["instagram"]["remaining_s"],
        json!(200.0)
    );
    assert_eq!(
        payload["cooldowns"]["instagram"]["remaining_human"],
        json!("3m 20s")
    );
    assert_eq!(payload["file_present"], json!(true));
    assert!(
        payload["cooldowns"].get("tiktok").is_none(),
        "idle records are omitted"
    );
    assert!(payload["note"]
        .as_str()
        .expect("note")
        .contains("does not fail them"));
}

#[test]
fn rate_limit_status_is_idle_with_no_file() {
    let dir = profile("rate_limit_empty");
    let payload = call(&ctx(&dir), "xpst_rate_limit_status", json!({}));
    assert_eq!(payload["state"], json!("idle"));
    assert_eq!(payload["file_present"], json!(false));
    assert!(payload["active"].as_array().expect("array").is_empty());
}

#[test]
fn schedule_list_sorts_entries_and_reports_the_pause_marker() {
    let dir = profile("schedule");
    std::fs::write(
        dir.join("schedule.json"),
        serde_json::to_string(&json!([
            {"id": "later", "status": "pending", "scheduled_time": "2026-12-01T15:00:00+00:00", "caption": "b"},
            {"id": "sooner", "status": "pending", "scheduled_time": "2026-11-20T09:00:00+00:00", "caption": "a"}
        ]))
        .expect("serialise"),
    )
    .expect("write schedule");
    std::fs::write(
        dir.join("schedule.paused.json"),
        serde_json::to_string(&json!({"paused_at": "2026-11-17T08:00:00", "reason": "travel"}))
            .expect("serialise"),
    )
    .expect("write pause marker");
    std::fs::write(
        dir.join("schedule.json.paused-20261101-120000"),
        serde_json::to_string(&json!([{"id": "parked"}, {"id": "parked2"}])).expect("serialise"),
    )
    .expect("write snapshot");

    let payload = call(&ctx(&dir), "xpst_schedule_list", json!({}));

    let schedules = payload["schedules"].as_array().expect("array");
    assert_eq!(schedules.len(), 2);
    assert_eq!(schedules[0]["id"], json!("sooner"));
    assert!(schedules[0]["scheduled_time_local"].is_string());
    assert_eq!(payload["paused"]["paused"], json!(true));
    assert_eq!(payload["paused"]["reason"], json!("travel"));
    let snapshots = payload["paused_snapshots"].as_array().expect("snapshots");
    assert_eq!(snapshots.len(), 1);
    assert_eq!(snapshots[0]["entries"], json!(2));
    assert!(payload["note"].as_str().expect("note").contains("PAUSED"));
}

#[test]
fn an_unreadable_pause_marker_fails_closed() {
    let dir = profile("schedule_bad_marker");
    std::fs::write(dir.join("schedule.paused.json"), "{not json").expect("write marker");
    let payload = call(&ctx(&dir), "xpst_schedule_list", json!({}));
    assert_eq!(payload["paused"]["paused"], json!(true));
    assert_eq!(
        payload["paused"]["reason"],
        json!("unreadable pause marker")
    );
    assert!(payload.get("paused_snapshots").is_none());
}

#[test]
fn a_corrupt_schedule_entry_is_listed_verbatim_with_a_null_local_time() {
    let dir = profile("schedule_corrupt_entry");
    std::fs::write(
        dir.join("schedule.json"),
        serde_json::to_string(&json!([{"id": "bad", "scheduled_time": "not-a-time"}]))
            .expect("serialise"),
    )
    .expect("write schedule");
    let payload = call(&ctx(&dir), "xpst_schedule_list", json!({}));
    let entry = &payload["schedules"][0];
    assert_eq!(entry["scheduled_time"], json!("not-a-time"));
    assert_eq!(entry["scheduled_time_local"], Value::Null);
}

#[test]
fn providers_serves_the_captured_catalog_verbatim() {
    let dir = profile("providers");
    let payload = call(&ctx(&dir), "xpst_providers", json!({}));
    let expected: Value = serde_json::from_str(
        &std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/python_providers.json"),
        )
        .expect("fixture"),
    )
    .expect("fixture json");
    assert_eq!(payload, expected);
    assert!(payload["sources"]
        .as_array()
        .expect("sources")
        .iter()
        .any(|item| item["name"] == json!("tiktok")));
    assert!(payload["destinations"]
        .as_array()
        .expect("destinations")
        .iter()
        .any(|item| item["name"] == json!("youtube")));
}
