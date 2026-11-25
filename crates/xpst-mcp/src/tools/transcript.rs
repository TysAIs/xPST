//! `xpst_transcript` — a cached transcript from the knowledge workspace.
//!
//! Ports `_handle_transcript()`. An invalid `video_id` is an **error result**
//! (`isError: true`, body `Invalid video_id: ...`), not a payload — the handler
//! returns `CallToolResult(isError=True, ...)` before touching the workspace.
//! A miss is a normal payload (`{"error": "Transcript not found", ...}`).
//!
//! The lookup order is the handler's: `<video_id>.json`, then
//! `content:<video_id>.json`, then the first file in `transcripts/` whose name
//! contains `video_id`.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(_ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let video_id = arguments
        .get("video_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    xpst_surface::transcript::payload(video_id).map_err(|error| match error {
        // `_validate_name` raises before the workspace is touched, and the
        // handler turns that into `isError=True` with the bare sentence — no
        // `Error: ` prefix, because nothing raised out of the handler.
        xpst_surface::transcript::TranscriptError::Invalid(message) => {
            ToolError::Reported { message }
        }
        other => ToolError::Handler {
            message: other.message(),
        },
    })
}
