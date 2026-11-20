//! Durability: concurrent writers, and corruption that fails closed.
//!
//! Both tests exist for one reason — the failure mode of this layer is not a
//! wrong number, it is publishing everything again.

mod support;

use std::collections::BTreeMap;
use std::process::Command;

use support::{path_str, Python, TempDir};
use xpst_state::{PostedPost, StateError, StateManager, STATE_FILE_NAME};

/// Child-process mode: a separate OS process doing real writes, so the lock is
/// actually exercised across processes rather than across threads.
///
/// Returns true when this "test" was really a child writer.
fn child_writer_mode() -> bool {
    let Ok(dir) = std::env::var("XPST_STATE_CHILD_DIR") else {
        return false;
    };
    let tag = std::env::var("XPST_STATE_CHILD_TAG").expect("child tag");
    let count: u32 = std::env::var("XPST_STATE_CHILD_COUNT")
        .expect("child count")
        .parse()
        .expect("numeric child count");
    let mut manager = StateManager::open(&dir).expect("child opens state");
    for index in 0..count {
        let post_id = format!("{tag}-{index}");
        let mut posted = BTreeMap::new();
        posted.insert(
            "youtube".to_string(),
            PostedPost::new(
                post_id.clone(),
                format!("https://example.invalid/v/{post_id}"),
            ),
        );
        manager
            .record_published_post(
                &format!("vid-{post_id}"),
                "",
                "",
                &posted,
                "",
                Some(&format!("hash-{post_id}")),
            )
            .expect("child records a post");
    }
    true
}

#[test]
fn concurrent_writers_lose_no_records() {
    if child_writer_mode() {
        return;
    }
    let temp = TempDir::new("concurrency");
    let writers = 4;
    let per_writer = 8;
    let exe = std::env::current_exe().expect("current test binary");

    let children: Vec<_> = (0..writers)
        .map(|writer| {
            Command::new(&exe)
                .args([
                    "--exact",
                    "concurrent_writers_lose_no_records",
                    "--nocapture",
                    "--test-threads=1",
                ])
                .env("XPST_STATE_CHILD_DIR", temp.path())
                .env("XPST_STATE_CHILD_TAG", format!("w{writer}"))
                .env("XPST_STATE_CHILD_COUNT", per_writer.to_string())
                .spawn()
                .expect("spawn child writer")
        })
        .collect();
    for mut child in children {
        let status = child.wait().expect("wait for child writer");
        assert!(status.success(), "a child writer failed: {status}");
    }

    let mut manager = StateManager::open(temp.path()).expect("parent opens state");
    let state = manager.refresh().expect("parent refreshes state");
    let expected = (writers * per_writer) as usize;
    assert_eq!(
        state.posted_videos.len(),
        expected,
        "a concurrent write was lost: {} of {expected} records survived",
        state.posted_videos.len()
    );
    assert_eq!(
        state.content_hashes.len(),
        expected,
        "a concurrent write lost a dedupe entry"
    );
    assert_eq!(
        state.health.total_processed as usize, expected,
        "the processed counter lost an increment"
    );
    for writer in 0..writers {
        for index in 0..per_writer {
            let video_id = format!("vid-w{writer}-{index}");
            assert!(
                state.posted_videos.contains_key(&video_id),
                "{video_id} is missing from the merged document"
            );
        }
    }

    let leftovers: Vec<_> = std::fs::read_dir(temp.path())
        .expect("read state dir")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .filter(|name| name.contains(".tmp."))
        .collect();
    assert!(
        leftovers.is_empty(),
        "temporary files were left behind: {leftovers:?}"
    );
}

#[test]
fn a_corrupt_state_file_fails_closed_and_is_never_reset() {
    let temp = TempDir::new("corrupt");
    let path = temp.path().join(STATE_FILE_NAME);
    let damaged = b"{\"version\": 2, \"posted_videos\": {\"vid-1\": {\"posted_to\":".to_vec();
    std::fs::write(&path, &damaged).unwrap();

    let err = StateManager::open(temp.path()).expect_err("a truncated document is an error");
    assert!(
        matches!(err, StateError::Corrupt { .. }),
        "expected a corruption error, got {err:?}"
    );
    assert!(
        err.to_string().contains("re-post"),
        "the error must say why it will not reset: {err}"
    );
    assert_eq!(
        std::fs::read(&path).unwrap(),
        damaged,
        "the damaged file must be left exactly as found, not emptied"
    );

    // The file is still there and still refused, so a retry cannot "fix" it by
    // creating a fresh empty document behind the operator's back.
    let still_damaged = StateManager::open(temp.path()).expect_err("still corrupt");
    assert!(matches!(still_damaged, StateError::Corrupt { .. }));
    assert_eq!(std::fs::read(&path).unwrap(), damaged);
}

