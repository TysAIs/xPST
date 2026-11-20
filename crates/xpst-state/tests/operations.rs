//! Lifecycle semantics: what counts as posted, what is history, and what the
//! state file must never say.

mod support;

use std::collections::BTreeMap;

use support::TempDir;
use xpst_state::{PostedPost, StateManager, LEGACY_POST_ID_KEY, STATE_FILE_NAME};

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

fn published(manager: &mut StateManager, video_id: &str, platform: &str, post_id: &str) {
    manager
        .record_published_post(
            video_id,
            "https://example.invalid/src",
            "local",
            &posts(&[(platform, post_id)]),
            "",
            None,
        )
        .unwrap();
}

/// The contract that produced a real defect: the platform post id lives under
/// `id`. A reader that looked for `post_id` found nothing, silently, for months.
#[test]
fn the_platform_post_id_is_stored_under_id_never_post_id() {
    let temp = TempDir::new("id-contract");
    let mut manager = StateManager::open(temp.path()).unwrap();
    manager
        .record_published_post(
            "vid-1",
            "https://example.invalid/src",
            "local",
            &posts(&[("youtube", "yt-1"), ("x", "x-1")]),
            "",
            Some("hash-1"),
        )
        .unwrap();

    let written: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(temp.path().join(STATE_FILE_NAME)).unwrap())
            .unwrap();
    let posted_to = &written["posted_videos"]["vid-1"]["posted_to"];
    for platform in ["youtube", "x"] {
        assert!(
            posted_to[platform]["id"].is_string(),
            "{platform} has no id under the canonical key: {}",
            posted_to[platform]
        );
        assert!(
            posted_to[platform].get("post_id").is_none(),
            "{platform} must not carry the legacy post_id key, found {}",
            posted_to[platform]
        );
    }
    assert_eq!(posted_to["youtube"]["id"], "yt-1");
    assert_eq!(posted_to["x"]["id"], "x-1");
}

/// A hand-edited or older file that only has `post_id` must still resolve, and
/// must not be rewritten into something that lost the value.
#[test]
fn a_legacy_post_id_is_accepted_on_read_and_preserved_on_write() {
    let temp = TempDir::new("legacy-id");
    let path = temp.path().join(STATE_FILE_NAME);
    std::fs::write(
        &path,
        serde_json::json!({
            "version": 2,
            "posted_videos": {
                "vid-legacy": {
                    "source_url": "",
                    "source_platform": "local",
                    "caption": "",
                    "posted_to": {
                        "youtube": {
                            "post_id": "legacy-9",
                            "url": "https://example.invalid/v/legacy-9",
                            "timestamp": "2026-01-02T03:04:05"
                        }
                    },
                    "downloaded_at": "",
                    "last_attempt": "",
                    "content_hash": null
                }
            },
            "content_hashes": {},
            "health": {"platforms": {}, "sources": {}, "total_processed": 0}
        })
        .to_string(),
    )
    .unwrap();

    let mut manager = StateManager::open(temp.path()).unwrap();
    assert_eq!(
        manager
            .find_video_id_by_platform_post("youtube", "legacy-9")
            .unwrap()
            .as_deref(),
        Some("vid-legacy"),
        "the legacy spelling must resolve through the id contract"
    );
    assert!(manager.is_posted("vid-legacy", "youtube").unwrap());

    // Any mutation rewrites the document; the legacy value must still be there.
    manager
        .record_failure("vid-legacy", "x", "no session")
        .unwrap();
    let written: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert_eq!(
        written["posted_videos"]["vid-legacy"]["posted_to"]["youtube"][LEGACY_POST_ID_KEY],
        "legacy-9",
        "a write must not erase an id it did not understand"
    );
}

#[test]
fn tombstone_pending_and_soft_hide_are_three_different_states() {
    let temp = TempDir::new("lifecycle");
    let mut manager = StateManager::open(temp.path()).unwrap();
    manager
        .record_published_post(
            "vid-1",
            "",
            "",
            &posts(&[
                ("youtube", "yt-1"),
                ("instagram", "ig-1"),
                ("tiktok", "tt-1"),
            ]),
            "",
            None,
        )
        .unwrap();

    // Soft hide: reversible, still published, still counted.
    assert!(manager
        .set_visibility("vid-1", "instagram", "private")
        .unwrap());
    let entry = &manager.state().posted_videos["vid-1"].posted_to["instagram"];
    assert_eq!(entry.visibility.as_deref(), Some("private"));
    assert_eq!(entry.soft_hidden, Some(true));
    assert_eq!(entry.deleted, None, "a soft hide is not a delete");
    assert!(manager.is_posted("vid-1", "instagram").unwrap());

    // Unconfirmed delete: still published, awaiting confirmation.
    assert!(manager
        .mark_delete_pending("vid-1", "tiktok", "confirmation_outstanding", Some("202"))
        .unwrap());
    let entry = &manager.state().posted_videos["vid-1"].posted_to["tiktok"];
    assert_eq!(entry.delete_pending, Some(true));
    assert_eq!(entry.deleted, Some(false), "pending is not deleted");
    assert_eq!(
        entry.delete_pending_reason.as_deref(),
        Some("confirmation_outstanding")
    );
    assert!(manager.is_posted("vid-1", "tiktok").unwrap());

    // Hard delete: a tombstone, history kept, no longer posted.
    assert!(manager
        .record_delete_tombstone("vid-1", "youtube", "hard_delete", Some("removed"))
        .unwrap());
    let entry = &manager.state().posted_videos["vid-1"].posted_to["youtube"];
    assert_eq!(entry.deleted, Some(true));
    assert_eq!(entry.deleted_reason.as_deref(), Some("hard_delete"));
    assert_eq!(entry.deleted_detail.as_deref(), Some("removed"));
    assert_eq!(
        entry.post_id(),
        "yt-1",
        "the tombstone keeps its id for analytics"
    );
    assert!(!manager.is_posted("vid-1", "youtube").unwrap());

    // A hard delete supersedes the softer markers on the same entry.
    manager
        .mark_delete_pending("vid-1", "tiktok", "confirmation_outstanding", None)
        .unwrap();
    manager
        .record_delete_tombstone("vid-1", "tiktok", "hard_delete", None)
        .unwrap();
    let entry = &manager.state().posted_videos["vid-1"].posted_to["tiktok"];
    assert_eq!(entry.deleted, Some(true));
    assert_eq!(
        entry.delete_pending, None,
        "a confirmed delete clears pending"
    );
    assert_eq!(entry.delete_pending_at, None);
    assert_eq!(entry.delete_pending_reason, None);
}

