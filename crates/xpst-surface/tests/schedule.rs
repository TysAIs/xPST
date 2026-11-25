//! The schedule write half's own guards: the branches the wire fixture cannot
//! reach, and the store round-trip.
//!
//! `crates/xpst-cli/tests/mcp_stdio.rs` pins every `tools/call` body the
//! `mutations` scenario can produce — the happy adds, the refusals, the cancel
//! verdicts and the pause/resume documents. What it cannot pin without a
//! 100 kB argument is `ScheduleManager.add`'s caption caps, and it cannot show
//! that the entry it reports is the entry the store holds.
//!
//! Nothing here touches the network or a provider: every write is a local JSON
//! file under a scratch profile.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use xpst_surface::config_view::ConfigView;
use xpst_surface::schedule::{add, cancel, pause, resume, AddFailure, MAX_CAPTION_LENGTH};

fn profile(name: &str) -> PathBuf {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("schedule-{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch profile");
    dir
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

fn view(dir: &Path) -> ConfigView {
    let document = xpst_config::yaml::from_str("{}").expect("an empty config document");
    ConfigView::load(&document, dir, &env).expect("the default config view")
}

/// The `{"ok": false, "error": …}` document a `ValueError` from
/// `ScheduleManager.add` becomes.
fn reported(failure: AddFailure) -> Value {
    match failure {
        AddFailure::Reported(message) => {
            serde_json::from_str(&message).expect("a Reported failure is a JSON document")
        }
        AddFailure::Raised(message) => {
            panic!("expected a reported document, got `Error: {message}`")
        }
    }
}

#[test]
fn a_caption_over_the_cap_is_the_managers_own_sentence() {
    // The route refuses an over-limit caption PER DESTINATION, so on a platform
    // with a caption limit the `NO_DESTINATIONS` document wins and
    // `ScheduleManager.add`'s own cap is never reached. YouTube declares no
    // caption limit, which is the one path that gets there — a real finding
    // about the shipped handler, not a test convenience.
    let dir = profile("caption-cap");
    std::fs::write(dir.join("clip.mp4"), "x".repeat(64)).expect("a media file");
    let caption = "x".repeat(MAX_CAPTION_LENGTH + 1);
    let failure = add(
        &view(&dir),
        &dir,
        &env,
        &json!({
            "video_path": dir.join("clip.mp4").display().to_string(),
            "caption": caption,
            "scheduled_time": "2026-12-01T09:30:00",
            "platforms": ["youtube"],
        }),
    )
    .expect_err("an oversized caption is refused");
    let document = reported(failure);
    assert_eq!(document["ok"], json!(false));
    assert_eq!(
        document["error"],
        json!(
            "Caption is 100,001 characters; the maximum is 100,000. Shorten the caption before \
             scheduling."
        )
    );
    // Nothing was written: the refusal happens before the store append.
    assert!(!dir.join("schedule.json").exists());
}

#[test]
fn an_override_over_the_cap_names_the_platform() {
    // The route validates an override only for a destination IN the route, so a
    // text post (route: x) with a YouTube override reaches `manager.add`'s
    // normaliser — which is where the cap lives.
    let dir = profile("override-cap");
    let failure = add(
        &view(&dir),
        &dir,
        &env,
        &json!({
            "caption": "c",
            "scheduled_time": "2026-12-01T09:30:00",
            "overrides": {"youtube": "y".repeat(MAX_CAPTION_LENGTH + 1)},
        }),
    )
    .expect_err("an oversized override is refused");
    let document = reported(failure);
    assert_eq!(document["ok"], json!(false));
    assert_eq!(
        document["error"],
        json!("Caption override for 'youtube' is 100,001 characters; the maximum is 100,000.")
    );
}

#[test]
fn an_unknown_repeat_rule_is_refused_before_anything_is_written() {
    let dir = profile("repeat-rule");
    let failure = add(
        &view(&dir),
        &dir,
        &env,
        &json!({
            "caption": "c",
            "scheduled_time": "2026-12-01T09:30:00",
            "repeat_rule": "hourly",
        }),
    )
    .expect_err("an unknown repeat rule is refused");
    let document = reported(failure);
    assert_eq!(document["ok"], json!(false));
    assert_eq!(
        document["error"],
        json!("Invalid repeat_rule: 'hourly'. Must be one of: None, 'daily', 'weekly', 'monthly'")
    );
}

#[test]
fn a_text_entry_round_trips_through_the_store() {
    let dir = profile("round-trip");
    let payload = add(
        &view(&dir),
        &dir,
        &env,
        &json!({
            "caption": "parity text",
            // An explicit offset keeps the stored instant independent of the
            // machine's zone; the zone only affects `timezone` and the local view.
            "scheduled_time": "2026-12-01T09:30:00+00:00",
            "overrides": {"x": "short", "youtube": "long"},
        }),
    )
    .expect("a text entry is scheduled");

    let entry = &payload["scheduled"];
    assert_eq!(entry["video_path"], json!(""));
    assert_eq!(entry["content_type"], json!("text"));
    assert_eq!(entry["scheduled_time"], json!("2026-12-01T09:30:00+00:00"));
    assert_eq!(entry["status"], json!("pending"));
    assert_eq!(entry["repeat_rule"], Value::Null);
    // The caller's override order survives into the entry.
    let captions: Vec<&String> = entry["per_platform_captions"]
        .as_object()
        .expect("an override map")
        .keys()
        .collect();
    assert_eq!(captions, vec!["x", "youtube"]);
    // The route plan is the same document the CLI's `preflight` serves.
    assert_eq!(payload["route_plan"]["content_type"], json!("text"));
    assert_eq!(payload["route_plan"]["posting_destinations"], json!(["x"]));

    // And the store holds exactly the entry that was reported.
    let listing = xpst_state::schedule::listing(&dir);
    assert_eq!(listing.entries.len(), 1);
    assert_eq!(listing.entries[0]["id"], entry["id"]);
    assert_eq!(listing.entries[0]["caption"], json!("parity text"));
    assert!(
        listing.entries[0]["scheduled_time_local"].is_string(),
        "the listing renders a local view"
    );
}

#[test]
fn cancel_reports_the_verdict_and_removes_only_what_it_found() {
    let dir = profile("cancel");
    add(
        &view(&dir),
        &dir,
        &env,
        &json!({"caption": "c", "scheduled_time": "2026-12-01T09:30:00+00:00"}),
    )
    .expect("a text entry is scheduled");
    let entry_id = xpst_state::schedule::listing(&dir).entries[0]["id"].clone();

    // An unknown id is a failure, never a silent success.
    let missing = cancel(&dir, &json!("nope"), false);
    assert_eq!(missing["ok"], json!(false));
    assert_eq!(missing["found"], json!(false));
    assert_eq!(missing["cancelled"], json!(false));
    assert_eq!(missing["entry"], Value::Null);
    assert_eq!(missing["error"]["code"], json!("POST_NOT_FOUND"));
    assert_eq!(
        missing["error"]["message"],
        json!("Scheduled post not found: nope")
    );

    // A dry run reports the entry without touching the store.
    let dry = cancel(&dir, &entry_id, true);
    assert_eq!(dry["ok"], json!(true));
    assert_eq!(dry["dry_run"], json!(true));
    assert_eq!(dry["cancelled"], json!(false));
    assert_eq!(xpst_state::schedule::listing(&dir).entries.len(), 1);

    // The real cancel removes it, and the second attempt is POST_NOT_FOUND.
    let removed = cancel(&dir, &entry_id, false);
    assert_eq!(removed["cancelled"], json!(true));
    assert_eq!(removed["entry"]["id"], entry_id);
    assert!(xpst_state::schedule::listing(&dir).entries.is_empty());
    assert_eq!(cancel(&dir, &entry_id, false)["ok"], json!(false));
}

#[test]
fn pause_and_resume_report_whether_they_changed_anything() {
    let dir = profile("pause");
    let now = jiff::Timestamp::now();

    let first = pause(&dir, "parity pause", now).expect("the marker is written");
    assert_eq!(first["paused"], json!(true));
    assert_eq!(first["already_paused"], json!(false));
    assert_eq!(first["reason"], json!("parity pause"));
    assert!(first["paused_at"].is_string());

    // A second pause keeps the FIRST reason: the marker is the record.
    let second = pause(&dir, "a different reason", now).expect("the marker is read");
    assert_eq!(second["already_paused"], json!(true));
    assert_eq!(second["reason"], json!("parity pause"));

    assert_eq!(
        resume(&dir).expect("the marker is removed"),
        json!({"paused": false, "was_paused": true})
    );
    assert_eq!(
        resume(&dir).expect("nothing to remove"),
        json!({"paused": false, "was_paused": false})
    );
}
