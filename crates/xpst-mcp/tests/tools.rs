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

#[test]
fn readiness_serves_the_report_and_mirrors_its_verdict() {
    let dir = profile("readiness");
    let payload = call(&ctx(&dir), "xpst_readiness", json!({}));

    assert_eq!(payload["contract_version"], json!(1));
    // `ok` is the report's own verdict, never a constant: a pristine profile is
    // not ready, so an agent gating on `ok` must not proceed.
    assert_eq!(payload["ok"], json!(false));
    assert_eq!(payload["readiness"]["ready"], json!(false));
    let checks = payload["readiness"]["checks"]
        .as_array()
        .expect("checks array");
    assert!(
        checks.len() >= 5,
        "the report must carry the setup checks, got {}",
        checks.len()
    );
    let ids: Vec<&str> = checks
        .iter()
        .map(|check| check["id"].as_str().unwrap_or_default())
        .collect();
    for expected in [
        "python",
        "directories",
        "ffmpeg",
        "yt_dlp",
        "source",
        "destinations",
    ] {
        assert!(ids.contains(&expected), "missing check {expected}: {ids:?}");
    }
    // The first-run config is materialized, exactly as `XPSTConfig.load` does.
    assert!(dir.join("config.yaml").exists());
}

#[test]
fn security_audit_serves_the_tool_document_not_the_cli_one() {
    let dir = profile("security");
    let payload = call(&ctx(&dir), "xpst_security_audit", json!({}));

    assert!(payload["overall_status"].is_string());
    let checks = payload["checks"].as_array().expect("checks array");
    assert!(checks.len() >= 5, "got {} checks", checks.len());
    // `cli.py:7906` adds `check_count` on top of `run_security_checks`; the MCP
    // tool serves the raw document, so the field must NOT be here.
    assert!(
        payload.get("check_count").is_none(),
        "the MCP tool must serve run_security_checks verbatim"
    );
    let names: Vec<&str> = checks
        .iter()
        .map(|check| check["check"].as_str().unwrap_or_default())
        .collect();
    for expected in [
        "dashboard_localhost",
        "mcp_readonly",
        "provider_mode",
        "ffmpeg_available",
        "encrypted_storage",
    ] {
        assert!(
            names.contains(&expected),
            "missing check {expected}: {names:?}"
        );
    }
    // A CLI process never binds the dashboard, so the row is honest about it.
    let dashboard = checks
        .iter()
        .find(|check| check["check"] == json!("dashboard_localhost"))
        .expect("dashboard row");
    assert_eq!(dashboard["status"], json!("unobserved"));
}

#[test]
fn best_time_is_empty_without_a_ledger_and_shapes_both_forms() {
    let dir = profile("best_time");
    let all = call(&ctx(&dir), "xpst_best_time", json!({}));
    assert_eq!(all, json!({"best_times": {}}));

    let one = call(&ctx(&dir), "xpst_best_time", json!({"platform": "youtube"}));
    assert_eq!(one, json!({"platform": "youtube", "best_time": null}));
}

#[test]
fn kb_query_reports_a_bad_workspace_as_pythons_exception_text() {
    let dir = profile("kb_query");
    let error = registry()
        .dispatch(
            &ctx(&dir),
            "kb_query",
            &json!({"text": "x", "workspace": "../escape"}),
        )
        .expect_err("invalid workspace");
    // Python raises `ValueError`, `handle_call_tool` serves `Error: {str(e)}`.
    assert_eq!(error.code(), "handler_error");
    assert!(
        error
            .to_string()
            .starts_with("Error: Invalid workspace name"),
        "{error}"
    );
}

#[test]
fn kb_query_without_text_reports_the_key_error() {
    let dir = profile("kb_query_missing");
    let error = registry()
        .dispatch(&ctx(&dir), "kb_query", &json!({}))
        .expect_err("missing text");
    assert_eq!(error.to_string(), "Error: 'text'");
}

#[test]
fn bio_get_lists_enabled_accounts_then_custom_links() {
    let dir = profile("bio");
    std::fs::write(
        dir.join("config.yaml"),
        r#"version: 4
accounts:
  youtube:
    enabled: true
    username: '@example'
  x:
    enabled: false
    username: example
bio:
  handle: Example Creator
  links:
    - label: Site
      url: https://example.invalid
    - label: Bad
      url: javascript:alert(1)
"#,
    )
    .expect("write config");

    let payload = call(&ctx(&dir), "xpst_bio_get", json!({}));
    assert_eq!(payload["url"], json!("http://127.0.0.1:8080/bio"));
    assert_eq!(payload["handle"], json!("Example Creator"));
    assert_eq!(
        payload["links"],
        json!([
            {"label": "YouTube", "url": "https://youtube.com/@example"},
            {"label": "Site", "url": "https://example.invalid"},
        ]),
        "an enabled account with a handle comes first; a non-http link is dropped"
    );
}

