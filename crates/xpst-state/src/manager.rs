//! The operations on `state.json`: the durable record of what was published.
//!
//! Every method here is a faithful port of a `StateManager` method in
//! `src/xpst/state_manager.py`, including the parts that look accidental,
//! because the state file is shared with the shipped engine and the desktop
//! UI: the same document, the same keys, the same lifecycle markers. Where a
//! behaviour is deliberately different it is named in this module's docs and in
//! the crate README.
//!
//! The lifecycle of one destination record, as the engine models it:
//!
//! | Situation | Fields set | Counts as posted |
//! | --- | --- | --- |
//! | published | `id`, `url`, `timestamp` | yes |
//! | soft hide (reversible unpublish) | `visibility`, `soft_hidden`, `soft_hidden_at` | yes |
//! | unconfirmed delete | `delete_pending`, `delete_pending_at`, `delete_pending_reason` | yes |
//! | hard delete | `deleted`, `deleted_at`, `deleted_reason` | no (tombstone) |
//!
//! A hard delete supersedes the soft-hide and pending markers, so the file
//! cannot end up claiming both "removed" and "still live". Only removing the
//! last destination record drops the video and its content-hash entry — the
//! hash is the dedupe key, and clearing it while another platform still holds
//! the post would let the same video be published twice.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::clock::utc_now_iso;
use crate::model::{FailureRecord, PostEntry, State, VideoRecord};
use crate::store::{StateError, StateFile};

/// One destination to record as published: what the platform returned.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PostedPost {
    /// The platform's post id. Goes into the document under `id`.
    pub post_id: String,
    pub url: String,
    /// When the platform accepted it. Defaults to now, like the Python call.
    pub timestamp: Option<String>,
}

impl PostedPost {
    pub fn new(post_id: impl Into<String>, url: impl Into<String>) -> Self {
        Self {
            post_id: post_id.into(),
            url: url.into(),
            timestamp: None,
        }
    }

    pub fn at(
        post_id: impl Into<String>,
        url: impl Into<String>,
        timestamp: impl Into<String>,
    ) -> Self {
        Self {
            post_id: post_id.into(),
            url: url.into(),
            timestamp: Some(timestamp.into()),
        }
    }
}

/// The owner of the persisted state document.
#[derive(Debug)]
pub struct StateManager {
    file: StateFile,
}

impl StateManager {
    /// Open the state document in `dir` (the config directory).
    pub fn open(dir: impl AsRef<std::path::Path>) -> Result<Self, StateError> {
        Ok(Self {
            file: StateFile::open(dir)?,
        })
    }

    /// The document path.
    pub fn path(&self) -> &std::path::Path {
        self.file.path()
    }

    /// The in-memory document as of this instance's last read or write.
    pub fn state(&self) -> &State {
        self.file.state()
    }

    /// Re-read from disk under a shared lock (cross-process freshness).
    pub fn refresh(&mut self) -> Result<&State, StateError> {
        self.file.refresh()
    }

    // ── Recording ──────────────────────────────────────────────────────────

    /// Record a successful publish.
    ///
    /// The platform post id is written under `id` — the canonical key — even if
    /// a caller hands it over as a legacy `post_id`; a record whose id landed in
    /// the wrong key is a record every reader treats as "not posted".
    pub fn record_published_post(
        &mut self,
        video_id: &str,
        source_url: &str,
        source_platform: &str,
        posted_to: &BTreeMap<String, PostedPost>,
        caption: &str,
        content_hash: Option<&str>,
    ) -> Result<(), StateError> {
        let content_hash = content_hash.filter(|hash| !hash.is_empty());
        self.file.update(|state| {
            let now = utc_now_iso();
            state
                .posted_videos
                .entry(video_id.to_string())
                .or_insert_with(|| VideoRecord {
                    source_url: source_url.to_string(),
                    source_platform: source_platform.to_string(),
                    caption: caption.to_string(),
                    downloaded_at: now.clone(),
                    last_attempt: now.clone(),
                    content_hash: content_hash.map(str::to_string),
                    ..VideoRecord::default()
                });
            let video = state
                .posted_videos
                .get_mut(video_id)
                .expect("just inserted");
            video.last_attempt = now.clone();

            for (platform, post) in posted_to {
                // A fresh entry: re-recording a tombstoned destination is a
                // re-post, and the tombstone must not survive it.
                video.posted_to.insert(
                    platform.clone(),
                    PostEntry {
                        id: post.post_id.clone(),
                        url: post.url.clone(),
                        timestamp: post.timestamp.clone().unwrap_or_else(|| now.clone()),
                        ..PostEntry::default()
                    },
                );
            }

            if let Some(hash) = content_hash {
                state
                    .content_hashes
                    .insert(hash.to_string(), video_id.to_string());
            }

            state.health.total_processed += 1;
            state.health.last_check = Some(now.clone());

            for platform in posted_to.keys() {
                if let Some(health) = state.health.platforms.get_mut(platform) {
                    health.status = "ok".to_string();
                    health.last_success = Some(
                        posted_to
                            .get(platform)
                            .and_then(|post| post.timestamp.clone())
                            .unwrap_or_else(|| now.clone()),
                    );
                    health.failures = 0;
                }
            }
        })
    }

