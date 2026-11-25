//! `xpst_stream` — the live-stream relay registry and key-presence verdict.
//!
//! Ports `_handle_stream()`: `stream_report(config_dir, platform=..., config=...)`,
//! which reads the captured ingest registry, the encrypted store and the config's
//! YouTube token path — no network.
//!
//! Two wire details that are the tool's, not the CLI's:
//!
//! * An unknown `platform` is **not** a bare error: the handler serves the whole
//!   payload (with `code: "unknown_platform"`, the `known` list and a `null`
//!   `platform` row) and sets `isError: true`. So the body is the document and the
//!   flag is the verdict — `ToolError::Reported` with the rendered payload.
//! * `include_live` asks Twitch/Kick who is live right now, which is the network
//!   half and is not ported. It answers a typed `not_implemented` rather than a
//!   payload with the `live` key silently missing.
//!
//! Never returns a stream key: the payload carries `key_present` and masked app
//! credentials only.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let view = ctx.config()?;

    if truthy(arguments.get("include_live")) {
        return Err(ToolError::NotImplemented {
            tool: "xpst_stream",
            reason: "asks Twitch/Kick who is live right now, which needs the network (omit \
                     `include_live` for the registry and key-presence verdict)",
        });
    }

    let platform = arguments
        .get("platform")
        .and_then(Value::as_str)
        .unwrap_or("");
    let payload = xpst_config::output::to_json(&xpst_surface::stream::stream_report(
        ctx.paths.config_dir(),
        view,
        platform,
    ));

    if payload.get("code").and_then(Value::as_str) == Some("unknown_platform") {
        let message = xpst_config::python_style_json_pretty(&payload)
            .map_err(|detail| ToolError::Handler { message: detail })?;
        return Err(ToolError::Reported { message });
    }
    Ok(payload)
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