/// `xpst_activity`: one row per (video, platform) failure, `retry` only when the
/// uploader classified the failure as retryable, newest first.
///
/// The `instagram` entry is a bare string on purpose — the handler skips a
/// non-dict result, and `get_statistics` raises on one, so this branch cannot be
/// carried by the shared protocol fixture.
#[test]
fn activity_flattens_failures_and_skips_a_non_dict_entry() {
    let dir = profile("activity");
    std::fs::write(
        dir.join("state.json"),
        r#"{
          "version": 2,
          "posted_videos": {
            "vid-a": {
              "source_url": "https://example.invalid/a",
              "last_attempt": "2026-11-20T10:00:00+00:00",
              "errors": {
                "youtube": {"error": "quota exceeded", "retryable": true, "timestamp": "2026-11-20T10:00:00+00:00"},
                "x": {"error": "rate limited", "retryable": false, "timestamp": "2026-11-19T09:00:00+00:00"},
                "instagram": "not a dict"
              }
            },
            "vid-b": {
              "source_url": "https://example.invalid/b",
              "errors": {"youtube": {"error": "", "retryable": null}}
            }
          },
          "health": {"total_processed": 2, "platforms": {}}
        }"#,
    )
    .expect("write state");

    let payload = call(&ctx(&dir), "xpst_activity", json!({}));

    assert_eq!(payload["count"], json!(3), "the non-dict entry is skipped");
    let rows = payload["failures"].as_array().expect("failures");
    assert_eq!(rows.len(), 3);
    // Newest first: vid-a/youtube, vid-a/x, then the undated vid-b row.
    assert_eq!(rows[0]["video_id"], json!("vid-a"));
    assert_eq!(rows[0]["platform"], json!("youtube"));
    assert_eq!(rows[0]["action"], json!("retry"));
    assert_eq!(rows[0]["retryable"], json!(true));
    assert_eq!(rows[1]["action"], json!("review"));
    assert_eq!(rows[1]["retryable"], json!(false));
    // `error or "Unknown error"`, and a missing timestamp stays null.
    assert_eq!(rows[2]["error"], json!("Unknown error"));
    assert_eq!(rows[2]["last_attempt"], Value::Null);
    assert_eq!(rows[2]["action"], json!("review"));
    assert_eq!(rows[2]["post_id"], Value::Null);
    assert_eq!(rows[2]["post_url"], Value::Null);
}

/// `xpst_followers`: per-platform count, growth over the last seven snapshots,
/// and the history oldest-first.
#[test]
fn followers_reports_counts_growth_and_history() {
    let dir = profile("followers");
    let store = xpst_store::Store::open(dir.join("analytics.db")).expect("open ledger");
    for (platform, count, captured_at) in [
        ("youtube", 1200, "2026-11-01T00:00:00+00:00"),
        ("youtube", 1240, "2026-11-08T00:00:00+00:00"),
        ("youtube", 1310, "2026-11-15T00:00:00+00:00"),
        ("x", 480, "2026-11-15T00:00:00+00:00"),
    ] {
        store
            .record_followers(platform, count, captured_at)
            .expect("seed follower");
    }

    let payload = call(&ctx(&dir), "xpst_followers", json!({}));

    assert_eq!(payload["total_followers"], json!(1790));
    assert_eq!(payload["platform_count"], json!(2));
    let platforms = payload["platforms"].as_array().expect("platforms");
    // `sorted(latest.items())` — x before youtube.
    assert_eq!(platforms[0]["platform"], json!("x"));
    assert_eq!(platforms[0]["count"], json!(480));
    assert_eq!(
        platforms[0]["growth"],
        json!(0),
        "one snapshot is no growth"
    );
    assert_eq!(platforms[1]["platform"], json!("youtube"));
    assert_eq!(platforms[1]["count"], json!(1310));
    assert_eq!(platforms[1]["growth"], json!(110));
    assert_eq!(
        platforms[1]["history"],
        json!([
            {"count": 1200, "captured_at": "2026-11-01T00:00:00+00:00"},
            {"count": 1240, "captured_at": "2026-11-08T00:00:00+00:00"},
            {"count": 1310, "captured_at": "2026-11-15T00:00:00+00:00"},
        ]),
        "history is oldest first"
    );
}

