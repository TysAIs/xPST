//! Round-trip parity with the shipped Python engine, in both directions.
//!
//! These are not spot checks. Each direction compares a **canonical dump of the
//! whole document** — every key, every value, recursively — through Python's own
//! `json.dumps(indent=2, sort_keys=True)`, so neither side is allowed to be the
//! judge of its own output:
//!
//! * Python writes the document → Rust reads and rewrites it → the canonical
//!   dump must be byte-identical. Anything this crate fails to model, drop, or
//!   normalise shows up as a diff.
//! * Rust writes the document → Python's real `StateManager` must both agree
//!   semantically (`verify`) and, once timestamps are normalised, produce the
//!   same canonical document Python itself writes for the same operations.
//!
//! The scenario in `scenario()` below is mirrored operation-for-operation by
//! `python_parity/state_tool.py`. Changing one without the other fails here.

mod support;

use std::collections::BTreeMap;

use support::{assert_success, path_str, stdout, Python, TempDir};
use xpst_state::{PostedPost, StateFile, StateManager, STATE_FILE_NAME};

fn python_or_skip(label: &str) -> Option<Python> {
    match Python::discover() {
        Some(python) => Some(python),
        None => {
            eprintln!(
                "SKIP {label}: no interpreter could import xpst.state_manager \
                 (set XPST_PYTHON to a python with the engine's dependencies)"
            );
            None
        }
    }
}

fn posts(entries: &[(&str, &str)]) -> BTreeMap<String, PostedPost> {
    entries
        .iter()
        .map(|(platform, post_id)| {
            (
                (*platform).to_string(),
                PostedPost::new(*post_id, format!("https://example.invalid/v/{post_id}")),
            )
        })
        .collect()
}

/// The canonical operation sequence — the Rust half of the mirror.
///
/// Every operation the state layer offers is exercised, including the two that
/// disagree about content hashes: removing one of two platforms keeps the hash,
/// removing the last platform drops the video and the hash together.
fn scenario(manager: &mut StateManager) {
    manager
        .record_published_post(
            "vid-a",
            "https://example.invalid/src/a",
            "youtube",
            &posts(&[("youtube", "yt-a"), ("x", "x-a")]),
            "first caption",
            Some("hash-a"),
        )
        .unwrap();
    manager
        .record_published_post(
            "vid-b",
            "https://example.invalid/src/b",
            "instagram",
            &posts(&[("tiktok", "tt-b"), ("instagram", "ig-b")]),
            "",
            Some("hash-b"),
        )
        .unwrap();
    manager
        .record_published_post(
            "vid-c",
            "https://example.invalid/src/c",
            "local",
            &posts(&[("x", "x-c")]),
            "",
            Some("hash-c"),
        )
        .unwrap();
    // One platform goes, the other stays: the hash must survive.
    manager.remove_post("vid-a", "x").unwrap();
    // The last platform goes: the video and its hash must disappear.
    manager.remove_post("vid-c", "x").unwrap();
    manager
        .record_failure("vid-b", "youtube", "upload rejected: expired token")
        .unwrap();
    manager
        .record_failure("vid-b", "youtube", "upload rejected: expired token")
        .unwrap();
    manager
        .record_published_post(
            "vid-b",
            "",
            "",
            &posts(&[("youtube", "yt-b")]),
            "",
            Some("hash-b"),
        )
        .unwrap();
    manager
        .record_delete_tombstone("vid-b", "tiktok", "hard_delete", Some("removed in studio"))
        .unwrap();
    manager
        .set_visibility("vid-b", "instagram", "private")
        .unwrap();
    manager
        .mark_delete_pending(
            "vid-a",
            "youtube",
            "confirmation_outstanding",
            Some("API returned 202"),
        )
        .unwrap();
}

fn dump(python: &Python, sandbox: &std::path::Path, mode: &str, path: &std::path::Path) -> String {
    let output = python.tool(sandbox, &[mode, path_str(path)]);
    assert_success(&output, mode);
    stdout(&output)
}

/// A document Python wrote must survive a Rust read/write cycle unchanged.
#[test]
fn python_created_state_survives_a_rust_read_write_cycle() {
    let Some(python) = python_or_skip("python_created_state_survives_a_rust_read_write_cycle")
    else {
        return;
    };
    let temp = TempDir::new("py-to-rs");
    let state_path = temp.path().join(STATE_FILE_NAME);
    assert_success(
        &python.tool(temp.path(), &["create", path_str(temp.path())]),
        "python create",
    );
    assert!(state_path.exists(), "python wrote state.json");
    let before = dump(&python, temp.path(), "dump", &state_path);

    // Rust parses it into the typed model and writes it straight back.
    let mut file = StateFile::open(temp.path()).expect("rust opens the python document");
    let state = file.state();
    assert_eq!(state.posted_videos.len(), 2, "vid-c was removed");
    assert_eq!(state.content_hashes.len(), 2, "hash-a and hash-b survive");
    assert_eq!(
        state.posted_videos["vid-a"].posted_to["youtube"].post_id(),
        "yt-a",
        "the platform id is read from the canonical key"
    );
    assert_eq!(state.health.total_processed, 4);
    file.save().expect("rust rewrites the document");

    let after = dump(&python, temp.path(), "dump", &state_path);
    assert_eq!(
        before, after,
        "a Rust read/write cycle changed the Python document"
    );
}

/// A document Rust wrote must be the document Python would have written.
#[test]
fn rust_created_state_matches_what_python_would_have_written() {
    let Some(python) = python_or_skip("rust_created_state_matches_what_python_would_have_written")
    else {
        return;
    };
    let rust_dir = TempDir::new("rs-create");
    let python_dir = TempDir::new("py-create");

    let mut manager = StateManager::open(rust_dir.path()).expect("open rust state");
    scenario(&mut manager);

    assert_success(
        &python.tool(python_dir.path(), &["create", path_str(python_dir.path())]),
        "python create",
    );
    // The real Python API reads the Rust-written document and asserts every
    // lifecycle invariant against it.
    assert_success(
        &python.tool(rust_dir.path(), &["verify", path_str(rust_dir.path())]),
        "python verify",
    );

    let rust_doc = dump(
        &python,
        rust_dir.path(),
        "dump-normalized",
        &rust_dir.path().join(STATE_FILE_NAME),
    );
    let python_doc = dump(
        &python,
        python_dir.path(),
        "dump-normalized",
        &python_dir.path().join(STATE_FILE_NAME),
    );
    assert_eq!(
        python_doc, rust_doc,
        "the Rust document differs from Python's for the same operations"
    );
    assert!(
        python_doc.contains("<ts>"),
        "the comparison must actually cover timestamped fields"
    );
}
