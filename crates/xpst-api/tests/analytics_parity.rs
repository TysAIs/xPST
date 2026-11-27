//! Byte parity for the six analytics-backed read routes.
//!
//! The fixture under `tests/fixtures/analytics/` is captured by
//! `xtask/scripts/capture_analytics_routes.py` from the REAL Python dashboard:
//! a fictional `state.json` plus an `analytics.db` created by Python's
//! `AnalyticsStore`, driven through `TestClient` with the clock pinned to
//! `2026-11-20T12:00:00Z`. `expected/<route>.json` is the response body
//! verbatim — key order and separators included.
//!
//! This test copies the fixture into a scratch directory (the store opens and
//! may touch the file), serves the same requests from the Rust router with the
//! same pinned clock, and compares the bodies byte for byte. A shape-only
//! comparison would pass while a client that parses the body still saw a
//! different document, which is exactly what the CLI `--json` parity work
//! already refuses to accept.

mod common;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use common::send;
use serde_json::Value;
use xpst_api::{AppState, AuthConfig};

const FIXTURE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/analytics");

/// Copy the checked-in fixture into a scratch directory.
///
/// The store opens the ledger (and would create it if absent), so the test must
/// never point at the checked-in file.
fn fixture_copy(name: &str) -> PathBuf {
    let dir = common::sandbox(name);
    // The fixture keeps the state document under a committable name because
    // `state.json` is gitignored (it is runtime state); the engine reads
    // `state.json`, so the copy restores the name.
    std::fs::copy(
        Path::new(FIXTURE).join("state.fixture.json"),
        dir.join("state.json"),
    )
    .expect("copy state.fixture.json");
    std::fs::copy(
        Path::new(FIXTURE).join("analytics.db"),
        dir.join("analytics.db"),
    )
    .expect("copy analytics.db");
    dir
}

fn meta() -> Value {
    let text = std::fs::read_to_string(Path::new(FIXTURE).join("meta.json"))
        .expect("meta.json is checked in");
    serde_json::from_str(&text).expect("meta.json is valid JSON")
}

fn expected(name: &str) -> Vec<u8> {
    std::fs::read(
        Path::new(FIXTURE)
            .join("expected")
            .join(format!("{name}.json")),
    )
    .unwrap_or_else(|error| panic!("expected/{name}.json: {error}"))
}

/// The standard test state with the fixture's pinned clock.
fn pinned_state(dir: &Path, now_ms: i64) -> Arc<AppState> {
    Arc::new(
        AppState::new(dir)
            .with_auth(
                AuthConfig::new()
                    .with_token("test-token")
                    .with_config_dir(dir),
            )
            .with_clock(move || now_ms),
    )
}

async fn get_bytes(state: Arc<AppState>, path: &str) -> (StatusCode, Vec<u8>) {
    let request = Request::builder()
        .method("GET")
        .uri(path)
        .header("authorization", "Bearer test-token")
        .body(Body::empty())
        .expect("request builds");
    send(state, request).await
}

/// Every captured route, byte for byte.
#[tokio::test]
async fn every_analytics_route_matches_the_python_body_byte_for_byte() {
    let meta = meta();
    let now_ms = meta["fixed_now_ms"].as_i64().expect("pinned clock");
    let routes = meta["routes"].as_object().expect("routes map");
    let statuses = meta["statuses"].as_object().expect("statuses map");
    let dir = fixture_copy("analytics-parity");
    let state = pinned_state(&dir, now_ms);

    let mut checked = 0;
    for (name, spec) in routes {
        let path = spec["path"].as_str().expect("route path");
        let (status, body) = get_bytes(state.clone(), path).await;
        let want_status = statuses[name].as_u64().expect("captured status") as u16;
        assert_eq!(
            status.as_u16(),
            want_status,
            "{name} ({path}) status differs from the capture"
        );
        let want = expected(name);
        assert_eq!(
            String::from_utf8_lossy(&body),
            String::from_utf8_lossy(&want),
            "{name} ({path}) body differs from the Python capture"
        );
        checked += 1;
    }
    assert_eq!(checked, 7, "expected the seven captured routes");
}

