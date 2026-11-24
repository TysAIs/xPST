//! `messenger_set_rules` — persist the Messenger auto-reply rules.
//!
//! `_handle_messenger_set_rules()` coerces the arguments, sets two fields on the
//! config and calls `config.save()`. The coercion order and the whole-file write
//! live in `xpst_surface::messenger_rules`; this module is the tool's projection.

use serde_json::Value;

use xpst_auth::credentials::CredentialStore;
use xpst_surface::config_save::SaveRequest;
use xpst_surface::messenger_rules::set_rules;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let config_dir = ctx.paths.config_dir();
    let path = xpst_surface::bootstrap::ensure_config_in(config_dir).map_err(|error| {
        ToolError::Handler {
            message: error.message(),
        }
    })?;
    let file = xpst_config::ConfigDocument::read(&path)
        .map_err(|error| ToolError::Handler {
            message: error.to_string(),
        })?
        .raw()
        .clone();

    let env = |name: &str| std::env::var(name).ok();
    let home = env("HOME").unwrap_or_default();
    let store =
        |dir: &str, key: &str, value: &str| CredentialStore::new(dir).store(key, value).is_ok();
    let request = SaveRequest {
        config_dir,
        home: std::path::Path::new(&home),
        env: &env,
        store_secret: &store,
    };

    let outcome =
        set_rules(arguments, Some(&file), &request).map_err(|error| ToolError::Handler {
            message: error.message,
        })?;
    Ok(xpst_config::output::to_json(&outcome.to_payload()))
}
