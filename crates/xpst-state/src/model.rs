//! The typed model of `state.json` — the durable record of what was published.
//!
//! Shape authority is the Python engine (`src/xpst/state_manager.py` and
//! `src/xpst/state_store.py`), not this file:
//!
//! ```text
//! {
//!   "version": 2,
//!   "posted_videos": {
//!     "<video_id>": {
//!       "source_url": "", "source_platform": "", "caption": "",
//!       "posted_to": {"<platform>": {"id": "", "url": "", "timestamp": ""}},
//!       "downloaded_at": "", "last_attempt": "", "content_hash": null,
//!       "errors": {...}, "failed_attempts": {...}      // only once seen
//!     }
//!   },
//!   "content_hashes": {"<sha256>": "<video_id>"},
//!   "health": {
//!     "platforms": {"<platform>": {"status": "ok", "last_success": null, "failures": 0}},
//!     "sources": {},
//!     "total_processed": 0, "last_check": null, "last_wake_check": null
//!   }
//! }
//! ```
//!
//! Two rules are load-bearing and both are pinned by tests:
//!
//! * The platform post id lives under **`id`**. `post_id` is a legacy spelling
//!   that is accepted on read and never written — a reader that assumed
//!   `post_id` silently found nothing, which is how a live analytics refresh
//!   collected zero rows for months.
//! * Fields this build does not know are **preserved**, not dropped. Every
//!   struct keeps the unrecognised keys in a flattened `extra` map, so a Rust
//!   write cannot delete a field another build or a human added. A state file
//!   that loses data is a state file that re-posts everything.

use std::collections::BTreeMap;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

/// The schema version this crate reads and writes.
pub const SCHEMA_VERSION: u64 = 2;

/// Key the engine writes for a published platform post id.
pub const CANONICAL_POST_ID_KEY: &str = "id";
/// Legacy spelling. Accepted on read, never written.
pub const LEGACY_POST_ID_KEY: &str = "post_id";

/// The platforms Python seeds into `health.platforms`.
pub const DEFAULT_PLATFORMS: [&str; 5] = ["instagram", "threads", "tiktok", "x", "youtube"];

/// The whole document.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct State {
    #[serde(default = "default_version")]
    pub version: u64,
    #[serde(default)]
    pub posted_videos: BTreeMap<String, VideoRecord>,
    #[serde(default)]
    pub content_hashes: BTreeMap<String, String>,
    #[serde(default)]
    pub health: Health,
    /// Keys this build does not model, preserved verbatim (e.g. the legacy
    /// top-level `cross_posted` map older engines wrote).
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

fn default_version() -> u64 {
    SCHEMA_VERSION
}

impl Default for State {
    fn default() -> Self {
        Self {
            version: SCHEMA_VERSION,
            posted_videos: BTreeMap::new(),
            content_hashes: BTreeMap::new(),
            health: Health::default(),
            extra: BTreeMap::new(),
        }
    }
}

impl State {
    /// A fresh, valid document. `health.platforms` is seeded exactly as
    /// Python's `StateStore._empty_state()` seeds it.
    pub fn new() -> Self {
        Self::default()
    }

    /// The video record, if tracked.
    pub fn video(&self, video_id: &str) -> Option<&VideoRecord> {
        self.posted_videos.get(video_id)
    }

    /// Mutable access to a video record.
    pub fn video_mut(&mut self, video_id: &str) -> Option<&mut VideoRecord> {
        self.posted_videos.get_mut(video_id)
    }
}

/// One tracked video.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct VideoRecord {
    #[serde(default)]
    pub source_url: String,
    #[serde(default)]
    pub source_platform: String,
    #[serde(default)]
    pub caption: String,
    #[serde(default)]
    pub posted_to: BTreeMap<String, PostEntry>,
    #[serde(default)]
    pub downloaded_at: String,
    #[serde(default)]
    pub last_attempt: String,
    /// Present as `null` in the documents Python writes for a video it has only
    /// ever seen a failure for; `Option<String>` keeps that distinction.
    #[serde(default)]
    pub content_hash: Option<String>,
    /// Per-platform error records (`record_failure`). Absent until a failure.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub errors: BTreeMap<String, FailureRecord>,
    /// Legacy duplicate of `errors`, written by the same call.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub failed_attempts: BTreeMap<String, FailureRecord>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