/// `xpst_followers` on a fresh profile: `AnalyticsStore.__init__` creates the
/// ledger, so the answer is zeroes, not an error.
#[test]
fn followers_on_a_fresh_profile_is_zeroes() {
    let dir = profile("followers-fresh");
    let payload = call(&ctx(&dir), "xpst_followers", json!({}));
    assert_eq!(payload["total_followers"], json!(0));
    assert_eq!(payload["platform_count"], json!(0));
    assert_eq!(payload["platforms"], json!([]));
    assert!(dir.join("analytics.db").exists(), "the ledger is created");
}

/// `xpst_capabilities`: the offline catalog projection plus the content
/// contract, with every live-derived fact `null`.
#[test]
fn capabilities_serves_the_offline_catalog_and_the_content_contract() {
    let dir = profile("capabilities");
    // Disable YouTube explicitly: `state` otherwise depends on whether the
    // machine's real `~/.xpst/youtube_token.json` exists, which would make this
    // assertion a property of the developer's profile.
    std::fs::write(
        dir.join("config.yaml"),
        "version: 4\naccounts:\n  youtube:\n    enabled: false\n",
    )
    .expect("write config");
    let payload = call(&ctx(&dir), "xpst_capabilities", json!({}));

    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["contract_version"], json!(1));
    assert_eq!(
        payload["roles"],
        json!(["source", "video_destination", "messaging", "analytics"])
    );
    let providers = payload["providers"].as_array().expect("providers");
    assert_eq!(providers.len(), 10, "every SUPPORTED_PROVIDERS entry");
    let youtube = providers
        .iter()
        .find(|row| row["name"] == json!("youtube"))
        .expect("youtube");
    assert_eq!(
        youtube.as_object().expect("row").keys().collect::<Vec<_>>(),
        vec![
            "name",
            "display_name",
            "roles",
            "capabilities",
            "state",
            "docs_url"
        ],
        "the tool's projection is six fields, not the CLI's whole catalog entry"
    );
    assert_eq!(youtube["state"], json!("disabled"));
    assert_eq!(
        youtube["roles"],
        json!(["source", "video_destination", "analytics"])
    );
    assert!(youtube["capabilities"]
        .as_array()
        .is_some_and(|caps| caps.contains(&json!("upload"))));
    // The content contract rides along verbatim.
    assert_eq!(payload["content"]["contract_version"], json!(2));
    assert!(payload["content"]["platforms"]["youtube"].is_object());
}

/// `xpst_transcript` refuses an unsafe id before touching the workspace, and the
/// wire body has no `Error: ` prefix — the handler answers with its own
/// `CallToolResult(isError=True, ...)` rather than raising.
#[test]
fn transcript_rejects_an_unsafe_video_id() {
    let dir = profile("transcript-invalid");
    let error = registry()
        .dispatch(
            &ctx(&dir),
            "xpst_transcript",
            &json!({"video_id": "../escape"}),
        )
        .expect_err("an unsafe id is refused");
    assert_eq!(
        error.to_string(),
        "Invalid video_id: must contain only alphanumeric, dash, underscore, colon, or dot \
         characters (got: '../escape')"
    );
    assert_eq!(error.code(), "reported_error");
}

// ── the three tools this tranche added ──────────────────────────────────────

