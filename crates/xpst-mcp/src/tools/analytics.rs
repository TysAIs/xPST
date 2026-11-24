//! `xpst_cross_post_analytics` and `xpst_analytics` — the local ledger surface.
//!
//! `cross_post` ports `_handle_cross_post_analytics()`: one source video posted
//! to several platforms, collapsed into a single entry with per-platform
//! breakdown, aggregated totals, an engagement rate and a tier. Offline and
//! fast — it reads `<config dir>/analytics.db` and nothing else.
//!
//! `run` ports `_handle_analytics()`'s **local** half: `live=false` reads the
//! same ledger plus `state.json`, runs the ownership gate and serves the same
//! document the UI renders. `live=true` runs a real collection against every
//! platform API, so it answers a typed `not_implemented` naming the reason
//! instead of an empty report — "not collected" and "collected nothing" are
//! different answers, and a fabricated quiet report would be a third.
//!
//! The documents themselves are built in `xpst_surface::cross_post` and
//! `xpst_surface::analytics`; this module is only the tools' projection (the
//! argument shapes and the error mapping).

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

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    if truthy(arguments.get("live")) {
        return Err(ToolError::NotImplemented {
            tool: "xpst_analytics",
            reason:
                "collects from every platform API over the network (call it with `live: false` \
                     for the recorded report)",
        });
    }

    // `arguments.get("platform")` — a falsy value means "no filter", a string
    // filters, and a truthy non-string matches no TEXT column.
    let filter = match arguments.get("platform") {
        Some(Value::String(name)) if !name.is_empty() => {
            xpst_surface::analytics::PlatformFilter::Named(name)
        }
        Some(value) if truthy(Some(value)) => xpst_surface::analytics::PlatformFilter::Unmatchable,
        _ => xpst_surface::analytics::PlatformFilter::All,
    };

    let payload =
        xpst_surface::analytics::payload(ctx.paths.config_dir(), filter).map_err(|error| {
            ToolError::Handler {
                message: error.message,
            }
        })?;
    Ok(xpst_config::output::to_json(&payload))
}

/// Python truthiness for a JSON argument.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().is_some_and(|value| value != 0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}
