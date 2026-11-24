//! Resolve (and, for a first run, materialize) the config file.
//!
//! The resolver lives in `xpst-surface::bootstrap` because the MCP server loads
//! its config the same way (`xpst.mcp.cli_main` calls `XPSTConfig.load`) and
//! `xpst-mcp` cannot depend on this crate. This module adds the CLI's
//! `--config` handling and the error rendering.

use std::io::Write;
use std::path::PathBuf;

pub use xpst_surface::bootstrap::{BootstrapError, DEFAULT_CONFIG_YAML};

use crate::paths;
use crate::GlobalOptions;

/// Resolve the config path, creating the first-run default when it is absent.
pub fn ensure_config(global: &GlobalOptions) -> Result<PathBuf, BootstrapError> {
    let explicit = global.config_path.as_ref().map(PathBuf::from);
    xpst_surface::bootstrap::ensure_config_at(explicit.as_deref())
}

/// Report a bootstrap failure in the shape the caller asked for.
///
/// Python prints a rich panel and exits 2, which is not machine-readable even
/// under `--json`; the Rust engine answers with the same error payload shape
/// every other implemented command uses.
pub fn report(
    err: &mut dyn Write,
    json_mode: bool,
    out: &mut dyn Write,
    error: &BootstrapError,
) -> i32 {
    let message = error.message();
    if json_mode {
        let code = match error {
            BootstrapError::ExplicitPathMissing(_) => "CONFIG_FILE_NOT_FOUND",
            _ => "CONFIG_ERROR",
        };
        let payload = crate::output::error_payload_with(
            code,
            &message,
            serde_json::json!({"path": config_path_hint(error)}),
        );
        let _ = crate::output::write_python_json_value(out, &payload);
    } else {
        let _ = writeln!(err, "Configuration error: {message}");
    }
    crate::exit::CONFIG_ERROR
}

fn config_path_hint(error: &BootstrapError) -> String {
    match error {
        BootstrapError::ExplicitPathMissing(path) => path.clone(),
        _ => paths::config_file()
            .map(|path| path.display().to_string())
            .unwrap_or_default(),
    }
}