/// `xpst_byo_app` reads the registry's `store_id_key`/`store_secret_key`, not a
/// config field: a store write must flip the status to `encrypted-store`, and a
/// clear must flip it back. A wrong store key would read as "no app configured"
/// forever, which is why this round trip exists.
#[test]
fn byo_app_round_trips_through_the_encrypted_store() {
    let dir = profile("byo_app");
    let context = ctx(&dir);

    let before = call(&context, "xpst_byo_app", json!({"platform": "instagram"}));
    assert_eq!(before["action"], json!("status"));
    assert_eq!(before["platform_status"]["configured"], json!(false));
    assert_eq!(before["platform_status"]["source"], json!(""));

    let stored = call(
        &context,
        "xpst_byo_app",
        json!({
            "platform": "instagram",
            "app_id": "1234567890",
            "app_secret": "parity-secret-1",
        }),
    );
    assert_eq!(stored["action"], json!("stored"));
    assert_eq!(stored["configured"], json!(true));
    assert_eq!(stored["source"], json!("encrypted-store"));
    // Never the identifier in full.
    assert_eq!(stored["app_id_masked"], json!("…7890"));

    // `instagram` and `threads` share one Meta app, so the second platform sees
    // the same credential — the registry's shared store keys, not a per-platform
    // config field.
    let threads = call(&context, "xpst_byo_app", json!({"platform": "threads"}));
    assert_eq!(threads["platform_status"]["configured"], json!(true));
    assert_eq!(
        threads["platform_status"]["source"],
        json!("encrypted-store")
    );

    let cleared = call(
        &context,
        "xpst_byo_app",
        json!({"platform": "instagram", "clear": true}),
    );
    assert_eq!(cleared["action"], json!("clear"));
    assert_eq!(
        cleared["removed"],
        json!(["byo_meta_app_id", "byo_meta_app_secret"])
    );

    let after = call(&context, "xpst_byo_app", json!({"platform": "instagram"}));
    assert_eq!(after["platform_status"]["configured"], json!(false));
}

/// A platform that keeps its app credential in its own connect flow refuses a
/// store call with a bare sentence, and the status read still works.
#[test]
fn byo_app_refuses_a_store_on_a_read_only_platform() {
    let dir = profile("byo_app_readonly");
    let context = ctx(&dir);
    let error = registry()
        .dispatch(
            &context,
            "xpst_byo_app",
            &json!({"platform": "messenger", "app_id": "1234567890"}),
        )
        .expect_err("refused");
    assert_eq!(error.code(), "reported_error");
    assert!(
        error
            .to_string()
            .starts_with("Messenger keeps its app credential in its own connect flow"),
        "{error}"
    );
}

/// `xpst_chat` reports a configured channel as ready and an unconfigured one as
/// `no_channel` — the two answers that differ in their next step.
#[test]
fn chat_readiness_separates_a_missing_channel_from_a_missing_credential() {
    let dir = profile("chat");
    std::fs::write(
        dir.join("config.yaml"),
        "version: 4\nchat:\n  twitch_channels:\n    - paritychannel\n",
    )
    .expect("write config");
    let payload = call(&ctx(&dir), "xpst_chat", json!({}));
    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["read_only"], json!(true));

    let rows = payload["platforms"].as_array().expect("platforms");
    let twitch = rows
        .iter()
        .find(|row| row["platform"] == json!("twitch"))
        .expect("twitch row");
    assert_eq!(twitch["status"], json!("ready"));
    assert_eq!(twitch["channels"], json!(["paritychannel"]));

    let youtube = rows
        .iter()
        .find(|row| row["platform"] == json!("youtube"))
        .expect("youtube row");
    assert_eq!(youtube["status"], json!("unavailable"));
    assert_eq!(youtube["missing"], json!(["oauth_token"]));
    assert_eq!(youtube["unavailable"]["code"], json!("credentials_missing"));

    // A platform with no read path is a row, not a silent omission.
    let unknown = call(&ctx(&dir), "xpst_chat", json!({"platforms": ["nope"]}));
    assert_eq!(unknown["platforms"][0]["status"], json!("unavailable"));
    assert_eq!(
        unknown["platforms"][0]["unavailable"]["code"],
        json!("not_supported")
    );
}

/// `xpst_stream` serves the whole registry, and an unknown platform is the
/// payload with `code: unknown_platform` — an error result whose body is still
/// the document, exactly as Python answers.
#[test]
fn stream_reports_the_registry_and_refuses_an_unknown_platform() {
    let dir = profile("stream");
    let context = ctx(&dir);

    let payload = call(&context, "xpst_stream", json!({}));
    assert_eq!(payload["contract_version"], json!(1));
    assert_eq!(payload["stream"]["ready_to_relay"], json!(false));
    let rows = payload["stream"]["platforms"]
        .as_array()
        .expect("platforms");
    assert_eq!(rows.len(), 10, "the captured registry has ten ingest paths");
    assert_eq!(rows[0]["platform"], json!("youtube"));
    // A key never appears in the payload.
    assert!(rows.iter().all(|row| row["key_present"] == json!(false)));

    let error = registry()
        .dispatch(&context, "xpst_stream", &json!({"platform": "nope"}))
        .expect_err("refused");
    assert_eq!(error.code(), "reported_error");
    let body: Value = serde_json::from_str(&error.to_string()).expect("the body is the payload");
    assert_eq!(body["ok"], json!(false));
    assert_eq!(body["code"], json!("unknown_platform"));
    assert_eq!(body["platform"], Value::Null);
    assert!(body["known"].as_array().expect("known").len() == 10);
}