#[test]
fn a_tombstoned_destination_can_be_published_again() {
    let temp = TempDir::new("repost");
    let mut manager = StateManager::open(temp.path()).unwrap();
    published(&mut manager, "vid-1", "youtube", "yt-1");
    manager
        .record_delete_tombstone("vid-1", "youtube", "hard_delete", None)
        .unwrap();
    assert!(!manager.is_posted("vid-1", "youtube").unwrap());

    published(&mut manager, "vid-1", "youtube", "yt-2");
    let entry = &manager.state().posted_videos["vid-1"].posted_to["youtube"];
    assert_eq!(
        entry.deleted, None,
        "the tombstone must not survive a re-post"
    );
    assert_eq!(entry.post_id(), "yt-2");
    assert!(manager.is_posted("vid-1", "youtube").unwrap());
}

#[test]
fn the_content_hash_is_kept_until_the_last_platform_is_removed() {
    let temp = TempDir::new("hash-cleanup");
    let mut manager = StateManager::open(temp.path()).unwrap();
    manager
        .record_published_post(
            "vid-1",
            "",
            "",
            &posts(&[("youtube", "yt-1"), ("x", "x-1")]),
            "",
            Some("hash-1"),
        )
        .unwrap();
    assert_eq!(
        manager.get_by_hash("hash-1").unwrap().as_deref(),
        Some("vid-1")
    );

    assert!(manager.remove_post("vid-1", "youtube").unwrap());
    assert_eq!(
        manager.get_by_hash("hash-1").unwrap().as_deref(),
        Some("vid-1"),
        "another platform still holds the post, so the dedupe key must stay"
    );
    assert!(manager.state().posted_videos.contains_key("vid-1"));

    assert!(manager.remove_post("vid-1", "x").unwrap());
    assert!(
        !manager.has_hash("hash-1").unwrap(),
        "the last platform went, so the hash must go with the video"
    );
    assert!(
        !manager.state().posted_videos.contains_key("vid-1"),
        "a video with no destinations is not a record"
    );
    assert!(
        !manager.remove_post("vid-1", "x").unwrap(),
        "removing what is not there is a no-op"
    );
}

#[test]
fn posting_queries_ignore_tombstones_and_an_empty_platform_set() {
    let temp = TempDir::new("queries");
    let mut manager = StateManager::open(temp.path()).unwrap();
    manager
        .record_published_post(
            "vid-1",
            "",
            "",
            &posts(&[("youtube", "yt-1"), ("x", "x-1")]),
            "",
            None,
        )
        .unwrap();
    assert!(manager
        .is_fully_cross_posted("vid-1", &["youtube", "x"])
        .unwrap());
    manager
        .record_delete_tombstone("vid-1", "x", "hard_delete", None)
        .unwrap();
    assert!(!manager
        .is_fully_cross_posted("vid-1", &["youtube", "x"])
        .unwrap());
    assert!(manager
        .is_fully_cross_posted("vid-1", &["youtube"])
        .unwrap());
    assert!(
        !manager.is_fully_cross_posted("vid-1", &[]).unwrap(),
        "'posted everywhere' must never be the answer to an empty question"
    );
    assert!(!manager.is_posted("vid-missing", "youtube").unwrap());
}

#[test]
fn a_failure_is_recorded_but_never_becomes_a_post() {
    let temp = TempDir::new("failure");
    let mut manager = StateManager::open(temp.path()).unwrap();
    manager
        .record_failure("vid-1", "tiktok", "rate limited")
        .unwrap();
    manager
        .record_failure("vid-1", "tiktok", "rate limited")
        .unwrap();
    let video = &manager.state().posted_videos["vid-1"];
    assert!(video.posted_to.is_empty());
    assert_eq!(video.errors["tiktok"].count, 2, "failures accumulate");
    assert_eq!(video.failed_attempts["tiktok"].count, 2);
    assert!(!manager.is_posted("vid-1", "tiktok").unwrap());
    assert_eq!(
        manager.state().health.platforms["tiktok"].last_error(),
        Some("rate limited"),
        "the platform's health carries the last error"
    );

    published(&mut manager, "vid-1", "tiktok", "tt-1");
    assert!(manager.is_posted("vid-1", "tiktok").unwrap());
    assert_eq!(
        manager.state().health.platforms["tiktok"].failures,
        0,
        "a success resets the platform's failure count"
    );
    assert_eq!(
        manager.state().health.platforms["tiktok"].last_error(),
        Some("rate limited"),
        "the historical error stays; health is history, not a live check"
    );
}