impl VideoRecord {
    /// True when the video is published somewhere that still exists: at least
    /// one destination record, none of them a tombstone.
    pub fn is_posted_anywhere(&self) -> bool {
        self.posted_to.values().any(PostEntry::is_live)
    }
}

/// One destination record: where the video was published and what happened.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct PostEntry {
    /// Canonical platform post id. Empty means "no id was recorded" — never
    /// fabricate one.
    #[serde(default, deserialize_with = "deserialize_scalar_string")]
    pub id: String,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub timestamp: String,

    /// Hard delete: the post is gone from the platform. The row stays as a
    /// tombstone so analytics can label it, but it does not count as posted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deleted: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deleted_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deleted_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deleted_detail: Option<String>,

    /// Soft hide: the post exists but its visibility was lowered
    /// (`private`/`unlisted`). It still counts as posted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub visibility: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub soft_hidden: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub soft_hidden_at: Option<String>,

    /// Unconfirmed delete: the platform never confirmed removal, so the post is
    /// still live and the entry still counts as posted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delete_pending: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delete_pending_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delete_pending_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delete_pending_detail: Option<String>,

    /// Anything else, including the legacy `post_id` spelling.
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

impl PostEntry {
    /// A live (non-tombstone) post.
    pub fn is_live(&self) -> bool {
        self.deleted != Some(true)
    }

    /// The platform post id, canonical key first, legacy `post_id` second.
    ///
    /// Returns `""` when the record carries no id — callers must read that as
    /// "nothing to fetch", never as a value.
    pub fn post_id(&self) -> &str {
        let canonical = self.id.trim();
        if !canonical.is_empty() {
            return canonical;
        }
        self.extra
            .get(LEGACY_POST_ID_KEY)
            .and_then(scalar_str)
            .map(str::trim)
            .unwrap_or("")
    }
}

/// One `errors` / `failed_attempts` entry.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct FailureRecord {
    #[serde(default)]
    pub error: String,
    #[serde(default)]
    pub timestamp: String,
    #[serde(default)]
    pub count: u64,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

/// `health`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Health {
    #[serde(default)]
    pub platforms: BTreeMap<String, PlatformHealth>,
    #[serde(default)]
    pub sources: BTreeMap<String, SourceHealth>,
    #[serde(default)]
    pub total_processed: u64,
    #[serde(default)]
    pub last_check: Option<String>,
    #[serde(default)]
    pub last_wake_check: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

impl Default for Health {
    fn default() -> Self {
        let platforms = DEFAULT_PLATFORMS
            .iter()
            .map(|name| ((*name).to_string(), PlatformHealth::default()))
            .collect();
        Self {
            platforms,
            sources: BTreeMap::new(),
            total_processed: 0,
            last_check: None,
            last_wake_check: None,
            extra: BTreeMap::new(),
        }
    }
}

/// Per-platform health. Historical, not a live check.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlatformHealth {
    #[serde(default = "default_status")]
    pub status: String,
    /// Always serialised, including as `null` — Python persists the key.
    #[serde(default)]
    pub last_success: Option<String>,
    #[serde(default)]
    pub failures: u64,
    /// `last_error` and anything newer than this build. Typed as a JSON value
    /// because Python stores both "absent" and `null` here and the difference
    /// must survive a round trip.
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

fn default_status() -> String {
    "ok".to_string()
}

impl Default for PlatformHealth {
    fn default() -> Self {
        Self {
            status: default_status(),
            last_success: None,
            failures: 0,
            extra: BTreeMap::new(),
        }
    }
}

impl PlatformHealth {
    /// `last_error` as written by `record_failure` / `update_platform_health`.
    pub fn last_error(&self) -> Option<&str> {
        self.extra.get("last_error").and_then(Value::as_str)
    }

    pub fn set_last_error(&mut self, error: Option<Value>) {
        match error {
            Some(value) => {
                self.extra.insert("last_error".to_string(), value);
            }
            None => {
                self.extra.remove("last_error");
            }
        }
    }
}

/// Per-source fetch health (`health.sources`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SourceHealth {
    #[serde(default = "default_status")]
    pub status: String,
    #[serde(default)]
    pub last_success: Option<String>,
    #[serde(default)]
    pub consecutive_failures: u64,
    #[serde(default)]
    pub last_error: Option<String>,
    #[serde(default)]
    pub last_error_at: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

impl Default for SourceHealth {
    fn default() -> Self {
        Self {
            status: default_status(),
            last_success: None,
            consecutive_failures: 0,
            last_error: None,
            last_error_at: None,
            extra: BTreeMap::new(),
        }
    }
}

