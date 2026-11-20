//! `scheduled_time_local` renders the same instant the Python engine renders.
//!
//! The schedule listing used to be rendered with `chrono`/`chrono-tz`; both are
//! unmaintained (RUSTSEC-2026-0543/-0555) and one of them carries the IANA
//! database, so "unmaintained" there means the zone rules stop tracking reality.
//! The replacement is `jiff`, and this file is the contract it has to meet.
//!
//! The expectations were **captured from the shipped Python engine** —
//! `ScheduleManager.local_time(entry).isoformat()`, the function
//! `_with_local_view()` calls — not from the previous Rust implementation,
//! because that implementation had a defect this migration fixes: it formatted
//! `"{stamp}+{offset}"` and chrono's offset already carries its own sign, so
//! every value came out as `2026-11-20T04:00:00+-05:00` / `...++00:00`. Python
//! emits `2026-11-20T04:00:00-05:00`, which is the value below.
//!
//! Only zone-explicit cases are in the fixture: the machine's own zone is not a
//! property of this repo, so local-zone behaviour is asserted separately, on
//! its shape, at the bottom of this file.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use xpst_mcp::{StorePaths, ToolContext};

const CASES: &str = include_str!("fixtures/scheduled_time_local.json");

fn profile(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create fixture profile");
    dir
}

/// The rendered value for one stored entry, through the public tool.
fn render(dir: &Path, scheduled_time: &str, timezone: Option<&str>) -> Value {
    let mut entry = serde_json::Map::new();
    entry.insert("id".into(), json!("case"));
    entry.insert("status".into(), json!("pending"));
    entry.insert("scheduled_time".into(), json!(scheduled_time));
    if let Some(zone) = timezone {
        entry.insert("timezone".into(), json!(zone));
    }
    std::fs::write(
        dir.join("schedule.json"),
        serde_json::to_string(&json!([Value::Object(entry)])).expect("serialise"),
    )
    .expect("write schedule");
    let ctx = ToolContext::new(StorePaths::at(dir));
    let payload = xpst_mcp::registry::registry()
        .dispatch(&ctx, "xpst_schedule_list", &json!({}))
        .expect("schedule list");
    payload["schedules"][0]["scheduled_time_local"].clone()
}

#[test]
fn the_fixture_is_not_empty_and_self_describing() {
    let cases: Value = serde_json::from_str(CASES).expect("fixture json");
    let cases = cases.as_array().expect("array of cases");
    assert_eq!(cases.len(), 25, "the rendering corpus changed size");
    for case in cases {
        assert!(case["id"].as_str().is_some_and(|id| !id.is_empty()));
        assert!(case.get("expected").is_some());
    }
}

#[test]
fn every_rendered_stamp_matches_the_python_engine() {
    let dir = profile("schedule_local_time");
    let cases: Value = serde_json::from_str(CASES).expect("fixture json");
    for case in cases.as_array().expect("array of cases") {
        let id = case["id"].as_str().expect("id");
        let rendered = render(
            &dir,
            case["scheduled_time"].as_str().expect("scheduled_time"),
            case["timezone"].as_str(),
        );
        assert_eq!(
            rendered, case["expected"],
            "{id}: rendering changed for {} in {:?}",
            case["scheduled_time"], case["timezone"]
        );
    }
}

#[test]
fn a_stamp_is_never_double_signed() {
    // The defect this migration fixed. Asserted on its own so a regression
    // names itself instead of hiding inside a table diff.
    let dir = profile("schedule_local_time_signs");
    let rendered = render(&dir, "2026-11-20T09:00:00+00:00", Some("America/New_York"));
    let text = rendered.as_str().expect("a rendered stamp");
    assert!(
        !text.contains("+-") && !text.contains("++"),
        "an offset must carry exactly one sign: {text}"
    );
    assert!(text.ends_with("-05:00"), "unexpected offset: {text}");
}

#[test]
fn a_naive_stamp_is_read_as_local_wall_clock() {
    // Machine-local behaviour cannot be pinned to a literal string without
    // pinning the runner's zone. What must hold anywhere: the wall clock is
    // preserved verbatim, and the value carries the machine's own offset rather
    // than being read as UTC — a CI runner in Etc/UTC legitimately renders
    // "+00:00", so the assertion is that the two agree, not that either is
    // non-zero.
    let dir = profile("schedule_local_time_naive");
    let rendered = render(&dir, "2026-11-20T09:00:00", None);
    let text = rendered.as_str().expect("a rendered stamp");
    assert!(
        text.starts_with("2026-11-20T09:00:00"),
        "local wall clock was not preserved: {text}"
    );
    let instant: jiff::Timestamp = "2026-11-20T09:00:00Z".parse().expect("instant");
    let machine_offset_is_utc = instant
        .to_zoned_datetime(jiff::tz::TimeZone::system())
        .offset()
        .seconds()
        == 0;
    assert_eq!(
        text.ends_with("+00:00"),
        machine_offset_is_utc,
        "the rendering must use the machine's zone, not UTC: {text}"
    );
}

#[test]
fn an_unknown_zone_name_falls_back_to_the_local_zone() {
    // A stored zone this build cannot look up must not fail the listing, and
    // must not silently render UTC either: the Python engine falls back to the
    // local zone, and so does this.
    let dir = profile("schedule_local_time_unknown_zone");
    let unknown = render(&dir, "2026-11-20T09:00:00+00:00", Some("Mars/Olympus"));
    let no_zone = render(&dir, "2026-11-20T09:00:00+00:00", None);
    assert_eq!(unknown, no_zone);
    assert!(unknown.is_string(), "the fallback must still render");
}