/// The checked-in ledger is the one the capture produced.
#[test]
fn the_checked_in_ledger_is_the_captured_one() {
    use sha2::{Digest, Sha256};

    let meta = meta();
    for file in ["state.fixture.json", "analytics.db"] {
        let bytes = std::fs::read(Path::new(FIXTURE).join(file)).expect("fixture file");
        let digest = Sha256::digest(&bytes);
        let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        assert_eq!(
            hex,
            meta["sha256"][file].as_str().expect("captured digest"),
            "{file} is not the file the capture wrote"
        );
    }
}

/// The Rust store opens a Python-created `analytics.db` and reads it.
///
/// This is the schema-compatibility claim: no migration, no re-creation, the
/// same rows Python wrote.
#[test]
fn the_store_reads_a_python_created_ledger() {
    let dir = fixture_copy("analytics-store");
    let store = xpst_store::Store::open(dir.join("analytics.db")).expect("open the captured db");

    assert_eq!(
        store.applied_migrations(),
        &[] as &[&'static str],
        "a Python-created ledger needs no migration"
    );
    assert_eq!(store.snapshot_count().expect("count"), 7);

    let totals = store.platform_totals().expect("platform totals");
    let names: Vec<&str> = totals.iter().map(|row| row.platform.as_str()).collect();
    assert_eq!(names, vec!["tiktok", "x", "youtube"]);
    let youtube = totals
        .iter()
        .find(|row| row.platform == "youtube")
        .expect("youtube totals");
    assert_eq!(youtube.views, Some(210));
    assert_eq!(youtube.likes, Some(17));
    // A metric no latest snapshot reported stays missing, never a zero.
    assert_eq!(youtube.reposts, None);

    let runs = store.latest_collection_runs().expect("collection runs");
    let names: Vec<&str> = runs.keys().map(String::as_str).collect();
    assert_eq!(names, vec!["instagram", "x", "youtube"]);
    assert_eq!(runs["x"].status.as_str(), "failed");

    let history = store
        .history("youtube", "fic-yt-0001", 100)
        .expect("history");
    assert_eq!(history.len(), 2);
    assert_eq!(history[0].captured_at, "2026-11-20T00:00:00+00:00");
}

/// `live=1` is the network tranche: a typed refusal, never a recorded report
/// labelled live.
#[tokio::test]
async fn a_live_outcome_request_is_refused_with_a_typed_501() {
    let dir = fixture_copy("analytics-live");
    let state = pinned_state(&dir, 1_795_176_000_000);
    let (status, body) = get_bytes(state, "/api/analytics/outcomes?live=1").await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    let body: Value = serde_json::from_slice(&body).expect("JSON body");
    assert_eq!(
        body["code"],
        serde_json::json!("LIVE_COLLECTION_NOT_IMPLEMENTED")
    );
    assert_eq!(body["route"], serde_json::json!("api_analytics_outcomes"));
    assert!(body["reason"]
        .as_str()
        .unwrap_or_default()
        .contains("network tranche"));
}

/// `live=0` is the default and is served.
#[tokio::test]
async fn the_default_outcome_request_is_the_recorded_report() {
    let dir = fixture_copy("analytics-recorded");
    let state = pinned_state(&dir, 1_795_176_000_000);
    let (status, body) = get_bytes(state, "/api/analytics/outcomes?live=0").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, expected("analytics_outcomes"));
}

/// An unknown video id is Python's `404 {"detail": …}`, not an empty document.
#[tokio::test]
async fn an_unknown_video_is_a_404_with_pythons_detail() {
    let dir = fixture_copy("analytics-unknown");
    let state = pinned_state(&dir, 1_795_176_000_000);
    let (status, body) = get_bytes(state, "/api/videos/fic-vid-9999").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body, expected("video_detail_unknown"));
}