/// Accept a JSON scalar where a string is expected.
///
/// Python's `resolve_platform_post_id` calls `str()` on whatever it finds, so a
/// hand-edited file with `"id": 123456` is not an error there. Rejecting the
/// whole document — and therefore refusing every write — over one numeric id
/// would be the wrong direction; converting keeps the file usable.
fn deserialize_scalar_string<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    match Option::<Value>::deserialize(deserializer)? {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(text)) => Ok(text),
        Some(Value::Number(number)) => Ok(number.to_string()),
        Some(Value::Bool(flag)) => Ok(flag.to_string()),
        Some(other) => Err(D::Error::custom(format!(
            "expected a string or scalar, found {other}"
        ))),
    }
}

fn scalar_str(value: &Value) -> Option<&str> {
    value.as_str().filter(|text| !text.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_post_id_is_read_from_the_canonical_key() {
        let entry: PostEntry =
            serde_json::from_str(r#"{"id": "yt-1", "url": "u", "timestamp": "t"}"#).unwrap();
        assert_eq!(entry.post_id(), "yt-1");
        assert!(!entry.extra.contains_key(LEGACY_POST_ID_KEY));
    }

    #[test]
    fn the_legacy_spelling_is_accepted_on_read_and_preserved() {
        let entry: PostEntry =
            serde_json::from_str(r#"{"post_id": "legacy-9", "url": "u", "timestamp": "t"}"#)
                .unwrap();
        assert_eq!(entry.post_id(), "legacy-9");
        let written = serde_json::to_value(&entry).unwrap();
        assert_eq!(
            written[LEGACY_POST_ID_KEY], "legacy-9",
            "the legacy key must survive a write, not be silently erased"
        );
    }

    #[test]
    fn an_empty_canonical_id_falls_through_to_the_legacy_key() {
        let entry: PostEntry = serde_json::from_str(
            r#"{"id": "", "post_id": "legacy-9", "url": "u", "timestamp": "t"}"#,
        )
        .unwrap();
        assert_eq!(entry.post_id(), "legacy-9");
    }

    #[test]
    fn a_numeric_id_is_not_a_document_failure() {
        let entry: PostEntry =
            serde_json::from_str(r#"{"id": 123456, "url": "u", "timestamp": "t"}"#).unwrap();
        assert_eq!(entry.post_id(), "123456");
    }

    #[test]
    fn unknown_fields_survive_a_round_trip() {
        let text = r#"{
            "version": 2,
            "posted_videos": {"v": {"posted_to": {"x": {"id": "1", "url": "", "timestamp": "t",
                "future_flag": true}}, "future_video_field": 7}},
            "content_hashes": {},
            "health": {"platforms": {"x": {"status": "ok", "last_success": null, "failures": 0,
                "quota_reset_at": "t"}}, "future_health": [1, 2]},
            "future_root": {"a": 1}
        }"#;
        let state: State = serde_json::from_str(text).unwrap();
        let written = serde_json::to_value(&state).unwrap();
        assert_eq!(written["future_root"]["a"], 1);
        assert_eq!(written["health"]["future_health"][1], 2);
        assert_eq!(written["health"]["platforms"]["x"]["quota_reset_at"], "t");
        assert_eq!(written["posted_videos"]["v"]["future_video_field"], 7);
        assert_eq!(
            written["posted_videos"]["v"]["posted_to"]["x"]["future_flag"],
            true
        );
    }

    #[test]
    fn a_fresh_state_matches_the_defaults_python_seeds() {
        let state = State::new();
        let written = serde_json::to_value(&state).unwrap();
        assert_eq!(written["version"], 2);
        assert_eq!(written["posted_videos"], serde_json::json!({}));
        assert_eq!(written["content_hashes"], serde_json::json!({}));
        assert_eq!(written["health"]["sources"], serde_json::json!({}));
        assert_eq!(written["health"]["total_processed"], 0);
        assert_eq!(written["health"]["last_check"], Value::Null);
        assert_eq!(written["health"]["last_wake_check"], Value::Null);
        for platform in DEFAULT_PLATFORMS {
            let entry = &written["health"]["platforms"][platform];
            assert_eq!(entry["status"], "ok");
            assert_eq!(entry["last_success"], Value::Null);
            assert_eq!(entry["failures"], 0);
        }
    }
}
