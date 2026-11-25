//! `xpst_followers` — follower counts per platform from the stored snapshots.
//!
//! Ports `_handle_followers()`: `AnalyticsStore(config.config_dir / "analytics.db")`,
//! `latest_followers()`, then `follower_history(platform, limit=7)` per platform
//! for the growth figure. The payload is `indent=2`.
//!
//! `AnalyticsStore.__init__` creates the ledger and applies the schema, so a
//! fresh profile answers zeroes rather than an error — the same thing
//! `xpst_store::Store::open` does here.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let db = ctx.paths.config_dir().join("analytics.db");
    xpst_surface::followers::payload(&db)
        .map(|payload| xpst_config::output::to_json(&payload))
        .map_err(|detail| ToolError::Malformed {
            kind: "analytics ledger",
            path: db.display().to_string(),
            detail,
        })
}
