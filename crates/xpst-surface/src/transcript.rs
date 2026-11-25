//! `xpst_transcript` — a cached transcript from the knowledge workspace.
//!
//! Ports `_handle_transcript()`: `Workspace.resolve("default", create=False)`,
//! then three lookups in the order the handler tries them —
//!
//! 1. `<video_id>.json` in the workspace's `transcripts/` directory;
//! 2. `content:<video_id>.json` (the content-hash spelling);
//! 3. the first file in `transcripts/` whose *name contains* `video_id`.
//!
//! A miss is `{"error": "Transcript not found", "video_id": ...}` — a payload,
//! not an error result, because "not transcribed yet" is a normal answer. An
//! invalid `video_id` IS an error result (`isError: true`) with Python's
//! `Invalid video_id: ...` sentence, because it is a caller mistake.
//!
//! `create=False` matters: probing a workspace that does not exist must not
//! create it (G30), so this reader never writes.

use std::path::Path;

use serde_json::Value;

use crate::kb;

/// Why a transcript lookup could not run.
#[derive(Debug)]
pub enum TranscriptError {
    /// `_validate_name(video_id, "video_id")` raised `ValueError`.
    Invalid(String),
    /// The workspace could not be resolved (no `XPST_HOME`/`HOME`).
    Workspace(String),
}

impl TranscriptError {
    /// The sentence the MCP server serves for a raising handler.
    pub fn message(&self) -> String {
        match self {
            Self::Invalid(message) | Self::Workspace(message) => message.clone(),
        }
    }
}

/// `_handle_transcript()`'s payload.
pub fn payload(video_id: &str) -> Result<Value, TranscriptError> {
    let home = kb::xpst_home().ok_or_else(|| {
        TranscriptError::Workspace(
            "cannot determine the xPST home: neither XPST_HOME nor HOME is set".to_string(),
        )
    })?;
    payload_in(&home, video_id)
}

/// [`payload`] against an explicit home directory.
pub fn payload_in(home: &Path, video_id: &str) -> Result<Value, TranscriptError> {
    Ok(match lookup_in(home, video_id)? {
        Some(transcript) => transcript,
        None => serde_json::json!({
            "error": "Transcript not found",
            "video_id": video_id,
        }),
    })
}

/// The three lookups, with a miss as `None`.
///
/// `xpst transcript` needs the miss as a distinct outcome (it prints the
/// payload and exits 1), while the MCP tool serves the payload as a normal
/// answer. Splitting the search out keeps one implementation of the order.
pub fn lookup_in(home: &Path, video_id: &str) -> Result<Option<Value>, TranscriptError> {
    lookup_in_named(home, video_id, "video_id")
}

/// [`lookup_in`] with the field name the validation sentence names.
///
/// The MCP handler validates `video_id`; the CLI goes through
/// `Workspace.get_transcript()`, whose `_validate_content_hash()` names
/// `content_hash`. Same rule, different sentence — so the caller picks.
pub fn lookup_in_named(
    home: &Path,
    video_id: &str,
    field: &str,
) -> Result<Option<Value>, TranscriptError> {
    kb::validate_name(video_id, field).map_err(TranscriptError::Invalid)?;

    let root = kb::resolve_workspace_in(home, "default", false).map_err(|error| match error {
        kb::WorkspaceError::Invalid(message) => TranscriptError::Workspace(message),
        kb::WorkspaceError::NoHome => TranscriptError::Workspace(
            "cannot determine the xPST home: neither XPST_HOME nor HOME is set".to_string(),
        ),
    })?;
    let transcripts = root.join("transcripts");

    let mut found = read_transcript(&transcripts, video_id);
    if found.is_none() {
        found = read_transcript(&transcripts, &format!("content:{video_id}"));
    }
    if found.is_none() {
        // `for fname in os.listdir(tdir): if video_id in fname` — the first
        // match wins, and the `.json` suffix is stripped before the lookup.
        if let Ok(entries) = std::fs::read_dir(&transcripts) {
            let mut names: Vec<String> = entries
                .filter_map(Result::ok)
                .map(|entry| entry.file_name().to_string_lossy().to_string())
                .collect();
            // `os.listdir` order is filesystem order; sorting makes the
            // "first match" deterministic without changing a single-match
            // result. Recorded as a divergence in the ledger note.
            names.sort();
            for name in names {
                if name.contains(video_id) {
                    let stem = name.replace(".json", "");
                    found = read_transcript(&transcripts, &stem);
                    break;
                }
            }
        }
    }

    Ok(found)
}

