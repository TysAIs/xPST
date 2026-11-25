//! `xpst_disconnect` — remove a platform's stored credentials and disable it.
//!
//! `_handle_disconnect()` is `disconnect_platform(platform, config)` plus
//! `json.dumps(result, indent=2, default=str)`. The engine half lives in
//! `xpst_surface::disconnect`; this module is the tool's projection: the
//! argument shape, the config read and the error mapping.

use serde_json::Value;

use xpst_auth::credentials::CredentialStore;
use xpst_surface::config_save::SaveRequest;
use xpst_surface::disconnect::disconnect;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    // `args["platform"]` is a hard index: a missing key is a `KeyError`, whose
    // sentence is the quoted key.
    let Some(platform) = arguments.get("platform") else {
        return Err(ToolError::Handler {
            message: "'platform'".to_string(),
        });
    };
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

    // The raw argument is echoed back in the payload, so it is converted to the
    // config value type rather than to a string.
    let platform = xpst_config::output::from_json(platform);
    let outcome = disconnect(&platform, Some(&file), &request)
        .map_err(|message| ToolError::Handler { message })?;
    Ok(xpst_config::output::to_json(&outcome.to_payload()))
}