#[test]
fn an_empty_file_is_corruption_and_a_missing_one_is_a_fresh_install() {
    let temp = TempDir::new("empty");
    assert!(
        StateManager::open(temp.path()).is_ok(),
        "a missing state.json is a fresh install, not an error"
    );
    assert!(
        !temp.path().join(STATE_FILE_NAME).exists(),
        "opening must not create a document"
    );

    std::fs::write(temp.path().join(STATE_FILE_NAME), "").unwrap();
    let err = StateManager::open(temp.path()).expect_err("an empty file is corruption");
    assert!(matches!(err, StateError::Corrupt { .. }), "{err:?}");
    assert_eq!(
        std::fs::read(temp.path().join(STATE_FILE_NAME)).unwrap(),
        b"",
        "the empty file is reported, not replaced"
    );
}

#[test]
fn a_state_file_from_the_future_is_refused_rather_than_downgraded() {
    let temp = TempDir::new("future");
    let path = temp.path().join(STATE_FILE_NAME);
    std::fs::write(&path, r#"{"version": 7, "posted_videos": {}}"#).unwrap();
    let err = StateManager::open(temp.path()).expect_err("v7 is not readable");
    assert!(
        matches!(err, StateError::UnsupportedVersion { version: 7, .. }),
        "{err:?}"
    );
    assert_eq!(
        std::fs::read_to_string(&path).unwrap(),
        r#"{"version": 7, "posted_videos": {}}"#,
        "an unknown schema is never rewritten"
    );
}

#[test]
fn a_write_by_another_process_is_visible_after_refresh() {
    let temp = TempDir::new("refresh");
    let mut reader = StateManager::open(temp.path()).unwrap();
    assert!(reader.state().posted_videos.is_empty());

    let mut writer = StateManager::open(temp.path()).unwrap();
    let mut posted = BTreeMap::new();
    posted.insert(
        "x".to_string(),
        PostedPost::new("x-1", "https://example.invalid/x"),
    );
    writer
        .record_published_post("vid-1", "", "", &posted, "", None)
        .unwrap();

    assert!(
        reader
            .refresh()
            .unwrap()
            .posted_videos
            .contains_key("vid-1"),
        "a peer's record must become visible without reopening"
    );
    assert!(reader.is_posted("vid-1", "x").unwrap());
}

/// Cross-implementation exclusion: the Python engine holds `.state.lock`, and a
/// Rust write into the same directory must wait for it.
///
/// This is the claim that makes the lock worth having. If the two
/// implementations took *different* locks — different files, or `flock` versus
/// POSIX record locks, which do not conflict on Linux — every test in this
/// crate would still pass while the engine and the CLI raced in production.
#[test]
fn the_python_engine_and_this_crate_exclude_each_other() {
    let Some(python) = Python::discover() else {
        eprintln!("SKIP the_python_engine_and_this_crate_exclude_each_other: no interpreter could import xpst.state_store");
        return;
    };
    let temp = TempDir::new("lock-interop");
    // Open first: opening takes the lock briefly, and this test is about the
    // write path, not about open.
    let mut manager = StateManager::open(temp.path()).unwrap();

    let hold_seconds = 2.5;
    let mut holder = python
        .tool_command(temp.path(), &["hold-lock", path_str(temp.path()), "2.5"])
        .spawn()
        .expect("spawn the lock holder");

    // Wait until the engine actually holds the lock.
    let marker = temp.path().join("lock-held");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
    while !marker.exists() {
        assert!(
            std::time::Instant::now() < deadline,
            "the python lock holder never reported holding the lock"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }

    let started = std::time::Instant::now();
    let mut posted = BTreeMap::new();
    posted.insert(
        "x".to_string(),
        PostedPost::new("x-1", "https://example.invalid/x"),
    );
    manager
        .record_published_post("vid-1", "", "", &posted, "", None)
        .expect("the write proceeds once the engine releases the lock");
    let waited = started.elapsed();

    assert!(
        waited >= std::time::Duration::from_secs_f64(hold_seconds / 2.0),
        "the Rust write did not wait for the engine's lock (waited {waited:?})"
    );
    assert!(
        holder.wait().expect("python holder exits").success(),
        "the lock holder failed"
    );
    assert!(manager.state().posted_videos.contains_key("vid-1"));
}
