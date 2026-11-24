//! The setup transaction's own guards: the on-disk bytes, the recovery copy,
//! the reset, and the selection normaliser.
//!
//! The MCP wire is pinned by `crates/xpst-cli/tests/mcp_stdio.rs` against a
//! capture from the shipped Python server. What that fixture cannot show is the
//! *file*: it masks the clock and the transaction id, and it never corrupts the
//! primary document. These tests cover the parts a byte comparison of the wire
//! cannot reach.

use std::path::PathBuf;

use serde_json::{json, Value};
use xpst_surface::setup_transaction::{
    normalize_role_capabilities, ResumeRequest, SetupError, SetupTransactionService,
    BACKUP_FILENAME, TRANSACTION_FILENAME,
};

fn profile(name: &str) -> PathBuf {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("setup-{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch profile");
    dir
}

#[test]
fn a_first_save_writes_a_recovery_copy_and_sorted_bytes() {
    let dir = profile("first-save");
    let mut service = SetupTransactionService::new(&dir);
    let transaction = service.start(None).expect("start");

    let primary = dir.join(TRANSACTION_FILENAME);
    let backup = dir.join(BACKUP_FILENAME);
    assert!(primary.is_file(), "the transaction file was not written");
    assert!(
        backup.is_file(),
        "a first write must still leave a recovery point"
    );

    let bytes = std::fs::read_to_string(&primary).expect("read the transaction");
    // `json.dumps(..., sort_keys=True, indent=2)` — sorted at every level, two
    // spaces, no trailing newline. `completion` is the first key alphabetically.
    assert!(
        bytes.starts_with("{\n  \"completion\": {\n"),
        "the document is not sorted-and-indented: {:?}",
        &bytes[..bytes.len().min(80)]
    );
    assert!(
        !bytes.ends_with('\n'),
        "Python's json.dumps adds no newline"
    );

    // The file is the document, not a rendering of it.
    let parsed: Value = serde_json::from_str(&bytes).expect("valid JSON");
    assert_eq!(parsed, transaction);
    assert_eq!(
        parsed["transaction_id"].as_str().map(str::len),
        Some(36),
        "the identifier is a hyphenated v4 UUID"
    );
}

#[test]
fn a_corrupt_primary_recovers_from_the_backup_and_says_so() {
    let dir = profile("recover");
    let mut service = SetupTransactionService::new(&dir);
    let started = service.start(None).expect("start");
    let id = started["transaction_id"].clone();

    std::fs::write(dir.join(TRANSACTION_FILENAME), b"{ not a transaction").expect("corrupt");

    let recovered = service.status(None).expect("status").expect("recovered");
    assert_eq!(recovered["transaction_id"], id, "the backup was not used");
    let codes: Vec<&str> = recovered["errors"]
        .as_array()
        .expect("errors")
        .iter()
        .filter_map(|error| error["code"].as_str())
        .collect();
    assert!(
        codes.contains(&"STATE_RECOVERED"),
        "a recovery must be recorded, not silent: {codes:?}"
    );

    // The primary was restored through the atomic path, so the next load is
    // clean and does not report a second recovery.
    let again = service.status(None).expect("status").expect("present");
    let codes: Vec<&str> = again["errors"]
        .as_array()
        .expect("errors")
        .iter()
        .filter_map(|error| error["code"].as_str())
        .collect();
    assert_eq!(
        codes
            .iter()
            .filter(|code| **code == "STATE_RECOVERED")
            .count(),
        1,
        "the recovery marker must not accumulate: {codes:?}"
    );
}

#[test]
fn a_missing_file_is_not_a_corrupt_one() {
    let dir = profile("missing");
    let mut service = SetupTransactionService::new(&dir);
    let status = service.status(None).expect("status");
    assert!(status.is_none(), "a fresh profile has no transaction");
    // And nothing was created by looking.
    assert!(!dir.join(TRANSACTION_FILENAME).exists());
    assert!(!dir.join(BACKUP_FILENAME).exists());
}

#[test]
fn reset_removes_the_document_and_every_recovery_copy() {
    let dir = profile("reset");
    let mut service = SetupTransactionService::new(&dir);
    service.start(None).expect("start");
    // A leftover temp file from an interrupted write is cleaned up too.
    std::fs::write(
        dir.join(format!(".{TRANSACTION_FILENAME}.deadbeef.tmp")),
        b"{}",
    )
    .expect("temp");

    let payload = service.reset(None).expect("reset");
    assert_eq!(payload["state"], json!("reset"));
    assert_eq!(payload["schema_version"], json!(1));
    assert_eq!(payload["pending_human_actions"], json!([]));
    assert!(!dir.join(TRANSACTION_FILENAME).exists());
    assert!(!dir.join(BACKUP_FILENAME).exists());
    assert!(
        !dir.join(format!(".{TRANSACTION_FILENAME}.deadbeef.tmp"))
            .exists(),
        "the reset must sweep interrupted writes"
    );
}

