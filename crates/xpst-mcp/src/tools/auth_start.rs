//! `xpst_auth_start` — the human-only authentication action plan.
//!
//! Ports `_handle_auth_start()` from `src/xpst/mcp/server.py`. The plan is
//! entirely local: it names the provider, the command a human runs, and the
//! provider's docs URL. It never opens a browser, never accepts a secret and
//! never touches the network — the flow itself is `xpst connect`, which is a
//! separate (unported) surface.
//!
//! Two wire details are the tool's own:
//!
//! * the body is `json.dumps(payload)` — compact, insertion order — not the
//!   `indent=2` most handlers use;
//! * an unknown provider is a JSON document with `isError: true` and no
//!   `Error: ` prefix, because the handler returns its own `CallToolResult`
//!   instead of raising.

use serde_json::{json, Value};

use crate::error::ToolError;

use super::ToolContext;

pub fn run(_ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    // Python does `provider_definition(str(platform))`, so an absent argument is
    // the string `"None"` and a number is its decimal text — both unknown.
    let requested = python_str(arguments.get("platform").unwrap_or(&Value::Null));
    let registry =
        xpst_surface::provider_status::registry().map_err(|detail| ToolError::Malformed {
            kind: "provider registry",
            path: String::new(),
            detail,
        })?;
    let Some(definition) = registry
        .definitions
        .iter()
        .find(|definition| definition.name == requested)
    else {
        let document = json!({
            "ok": false,
            "contract_version": 1,
            "error": {
                "code": "UNKNOWN_PROVIDER",
                "message": "Choose a supported provider.",
            },
        });
        let message = xpst_config::python_style_json_compact(&document)
            .map_err(|detail| ToolError::Handler { message: detail })?;
        return Err(ToolError::Reported { message });
    };

    Ok(json!({
        "ok": true,
        "contract_version": 1,
        "platform": definition.name,
        "status": "human_action_required",
        "browser_opened": false,
        "command": format!("xpst connect {}", definition.name),
        "docs_url": definition.docs_url,
    }))
}

/// Python's `str()` for the scalar shapes an argument can carry.
fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}