/// `xpst_analytics`'s `live: true` half runs a real collection against every
/// platform API, so it answers a typed `not_implemented` rather than an empty
/// report — "not collected" and "collected nothing" are different answers.
#[test]
fn analytics_refuses_the_live_half() {
    let dir = profile("analytics_live");
    let error = registry()
        .dispatch(&ctx(&dir), "xpst_analytics", &json!({"live": true}))
        .expect_err("the network half is not ported");
    assert_eq!(error.code(), "not_implemented");
    assert!(error.to_string().contains("xpst_analytics"));
}

/// A fresh profile has no ledger and no state: the report is the honest empty
/// document — five platform rows, `totals: null` everywhere, `snapshot_count: 0`
/// — and the store is created rather than reported missing.
#[test]
fn analytics_answers_an_empty_report_on_a_fresh_profile() {
    let dir = profile("analytics_empty");
    let payload = call(&ctx(&dir), "xpst_analytics", json!({}));
    assert_eq!(payload["live"], json!(false));
    assert_eq!(payload["snapshot_count"], json!(0));
    assert_eq!(payload["platforms"], json!({}));
    assert_eq!(payload["posts"], json!([]));
    let report = &payload["outcome_report"];
    assert_eq!(report["live"], json!(false));
    assert_eq!(report["data_source"], Value::Null);
    let platforms = report["platforms"].as_object().expect("platforms");
    assert_eq!(
        platforms.len(),
        5,
        "every platform renders, even with nothing"
    );
    for name in ["youtube", "x", "instagram", "tiktok", "threads"] {
        assert_eq!(platforms[name]["totals"], Value::Null, "{name} totals");
        assert_eq!(platforms[name]["has_data"], json!(false), "{name} has_data");
        assert_eq!(
            platforms[name]["note"],
            json!("No posts recorded for this platform"),
            "{name} note"
        );
        assert_eq!(
            platforms[name]["collection_status"],
            json!("unknown"),
            "{name} status is unknown, never ok"
        );
    }
    assert_eq!(
        report["diagnostics"]["unknown_collection_status_platforms"],
        json!(["youtube", "x", "instagram", "tiktok", "threads"])
    );
}

/// A row recorded during a FAILED collection stays visible but out of the
/// totals, and the platform's aggregate keeps the `null`-not-zero contract.
#[test]
fn analytics_excludes_a_failed_collection_from_the_totals() {
    let dir = profile("analytics_failed_row");
    std::fs::write(
        dir.join("state.json"),
        r#"{"posted_videos": {"vid-1": {"posted_to": {"x": {"id": "x-1", "timestamp": "2026-11-10T01:00:00+00:00"}}}}}"#,
    )
    .expect("seed state");
    let store = xpst_store::Store::open(dir.join("analytics.db")).expect("open ledger");
    store
        .record_snapshots(&[
            xpst_store::NewSnapshot::new("x", "x-1", "2026-11-20T00:00:00+00:00")
                .metrics(xpst_store::Metrics {
                    views: Some(100),
                    likes: Some(10),
                    ..xpst_store::Metrics::default()
                })
                .status(xpst_store::CollectionStatus::Failed),
        ])
        .expect("seed snapshot");

    let payload = call(&ctx(&dir), "xpst_analytics", json!({}));
    let x = &payload["outcome_report"]["platforms"]["x"];
    assert_eq!(x["totals"], Value::Null, "a failed capture is not a total");
    assert_eq!(x["has_data"], json!(false));
    let outcomes = x["outcomes"].as_array().expect("outcomes");
    assert_eq!(outcomes.len(), 1);
    assert_eq!(outcomes[0]["metrics"]["views"], json!(100), "still visible");
    assert_eq!(outcomes[0]["counted_in_totals"], json!(false));
    assert_eq!(outcomes[0]["collection_status"], json!("failed"));
    // The aggregate keeps the missing metric missing rather than inventing 0.
    assert_eq!(payload["platforms"]["x"]["views"], json!(100));
    assert_eq!(payload["platforms"]["x"]["comments"], Value::Null);
}
