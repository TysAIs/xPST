//! `xpst_preflight` — the per-destination plan, resolved before anything is
//! published.
//!
//! Ports `_handle_preflight()`: target resolution (`resolve_targets`), the
//! content verdict (`content_verdict`), the post plan
//! (`PostPreflightService.plan`) and the route plan (`build_route_plan`) in one
//! document. Never uploads and never touches the network — `network_calls` is
//! `false` in the payload, and it is a fact rather than a promise.
//!
//! The composite lives in `xpst_surface::preflight` (the `xpst preflight` and
//! `xpst verify-media` commands serve the same builders), so the three parts
//! cannot disagree with the CLI about a destination's state.
//!
//! `check_loudness` is on here because Python's `PostPlanRequest` defaults it on;
//! `xpst preflight` pins it off so the command never spawns ffmpeg twice.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let view = ctx.config()?;
    let env = |name: &str| std::env::var(name).ok();
    let payload =
        xpst_surface::preflight::mcp_payload(view, ctx.paths.config_dir(), &env, arguments)
            .map_err(|detail| ToolError::Handler { message: detail })?;
    Ok(xpst_config::output::to_json(&payload))
}
