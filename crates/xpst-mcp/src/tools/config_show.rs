//! `xpst_config_show` — the configuration with secrets masked.
//!
//! Mirrors `_handle_config_show()`: the same four sections, values passed
//! through the ported masker (`xpst_config::mask_sensitive`, which mirrors
//! `_mask_sensitive_values()` key-for-key). A missing config file is reported
//! as missing rather than rendered as default values the Rust side has not
//! ported yet.

use serde_json::{json, Map, Value};

use crate::error::ToolError;
use crate::store::StorePaths;

use super::ToolContext;

/// Platforms rendered under `accounts` (the Python projection's list).
const ACCOUNT_SECTIONS: &[&str] = &[
    "tiktok",
    "youtube",
    "x",
    "instagram",
    "threads",
    "messenger",
    "local",
];

/// Sections rendered as-is (masked).
const PLAIN_SECTIONS: &[&str] = &["video", "monitoring", "schedule"];

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let path = ctx.paths.config_file();
    let path_text = path.display().to_string();
    let document = match xpst_config::ConfigDocument::read(&path) {
        Ok(document) => document,
        Err(xpst_config::ConfigError::NotFound { .. }) => {
            return Err(ToolError::NotFound {
                kind: "config file",
                path: path_text,
            })
        }
        Err(err) => {
            return Err(ToolError::Malformed {
                kind: "config file",
                path: path_text,
                detail: err.to_string(),
            })
        }
    };

    let masked = serde_json::to_value(document.masked()).map_err(|err| ToolError::Malformed {
        kind: "config file",
        path: path_text,
        detail: err.to_string(),
    })?;
    Ok(project(masked))
}

/// The Python tool returns `accounts.*`, `video`, `monitoring` and `schedule`
/// rather than the whole document. Unknown top-level keys are omitted, not
/// leaked — same narrow projection, so the tool cannot disclose a section the
/// Python surface does not.
fn project(masked: Value) -> Value {
    let object = masked.as_object();
    let mut accounts = Map::new();
    let mut payload = Map::new();

    if let Some(object) = object {
        for platform in ACCOUNT_SECTIONS {
            if let Some(section) = object.get(*platform) {
                accounts.insert((*platform).to_string(), section.clone());
            }
        }
    }
    payload.insert("accounts".to_string(), Value::Object(accounts));

    for section in PLAIN_SECTIONS {
        let value = object
            .and_then(|object| object.get(*section))
            .cloned()
            .unwrap_or_else(|| json!({}));
        payload.insert((*section).to_string(), value);
    }

    Value::Object(payload)
}

/// The resolved config path is useful to an operator but is not part of the
/// Python payload; exposed for tests and the README rather than the wire.
pub fn config_path(paths: &StorePaths) -> String {
    paths.config_file().display().to_string()
}
