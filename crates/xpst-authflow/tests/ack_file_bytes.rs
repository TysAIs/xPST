//! The acknowledgement file must be byte-identical to the one the shipped
//! Python engine writes.
//!
//! `src/xpst/auth_path_risk.py::record_acknowledgement` writes
//! `ban_risk_acknowledgements.json` with `json.dumps(payload, indent=2,
//! sort_keys=True)`, so every object is alphabetical at every level. A Rust
//! `serde_json::to_vec_pretty` of the structs renders declaration order
//! instead: the same document, a different key order. A structural comparison
//! cannot see that difference and `cmp` reports it as a byte difference, so
//! this test compares the file bytes.
//!
//! `tests/fixtures/ban_risk_acknowledgements.json` is captured by
//! `xtask/scripts/capture_ban_risk_ack.py`, which drives the real Python
//! function. Re-running it must reproduce the committed fixture byte for byte.

use serde_json::Value;
use tempfile::TempDir;
use xpst_authflow::risk::{record_acknowledgement, ACK_FILE_NAME};
use xpst_authflow::{AuthMode, AuthPlatform};

const FIXTURE: &str = include_str!("fixtures/ban_risk_acknowledgements.json");

/// A fixture loop with no count guard is a vacuous test: emptying the array
/// would make every assertion below pass. The capture script refuses to write
/// an empty fixture; this refuses to accept one.
const MIN_CASES: usize = 5;

fn platform(name: &str) -> AuthPlatform {
    AuthPlatform::ALL
        .into_iter()
        .find(|candidate| candidate.as_str() == name)
        .unwrap_or_else(|| panic!("fixture names an unknown platform: {name}"))
}

fn auth_mode(name: &str) -> AuthMode {
    match name {
        "oauth_loopback" => AuthMode::OauthLoopback,
        "api_v2" => AuthMode::ApiV2,
        "graph_api" => AuthMode::GraphApi,
        "session" => AuthMode::Session,
        "cookies" => AuthMode::Cookies,
        "app_password" => AuthMode::AppPassword,
        "personal_access_token" => AuthMode::PersonalAccessToken,
        other => panic!("fixture names an unknown auth mode: {other}"),
    }
}

#[test]
fn the_written_file_is_byte_identical_to_the_python_engine() {
    let fixture: Value = serde_json::from_str(FIXTURE).expect("fixture is JSON");
    let cases = fixture["cases"].as_array().expect("cases array");
    assert!(
        cases.len() >= MIN_CASES,
        "fixture has {} cases, expected at least {MIN_CASES}",
        cases.len()
    );

    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let steps = case["steps"].as_array().expect("steps array");
        assert!(
            !steps.is_empty(),
            "{name}: a case with no steps proves nothing"
        );

        let dir = TempDir::new().expect("tempdir");
        let path = dir.path().join(ACK_FILE_NAME);
        if let Some(seed) = case["seed"].as_str() {
            std::fs::write(&path, seed).expect("seed the acknowledgement file");
        }

        for step in steps {
            record_acknowledgement(
                dir.path(),
                platform(step["platform"].as_str().expect("platform")),
                auth_mode(step["auth_mode"].as_str().expect("auth_mode")),
                step["surface"].as_str().expect("surface"),
                step["acknowledged_at"].as_str().expect("acknowledged_at"),
            )
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        }

        let written = std::fs::read_to_string(&path).expect("read the written file back");
        let expected = case["expected"].as_str().expect("expected bytes");
        assert_eq!(
            written, expected,
            "{name}: the Rust writer and the Python engine disagree"
        );
    }
}

/// The property the fixture pins, asserted without the fixture: every object in
/// the written document is alphabetical at every level, and the file ends with
/// exactly one newline. This is what `sort_keys=True` means, and it is the part
/// a future refactor is most likely to drop.
#[test]
fn every_object_in_the_written_file_is_sorted() {
    let dir = TempDir::new().expect("tempdir");
    record_acknowledgement(
        dir.path(),
        AuthPlatform::Instagram,
        AuthMode::Session,
        "xpst connect instagram",
        "2026-11-17T00:00:00+0000",
    )
    .expect("record");
    record_acknowledgement(
        dir.path(),
        AuthPlatform::X,
        AuthMode::Cookies,
        "xpst connect x",
        "2026-11-17T00:00:01+0000",
    )
    .expect("record");

    let written = std::fs::read_to_string(dir.path().join(ACK_FILE_NAME)).expect("read back");
    assert!(written.ends_with("}\n"), "file must end with one newline");
    assert!(!written.ends_with("\n\n"), "file must end with ONE newline");

    let document: Value = serde_json::from_str(&written).expect("valid JSON");
    assert_sorted(&document, "$");
}

fn assert_sorted(value: &Value, path: &str) {
    match value {
        Value::Object(map) => {
            let keys: Vec<&String> = map.keys().collect();
            let mut sorted = keys.clone();
            sorted.sort();
            assert_eq!(keys, sorted, "{path}: keys are not alphabetical");
            for (key, child) in map {
                assert_sorted(child, &format!("{path}.{key}"));
            }
        }
        Value::Array(items) => {
            for (index, child) in items.iter().enumerate() {
                assert_sorted(child, &format!("{path}[{index}]"));
            }
        }
        _ => {}
    }
}
