//! `xpst_security_audit` — the live installation checks.
//!
//! Ports `_handle_security_audit()`: `run_security_checks(config)` verbatim,
//! rendered with `json.dumps(payload, indent=2, default=str)`.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let view = ctx.config()?;
    Ok(xpst_config::output::to_json(
        &xpst_surface::security::payload(view),
    ))
}
