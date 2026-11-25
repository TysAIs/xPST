//! `xpst_best_time` — recommended posting times from the analytics ledger.
//!
//! Ports `_handle_best_time()`: `{platform, best_time}` when the caller named a
//! platform, `{best_times}` otherwise. The ledger is
//! `<config dir>/analytics.db`, never the no-argument default path.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let platform = arguments
        .get("platform")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty());
    let db = ctx.paths.config_dir().join("analytics.db");
    Ok(xpst_config::output::to_json(
        &xpst_surface::best_time::payload(&db, platform),
    ))
}
