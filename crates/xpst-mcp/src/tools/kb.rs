//! `kb_query` and `xpst_search` — the knowledge-base query.
//!
//! Both port `query_nuggets`: `kb_query` takes an explicit workspace and the
//! handler's own `k=8` default, `xpst_search` is the top-level alias that reads
//! the `default` workspace with `k` from `limit` (default 10).
//!
//! A workspace that fails to resolve raises in Python, so the wire body is
//! `Error: <str(e)>` — [`ToolError::Handler`] carries exactly that message.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

/// `kb_query`: `kb_tools.kb_query(args["text"], workspace)`.
pub fn query(_ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let text = arguments
        .get("text")
        .and_then(Value::as_str)
        .ok_or_else(|| ToolError::Handler {
            // `args["text"]` on a missing key raises `KeyError: 'text'`.
            message: "'text'".to_string(),
        })?;
    let workspace = arguments
        .get("workspace")
        .and_then(Value::as_str)
        .unwrap_or("default");
    xpst_surface::kb::query_payload(workspace, text, 8)
        .map(|payload| xpst_config::output::to_json(&payload))
        .map_err(|error| ToolError::Handler {
            message: error.message(),
        })
}

/// `xpst_search`: `query_nuggets(query, k=limit)` on the `default` workspace.
pub fn search(_ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let text = arguments.get("query").and_then(Value::as_str).unwrap_or("");
    let limit = arguments.get("limit").and_then(Value::as_i64).unwrap_or(10);
    xpst_surface::kb::query_payload("default", text, limit)
        .map(|payload| xpst_config::output::to_json(&payload))
        .map_err(|error| ToolError::Handler {
            message: error.message(),
        })
}