/// `Workspace.get_transcript()`: the parsed file, or `None` when it is absent
/// or unreadable (Python swallows every exception there).
fn read_transcript(dir: &Path, name: &str) -> Option<Value> {
    let path = dir.join(format!("{name}.json"));
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn seeded_home(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("xpst-transcript-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let transcripts = dir.join("knowledge").join("default").join("transcripts");
        std::fs::create_dir_all(&transcripts).expect("transcripts");
        std::fs::write(
            transcripts.join("vid-a.json"),
            r#"{"content_hash": "vid-a", "text": "hello", "segments": []}"#,
        )
        .expect("transcript");
        std::fs::write(
            transcripts.join("content:hash-b.json"),
            r#"{"content_hash": "content:hash-b", "text": "second", "segments": []}"#,
        )
        .expect("transcript");
        dir
    }

    #[test]
    fn a_direct_id_is_found() {
        let home = seeded_home("direct");
        let payload = payload_in(&home, "vid-a").expect("payload");
        assert_eq!(payload["content_hash"], "vid-a");
        assert_eq!(payload["text"], "hello");
        let _ = std::fs::remove_dir_all(&home);
    }

    /// The second lookup is the `content:` spelling of the same id.
    #[test]
    fn the_content_prefix_is_tried_second() {
        let home = seeded_home("content");
        let payload = payload_in(&home, "hash-b").expect("payload");
        assert_eq!(payload["content_hash"], "content:hash-b");
        let _ = std::fs::remove_dir_all(&home);
    }

    /// A miss is a payload, not an error: "not transcribed yet" is a normal
    /// answer.
    #[test]
    fn a_miss_is_a_payload() {
        let home = seeded_home("miss");
        let payload = payload_in(&home, "vid-missing").expect("payload");
        assert_eq!(payload["error"], "Transcript not found");
        assert_eq!(payload["video_id"], "vid-missing");
        let _ = std::fs::remove_dir_all(&home);
    }

    /// The third lookup is a filename substring match, with `.json` stripped.
    #[test]
    fn a_filename_substring_is_the_last_resort() {
        let home = seeded_home("substring");
        let transcripts = home.join("knowledge").join("default").join("transcripts");
        std::fs::write(
            transcripts.join("prefix-vid-c-suffix.json"),
            r#"{"content_hash": "prefix-vid-c-suffix", "text": "third", "segments": []}"#,
        )
        .expect("transcript");
        let payload = payload_in(&home, "vid-c").expect("payload");
        assert_eq!(payload["content_hash"], "prefix-vid-c-suffix");
        let _ = std::fs::remove_dir_all(&home);
    }

    /// An invalid id is refused before the workspace is read, and `create=False`
    /// means a missing workspace is never materialized.
    #[test]
    fn an_invalid_id_is_refused_and_a_missing_workspace_is_not_created() {
        let home =
            std::env::temp_dir().join(format!("xpst-transcript-bare-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(&home).expect("home");

        let error = payload_in(&home, "../escape").expect_err("unsafe id");
        assert!(matches!(error, TranscriptError::Invalid(_)));
        assert!(error.message().starts_with("Invalid video_id:"));

        // A valid id against a workspace that does not exist: a miss, and no
        // directory created.
        let payload = payload_in(&home, "vid-a").expect("payload");
        assert_eq!(payload["error"], "Transcript not found");
        assert!(!home.join("knowledge").join("default").exists());
        let _ = std::fs::remove_dir_all(&home);
    }
}
