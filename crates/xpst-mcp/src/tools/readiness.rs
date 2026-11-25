//! `xpst_readiness` — local setup readiness and actionable blockers.
//!
//! Ports `_handle_readiness()`: the same `build_readiness_report` document the
//! CLI prints, wrapped in `{ok, contract_version, readiness}`. `ok` mirrors the
//! report's own verdict instead of being a constant, so an agent gating on `ok`
//! alone cannot proceed when the install is not ready.
//!
//! The Python handler renders with `json.dumps(payload, default=str)` — no
//! indent — so the registry row is [`Render::Compact`](crate::registry::Render).

use serde_json::{json, Value};
use xpst_auth::credentials::CredentialStore;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let view = ctx.config()?;
    let store = CredentialStore::new(view.config_dir());
    let report = xpst_surface::readiness::build_report(view, &store)
        .map_err(|message| ToolError::Handler { message })?;

    Ok(json!({
        "ok": report.ready,
        "contract_version": 1,
        "readiness": report.to_value(),
    }))
}
