//! `xpst_setup_start` / `xpst_setup_status` / `xpst_setup_resume` /
//! `xpst_setup_reset` — the shared resumable onboarding transaction.
//!
//! Ports `_handle_setup_tool()` from `src/xpst/mcp/server.py`. The state machine
//! itself is `xpst_surface::setup_transaction` (ported from
//! `src/xpst/setup_transaction.py`), so this module is only the tool's own
//! projection:
//!
//! * the `alias="mcp"` metadata every response carries;
//! * the `SETUP_REQUEST_INVALID` document a refused request answers with —
//!   `isError: true`, and the operation named from the tool's own suffix;
//! * the renderer: `json.dumps(payload, indent=2, sort_keys=True)`, which is
//!   neither of the two renderers the other tools use.
//!
//! The four tools are deliberately outside the consent gate (`_MUTATING_TOOLS`
//! does not list them): first-run setup happens before an operator opts into
//! mutations, and the transaction only ever holds non-secret identifiers.

use serde_json::Value;
use xpst_surface::setup_transaction::{
    request_invalid, ResumeRequest, SetupError, SetupTransactionService,
};

use crate::error::ToolError;

use super::ToolContext;

/// `json.dumps(payload, indent=2, sort_keys=True)` — the setup tools' renderer.
fn render(payload: &Value) -> Result<String, ToolError> {
    let mut sorted = payload.clone();
    xpst_core::json::sort_keys_recursive(&mut sorted);
    xpst_config::python_style_json_pretty(&sorted)
        .map_err(|detail| ToolError::Handler { message: detail })
}

/// Map a service failure onto the wire.
///
/// `SetupTransactionError` and `SetupTransactionNotFound` are caught by the
/// Python handler and answered with the `SETUP_REQUEST_INVALID` document; an
/// I/O failure is not caught, so it surfaces as `Error: <sentence>`.
fn failure(operation: &str, error: SetupError) -> ToolError {
    match error {
        SetupError::Invalid(_) | SetupError::NotFound(_) => {
            let document = request_invalid(operation);
            match render(&document) {
                Ok(message) => ToolError::Reported { message },
                Err(err) => err,
            }
        }
        SetupError::Io(message) | SetupError::Type(message) => ToolError::Handler { message },
    }
}

fn service(ctx: &ToolContext) -> SetupTransactionService {
    SetupTransactionService::new(ctx.paths.config_dir())
}

pub fn start(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let mut service = service(ctx);
    service
        .start_json(arguments.get("selected_role_capabilities"), "mcp")
        .map_err(|error| failure("start", error))
}

pub fn status(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let mut service = service(ctx);
    service
        .status_json(arguments.get("transaction_id"), "mcp")
        .map_err(|error| failure("status", error))
}

pub fn resume(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let mut service = service(ctx);
    // Python forwards exactly the keys the caller sent, so an absent
    // `finish_later` is `False` rather than "unset".
    let request = ResumeRequest {
        transaction_id: arguments.get("transaction_id"),
        step_id: arguments.get("step_id"),
        step_state: arguments.get("step_state"),
        step_updates: arguments.get("step_updates"),
        readiness: arguments.get("readiness"),
        finish_later: arguments.get("finish_later").map(truthy).unwrap_or(false),
        error: arguments.get("error"),
    };
    service
        .resume_json(request, "mcp")
        .map_err(|error| failure("resume", error))
}

pub fn reset(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let mut service = service(ctx);
    service
        .reset_json(arguments.get("transaction_id"), "mcp")
        .map_err(|error| failure("reset", error))
}

/// Python truthiness for a JSON argument.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}
