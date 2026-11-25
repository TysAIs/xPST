//! `xpst_chat` — the merged live-chat timeline, or the readiness verdict.
//!
//! Ports `_handle_chat()`'s **readiness** half: `chat_readiness(config_dir,
//! platforms=..., config=config)`, which touches no network. The `read: true`
//! half opens Twitch/YouTube/Kick transports for `duration_s` and is not ported,
//! so it answers a typed `not_implemented` naming the reason instead of an empty
//! `messages` list — "not live", "no credential" and "no such channel" are
//! different answers, and a fabricated quiet chat would be a fourth.
//!
//! The payload builder lives in `xpst_surface::chat` (the `xpst chat platforms`
//! command serves the same document), so the two surfaces cannot disagree about
//! which platform is readable.

use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let view = ctx.config()?;

    if truthy(arguments.get("read")) {
        return Err(ToolError::NotImplemented {
            tool: "xpst_chat",
            reason: "reads live chat transports over the network (call it with `read: false` for \
                     the readiness verdict)",
        });
    }

    // `arguments.get("platforms") or None`, then `isinstance(str)` → `[str]`.
    let platforms: Option<Vec<String>> = match arguments.get("platforms") {
        Some(Value::String(text)) if !text.is_empty() => Some(vec![text.clone()]),
        Some(Value::Array(items)) => {
            let names: Vec<String> = items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect();
            if names.is_empty() {
                None
            } else {
                Some(names)
            }
        }
        _ => None,
    };

    let payload =
        xpst_surface::chat::readiness_for(ctx.paths.config_dir(), view, platforms.as_deref())
            .map_err(|detail| ToolError::Malformed {
                kind: "chat registry",
                path: String::new(),
                detail,
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
