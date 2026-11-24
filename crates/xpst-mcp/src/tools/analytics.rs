//! `xpst_cross_post_analytics` — cross-post correlation over the local ledger.
//!
//! Ports `_handle_cross_post_analytics()`: one source video posted to several
//! platforms, collapsed into a single entry with per-platform breakdown,
//! aggregated totals, an engagement rate and a tier. Offline and fast — it
//! reads `<config dir>/analytics.db` and nothing else.
//!
//! The document itself is built in `xpst_surface::cross_post`; this module is
//! only the tool's projection (the payload wrapper and the error mapping).
//!
//! `xpst_analytics` is still a stub: its `live=true` path runs a real
//! collection against every platform API, which is the network tranche. The
//! `live=false` half reads the same ledger this tool does, so it is the natural
//! next slice once the HTTP client is decided.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn cross_post(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let db = ctx.paths.config_dir().join("analytics.db");
    let payload = xpst_surface::cross_post::payload(&db).map_err(|error| ToolError::Handler {
        message: error.message,
    })?;
    Ok(xpst_config::output::to_json(&payload))
}
