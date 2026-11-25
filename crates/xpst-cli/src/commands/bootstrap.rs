//! Resolve (and, for a first run, materialize) the config file.
//!
//! The resolver lives in `xpst-surface::bootstrap` because the MCP server loads
//! its config the same way (`xpst.mcp.cli_main` calls `XPSTConfig.load`) and
//! `xpst-mcp` cannot depend on this crate. This module adds the CLI's
//! `--config` handling and the error rendering.

use std::io::Write;
use std::path::{Path, PathBuf};

pub use xpst_surface::bootstrap::{BootstrapError, DEFAULT_CONFIG_YAML};

use xpst_config::{ConfigDocument, LoadError};

use crate::paths;
use crate::GlobalOptions;

/// Resolve the config path, creating the first-run default when it is absent,
/// and load it the way `XPSTConfig.load` does.
///
/// The document is returned rather than re-read by the caller: a second read
/// would be a second chance to back up the same corrupt file, and the two
/// engines must produce exactly one `backups/config.yaml.corrupt_<epoch>`.
pub fn load_document(global: &GlobalOptions) -> Result<(PathBuf, ConfigDocument), BootstrapError> {
    // `cfg_path: Path = Path(config_path)` — Python normalises before the
    // existence check, so `--config ./config.yaml` and `--config config.yaml/`
    // read the file instead of reporting it missing, and the message and the
    // backup directory carry the normalised form.
    let explicit = global
        .config_path
        .as_deref()
        .map(Path::new)
        .map(xpst_config::python_path);
    let path = match &explicit {
        Some(path) => path.clone(),
        None => default_config_file()?,
    };

    if !path.exists() {
        if explicit.is_some() {
            return Err(BootstrapError::ExplicitPathMissing(
                path.display().to_string(),
            ));
        }
        xpst_surface::bootstrap::materialize_at(&path)?;
    }

    let document = ConfigDocument::load(&path).map_err(|error| match error {
        // A file that vanished between the existence check and the read is the
        // same user error as an explicit path that never existed.
        LoadError::NotFound { path } => BootstrapError::ExplicitPathMissing(path),
        other => BootstrapError::Load(other),
    })?;
    Ok((path, document))
}

/// Resolve the config path, creating the first-run default when it is absent.
pub fn ensure_config(global: &GlobalOptions) -> Result<PathBuf, BootstrapError> {
    load_document(global).map(|(path, _)| path)
}

/// The default config file, normalised the way `Path(...)` normalises it.
///
/// `XPST_CONFIG_DIR=./cfg` is a legal override, and Python's
/// `get_config_dir()` runs it through `Path()` before joining `config.yaml`,
/// so the message must not carry the raw `./cfg/config.yaml`.
fn default_config_file() -> Result<PathBuf, BootstrapError> {
    let file = paths::config_file().ok_or_else(|| {
        BootstrapError::NoProfile(
            "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set"
                .to_string(),
        )
    })?;
    Ok(xpst_config::python_path(&file))
}

/// Report a bootstrap failure the way `load_config()` does.
///
/// Python's `load_config` catches `ValueError` and prints
/// `Configuration error: {e}`, catches everything else (a `FileNotFoundError`
/// for an explicit `--config`) and prints `Failed to load config: {e}`, then
/// exits 2. Both go through `console.print`, so they land on **stdout** and are
/// word-wrapped at the console width — even under `--json`, because the print
/// happens before any JSON is produced. A corrupt file also logs the backup
/// notice to stderr first, exactly where `_backup_corrupt_config_file` logs it.
pub fn report(out: &mut dyn Write, err: &mut dyn Write, error: &BootstrapError) -> i32 {
    let (prefix, message) = match error {
        BootstrapError::ExplicitPathMissing(_) => ("Failed to load config: ", error.message()),
        _ => ("Configuration error: ", error.message()),
    };
    if let BootstrapError::Load(load) = error {
        if let Some(notice) = load.backup_notice() {
            let _ = crate::rich::log_error(err, &notice);
        }
    }
    let _ = crate::rich::print_error(out, prefix, &message);
    crate::exit::CONFIG_ERROR
}