    /// Record a failed publish attempt.
    ///
    /// The failure is written to both `errors` and the legacy `failed_attempts`
    /// map, because both are read by shipped callers, and it never touches
    /// `posted_to` — a failed upload is not a post.
    pub fn record_failure(
        &mut self,
        video_id: &str,
        platform: &str,
        error: &str,
    ) -> Result<(), StateError> {
        self.file.update(|state| {
            let now = utc_now_iso();
            state
                .posted_videos
                .entry(video_id.to_string())
                .or_insert_with(|| VideoRecord {
                    downloaded_at: now.clone(),
                    last_attempt: now.clone(),
                    ..VideoRecord::default()
                });
            let video = state
                .posted_videos
                .get_mut(video_id)
                .expect("just inserted");
            video.last_attempt = now.clone();

            let count = video
                .errors
                .get(platform)
                .map(|entry| entry.count)
                .unwrap_or(0)
                + 1;
            let record = FailureRecord {
                error: error.to_string(),
                timestamp: now.clone(),
                count,
                extra: BTreeMap::new(),
            };
            video.errors.insert(platform.to_string(), record.clone());
            video.failed_attempts.insert(platform.to_string(), record);

            if let Some(health) = state.health.platforms.get_mut(platform) {
                health.failures += 1;
                health.status = "error".to_string();
                health.set_last_error(Some(Value::String(error.to_string())));
            }
        })
    }

    /// Remove one destination record.
    ///
    /// When that was the last one, the video record is dropped *and* its
    /// content-hash entry with it — otherwise the hash would point at a video
    /// that no longer exists and dedupe would silently skip a re-upload. While
    /// another platform still holds the post the hash stays: it is the key that
    /// stops the same file being published twice.
    ///
    /// Returns whether anything was removed.
    pub fn remove_post(&mut self, video_id: &str, platform: &str) -> Result<bool, StateError> {
        self.file.update(|state| {
            let Some(video) = state.posted_videos.get_mut(video_id) else {
                return false;
            };
            if video.posted_to.remove(platform).is_none() {
                return false;
            }
            if !video.posted_to.is_empty() {
                return true;
            }
            let hash = state
                .posted_videos
                .get(video_id)
                .and_then(|video| video.content_hash.clone())
                .filter(|hash| !hash.is_empty());
            if let Some(hash) = hash {
                state.content_hashes.remove(&hash);
            }
            state.posted_videos.remove(video_id);
            true
        })
    }

    // ── Delete / unpublish state discipline ────────────────────────────────

    /// Tombstone a hard delete: the post is gone from the platform, the record
    /// stays as history.
    ///
    /// Returns `false` (and writes nothing) when there is no such destination —
    /// a tombstone for a post that was never recorded would be a lie.
    pub fn record_delete_tombstone(
        &mut self,
        video_id: &str,
        platform: &str,
        reason: &str,
        detail: Option<&str>,
    ) -> Result<bool, StateError> {
        self.file.update(|state| {
            let Some(entry) = entry_mut(state, video_id, platform) else {
                return false;
            };
            entry.deleted = Some(true);
            entry.deleted_at = Some(utc_now_iso());
            entry.deleted_reason = Some(reason.to_string());
            if let Some(detail) = detail.filter(|text| !text.is_empty()) {
                entry.deleted_detail = Some(detail.to_string());
            }
            // A hard delete supersedes the softer markers.
            entry.delete_pending = None;
            entry.delete_pending_at = None;
            entry.delete_pending_reason = None;
            entry.delete_pending_detail = None;
            entry.visibility = None;
            true
        })
    }

    /// Record a soft hide — a reversible unpublish.
    ///
    /// The post keeps its id, url and metrics and still counts as posted; a
    /// later `set_visibility(.., "public")` reverses it. This is *not* a
    /// tombstone and must never be treated as one.
    pub fn set_visibility(
        &mut self,
        video_id: &str,
        platform: &str,
        visibility: &str,
    ) -> Result<bool, StateError> {
        self.file.update(|state| {
            let Some(entry) = entry_mut(state, video_id, platform) else {
                return false;
            };
            entry.visibility = Some(visibility.to_string());
            entry.soft_hidden = Some(true);
            entry.soft_hidden_at = Some(utc_now_iso());
            true
        })
    }