#[test]
fn completion_is_derived_from_readiness_not_from_the_saved_state() {
    let dir = profile("completion");
    let mut service = SetupTransactionService::new(&dir);
    service.start(None).expect("start");

    let partial = service
        .resume(ResumeRequest {
            readiness: Some(
                &json!({"sources": [{"capability": "tiktok", "verified": true, "live_ready": true}]}),
            ),
            ..Default::default()
        })
        .expect("resume");
    assert_eq!(partial["state"], json!("in_progress"));
    assert_eq!(partial["completion"]["complete"], json!(false));

    let complete = service
        .resume(ResumeRequest {
            readiness: Some(&json!({"video_destinations": {"youtube": true}})),
            ..Default::default()
        })
        .expect("resume");
    assert_eq!(complete["state"], json!("completed"));
    assert_eq!(complete["completion"]["complete"], json!(true));
    assert_eq!(complete["completion"]["resumable"], json!(false));

    // A resume on a completed transaction is the early return: no error, no
    // step touched.
    let after = service
        .resume(ResumeRequest {
            step_id: Some(&json!("nope")),
            step_state: Some(&json!("verified")),
            ..Default::default()
        })
        .expect("a completed transaction is not an error");
    assert_eq!(after["state"], json!("completed"));
}

#[test]
fn a_readiness_entry_for_an_unselected_capability_is_recorded_not_merged() {
    let dir = profile("unselected");
    let mut service = SetupTransactionService::new(&dir);
    service.start(None).expect("start");
    let payload = service
        .resume(ResumeRequest {
            readiness: Some(&json!({"sources": ["instagram"]})),
            ..Default::default()
        })
        .expect("resume");
    let codes: Vec<&str> = payload["errors"]
        .as_array()
        .expect("errors")
        .iter()
        .filter_map(|error| error["code"].as_str())
        .collect();
    assert!(codes.contains(&"READINESS_NOT_SELECTED"), "{codes:?}");
    assert_eq!(
        payload["readiness"]["sources"],
        json!([]),
        "an unselected capability must not land in readiness"
    );
}

#[test]
fn the_selection_normaliser_matches_python_shapes() {
    // Absent → the five built-in role-capabilities.
    let default = normalize_role_capabilities(None).expect("default");
    assert_eq!(default.len(), 5);
    assert_eq!(
        default[0],
        json!({"role": "source", "capability": "tiktok"})
    );

    // `role:capability` notation, and the friendly UI shape, dedupe together.
    let mixed = normalize_role_capabilities(Some(&json!([
        "source:tiktok",
        {"video_destination": ["youtube", "x"]},
        {"role": "source", "capability": "tiktok"}
    ])))
    .expect("mixed");
    assert_eq!(
        mixed,
        vec![
            json!({"role": "source", "capability": "tiktok"}),
            json!({"role": "video_destination", "capability": "youtube"}),
            json!({"role": "video_destination", "capability": "x"}),
        ]
    );

    // A secret-looking identifier is refused, not stored.
    let secret = normalize_role_capabilities(Some(&json!([
        {"role": "source", "capability": "my_token"}
    ])))
    .expect_err("a secret word must be refused");
    assert!(matches!(secret, SetupError::Invalid(_)));

    // An unsupported role is refused.
    let role = normalize_role_capabilities(Some(&json!([
        {"role": "nope", "capability": "tiktok"}
    ])))
    .expect_err("an unknown role must be refused");
    assert!(role.message().contains("Unsupported setup role"));

    // An empty selection is refused rather than defaulted.
    let empty = normalize_role_capabilities(Some(&json!([]))).expect_err("empty");
    assert!(empty.message().contains("At least one role-capability"));

    // A bare string is iterated character by character in Python, so the first
    // character has no `:` and the notation error is what the caller sees.
    let bare = normalize_role_capabilities(Some(&json!("source:tiktok"))).expect_err("bare");
    assert!(bare.message().contains("role:capability"));

    // A number is not iterable at all: a `TypeError`, not a request refusal.
    let number = normalize_role_capabilities(Some(&json!(5))).expect_err("number");
    assert!(matches!(number, SetupError::Type(_)));
    assert_eq!(number.message(), "'int' object is not iterable");
}
