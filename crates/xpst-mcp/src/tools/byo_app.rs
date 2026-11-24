//! `xpst_byo_app` — bring-your-own developer app: the masked status read, and
//! the two writers.
//!
//! Ports `_handle_byo_app()`. The registry, the credential resolution and the
//! masked status payload live in `xpst_surface::byo` (the CLI's `xpst byo`
//! commands serve the same document), so this module is the tool's own
//! projection: the `action` discriminator, the two bare-sentence refusals and
//! the `ByoAppError` document.
//!
//! Three wire details that are the tool's, not the CLI's:
//!
//! * The status read is `{"ok": true, "action": "status", **byo_status(config)}`
//!   — the whole-platform payload is spread, not nested.
//! * A refusal is a bare sentence with `isError: true` and no `Error: ` prefix
//!   (`ToolError::Reported`), because the handler returns its own
//!   `CallToolResult` instead of raising.
//! * A `ByoAppError` is a JSON document (`{"ok": false, "error", "errors"}`),
//!   also with no prefix.
//!
//! The tool is classified read-only by the Python registry (`xpst_byo_app` is
//! not in `_MUTATING_TOOLS`), so a store/clear call is not consent-gated — it
//! writes only the local encrypted store and the config mirror, never a
//! platform account.

use serde_json::{json, Map, Value};
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    // `ctx.config()` materializes a first-run `config.yaml` and reports a broken
    // one the way the Python server's startup load does. The document itself is
    // then read fresh: Python's server holds ONE `XPSTConfig` instance and
    // `store_byo_app` mutates it in place, so a status call after a store sees
    // the new app. A cached `ConfigView` here would answer the pre-store
    // document instead.
    ctx.config()?;
    let document = read_document(ctx)?;
    let store = CredentialStore::new(ctx.paths.config_dir());
    let registry = xpst_surface::byo::registry().map_err(|detail| ToolError::Malformed {
        kind: "byo registry",
        path: String::new(),
        detail,
    })?;

    let platform = arguments
        .get("platform")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();

    if platform.is_empty() {
        let status = xpst_surface::byo::status(&document, &store).map_err(|detail| {
            ToolError::Malformed {
                kind: "byo registry",
                path: String::new(),
                detail,
            }
        })?;
        let mut payload = Map::new();
        payload.insert("ok".to_string(), json!(true));
        payload.insert("action".to_string(), json!("status"));
        if let YamlValue::Mapping(map) = &status {
            for (key, value) in map {
                if let YamlValue::String(key) = key {
                    payload.insert(key.clone(), xpst_config::output::to_json(value));
                }
            }
        }
        return Ok(Value::Object(payload));
    }

    let Some(spec) = registry.spec(&platform) else {
        return Err(ToolError::Reported {
            message: format!("Unknown platform for BYO setup: {platform}"),
        });
    };

    let wants_id = truthy(arguments.get("app_id"));
    let wants_secret = truthy(arguments.get("app_secret"));
    let wants_clear = truthy(arguments.get("clear"));
    if !spec.configurable && (wants_id || wants_clear) {
        return Err(ToolError::Reported {
            message: format!(
                "{} keeps its app credential in its own connect flow; its BYO status is read-only here.",
                spec.display_name
            ),
        });
    }

    let mut document = document;
    if wants_clear {
        let (cleared, changed) = xpst_surface::byo::clear(&mut document, spec, &store);
        if changed {
            write_config(ctx, &document)?;
        }
        let mut payload = Map::new();
        payload.insert("ok".to_string(), json!(true));
        payload.insert("action".to_string(), json!("clear"));
        if let YamlValue::Mapping(map) = &cleared {
            for (key, value) in map {
                if let YamlValue::String(key) = key {
                    payload.insert(key.clone(), xpst_config::output::to_json(value));
                }
            }
        }
        return Ok(Value::Object(payload));
    }

    if wants_id || wants_secret {
        let app_id = arguments
            .get("app_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let app_secret = arguments
            .get("app_secret")
            .and_then(Value::as_str)
            .unwrap_or("");
        let status = match xpst_surface::byo::store(&mut document, spec, &store, app_id, app_secret)
        {
            Ok(status) => status,
            Err(error) => {
                let mut errors = Map::new();
                for (field, message) in &error.errors {
                    errors.insert(field.clone(), json!(message));
                }
                let document = json!({
                    "ok": false,
                    "error": error.message,
                    "errors": Value::Object(errors),
                });
                let message = xpst_config::python_style_json_pretty(&document)
                    .map_err(|detail| ToolError::Handler { message: detail })?;
                return Err(ToolError::Reported { message });
            }
        };
        if !spec.config_id_field.is_empty() {
            write_config(ctx, &document)?;
        }
        let mut payload = Map::new();
        payload.insert("ok".to_string(), json!(true));
        payload.insert("action".to_string(), json!("stored"));
        if let YamlValue::Mapping(map) = &status {
            for (key, value) in map {
                if let YamlValue::String(key) = key {
                    payload.insert(key.clone(), xpst_config::output::to_json(value));
                }
            }
        }
        return Ok(Value::Object(payload));
    }

    let status = xpst_surface::byo::status_for(spec, &document, &store);
    Ok(json!({
        "ok": true,
        "action": "status",
        "platform_status": xpst_config::output::to_json(&status),
    }))
}

/// Python truthiness for a JSON argument: `""`, `0`, `false`, `null`, `[]` and
/// `{}` are all falsy, and `args.get(...)` returns `None` for an absent key.
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

/// The profile's config document, as written.
fn read_document(ctx: &ToolContext) -> Result<YamlValue, ToolError> {
    let path = ctx.paths.config_file();
    xpst_config::ConfigDocument::read(&path)
        .map(|document| document.raw().clone())
        .map_err(|error| ToolError::Malformed {
            kind: "config file",
            path: path.display().to_string(),
            detail: error.to_string(),
        })
}

/// Write the config document back after a mirror write.
///
/// Python's `config.save()` raises on a write failure and nothing catches it,
/// so the tool answers `Error: <sentence>` — `ToolError::Handler`.
fn write_config(ctx: &ToolContext, document: &YamlValue) -> Result<(), ToolError> {
    let path = ctx.paths.config_file();
    xpst_surface::byo::write_config(&path, document)
        .map_err(|message| ToolError::Handler { message })
}