    /// Mark a delete as pending confirmation.
    ///
    /// The platform never confirmed the removal, so the post is still live: the
    /// entry keeps its id/url/metrics, counts as posted, and carries the
    /// request that a human can act on. Clearing `deleted` is deliberate — a
    /// pending delete supersedes an earlier tombstone claim that this build can
    /// no longer substantiate.
    pub fn mark_delete_pending(
        &mut self,
        video_id: &str,
        platform: &str,
        reason: &str,
        detail: Option<&str>,
    ) -> Result<bool, StateError> {
        self.file.update(|state| {
            let Some(entry) = entry_mut(state, video_id, platform) else {
                return false;
            };
            entry.delete_pending = Some(true);
            entry.delete_pending_at = Some(utc_now_iso());
            entry.delete_pending_reason = Some(reason.to_string());
            if let Some(detail) = detail.filter(|text| !text.is_empty()) {
                entry.delete_pending_detail = Some(detail.to_string());
            }
            entry.deleted = Some(false);
            true
        })
    }

    // ── Queries ────────────────────────────────────────────────────────────

    /// Is this video published on `platform`, with the post still live?
    ///
    /// Tombstones do not count: a hard-deleted post can be re-published.
    pub fn is_posted(&mut self, video_id: &str, platform: &str) -> Result<bool, StateError> {
        let state = self.file.refresh()?;
        Ok(state
            .video(video_id)
            .and_then(|video| video.posted_to.get(platform))
            .is_some_and(PostEntry::is_live))
    }

    /// Is this video published on *every* platform in `platforms`?
    ///
    /// An empty set is false, not vacuously true: "posted everywhere" must
    /// never be the answer for a caller that passed nothing.
    pub fn is_fully_cross_posted(
        &mut self,
        video_id: &str,
        platforms: &[&str],
    ) -> Result<bool, StateError> {
        if platforms.is_empty() {
            return Ok(false);
        }
        let state = self.file.refresh()?;
        Ok(state.video(video_id).is_some_and(|video| {
            platforms.iter().all(|platform| {
                video
                    .posted_to
                    .get(*platform)
                    .is_some_and(PostEntry::is_live)
            })
        }))
    }

    /// The video id recorded for a content hash, if any.
    pub fn get_by_hash(&mut self, content_hash: &str) -> Result<Option<String>, StateError> {
        let state = self.file.refresh()?;
        Ok(state.content_hashes.get(content_hash).cloned())
    }

    /// Is this content hash already recorded?
    pub fn has_hash(&mut self, content_hash: &str) -> Result<bool, StateError> {
        let state = self.file.refresh()?;
        Ok(state.content_hashes.contains_key(content_hash))
    }

    /// Resolve the internal video id from a platform-side post id or its URL.
    pub fn find_video_id_by_platform_post(
        &mut self,
        platform: &str,
        post_id: &str,
    ) -> Result<Option<String>, StateError> {
        let candidate = post_id.trim();
        if platform.is_empty() || candidate.is_empty() {
            return Ok(None);
        }
        let state = self.file.refresh()?;
        Ok(state.posted_videos.iter().find_map(|(video_id, video)| {
            let entry = video.posted_to.get(platform)?;
            if entry.post_id() == candidate {
                return Some(video_id.clone());
            }
            if !entry.url.is_empty() && entry.url.contains(candidate) {
                return Some(video_id.clone());
            }
            None
        }))
    }
}

fn entry_mut<'a>(
    state: &'a mut State,
    video_id: &str,
    platform: &str,
) -> Option<&'a mut PostEntry> {
    state
        .posted_videos
        .get_mut(video_id)?
        .posted_to
        .get_mut(platform)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(label: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "xpst-state-manager-{label}-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|elapsed| elapsed.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    fn posted(platform: &str, id: &str) -> BTreeMap<String, PostedPost> {
        let mut map = BTreeMap::new();
        map.insert(
            platform.to_string(),
            PostedPost::new(id, format!("https://example.invalid/{platform}/{id}")),
        );
        map
    }

    #[test]
    fn a_published_post_is_recorded_under_the_canonical_id_key() {
        let dir = temp_dir("canonical");
        let mut manager = StateManager::open(&dir).unwrap();
        manager
            .record_published_post(
                "vid-1",
                "https://example.invalid/src/1",
                "local",
                &posted("youtube", "yt-9"),
                "caption",
                Some("hash-1"),
            )
            .unwrap();
        let entry = &manager.state().posted_videos["vid-1"].posted_to["youtube"];
        assert_eq!(entry.id, "yt-9");
        assert!(!entry.extra.contains_key("post_id"));
        assert_eq!(
            manager.state().content_hashes["hash-1"],
            "vid-1",
            "the dedupe index is written"
        );
        assert_eq!(manager.state().health.total_processed, 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_failure_never_creates_a_post() {
        let dir = temp_dir("failure");
        let mut manager = StateManager::open(&dir).unwrap();
        manager
            .record_failure("vid-1", "tiktok", "rate limited")
            .unwrap();
        let video = &manager.state().posted_videos["vid-1"];
        assert!(video.posted_to.is_empty(), "a failure is not a post");
        assert_eq!(video.errors["tiktok"].error, "rate limited");
        assert_eq!(video.errors["tiktok"].count, 1);
        assert_eq!(video.failed_attempts["tiktok"].count, 1);
        assert_eq!(manager.state().health.platforms["tiktok"].failures, 1);
        assert_eq!(manager.state().health.platforms["tiktok"].status, "error");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
