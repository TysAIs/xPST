//! Resolve (and, for a first run, materialize) the config file.
//!
//! `status`, `schedule list` and `failures list` need only the *directory* the
//! engine keeps its files in, but Python resolves it through `XPSTConfig.load`,
//! which has one behaviour worth keeping: on a profile that has never been
//! used, the default `config.yaml` is created rather than the command failing.
//! A fresh install has no config until something writes it, and `xpst status`
//! is the first thing an install or QA lane runs.
//!
//! What is *not* reproduced: schema migration, Pydantic validation and the
//! secret store. Those belong to the config write path; this helper only has to
//! answer "which file, and does it parse".
//!
//! The MCP server loads its config through [`ensure_config_at`] too, because
//! `xpst.mcp.cli_main` calls `XPSTConfig.load(config_path)` — the same
//! first-run materialization, from the same file.

use std::path::{Path, PathBuf};

use xpst_config::ConfigDocument;

use crate::paths;

/// The document the Python engine writes on first run.
///
/// Generated, not hand-written: `xtask/scripts/capture_default_config.py` runs
/// the shipped `DEFAULT_CONFIG` through the same `yaml.safe_dump` the engine
/// uses, and the output is byte-identical to the file a fresh `xpst status`
/// leaves behind.
pub const DEFAULT_CONFIG_YAML: &str = include_str!("resources/default_config.yaml");

/// Why the config could not be resolved.
#[derive(Debug)]
pub enum BootstrapError {
    /// An explicit `--config` path that does not exist. Python refuses this
    /// instead of creating a file the user did not ask for.
    ExplicitPathMissing(String),
    /// The profile directory is unknown (`XPST_CONFIG_DIR` and `HOME` unset).
    NoProfile(String),
    /// Reading or creating the file failed.
    Io(String),
    /// The file exists but is not readable as an xPST config.
    Invalid(String),
}

impl BootstrapError {
    /// One sentence a caller can print; the message already names the fix.
    pub fn message(&self) -> String {
        match self {
            BootstrapError::ExplicitPathMissing(path) => format!(
                "XPST config file not found: {path} (expected {} or a path passed via --config)",
                paths::config_file()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| "~/.xpst/config.yaml".to_string())
            ),
            BootstrapError::NoProfile(message) => message.clone(),
            BootstrapError::Io(message) => message.clone(),
            BootstrapError::Invalid(message) => message.clone(),
        }
    }
}

/// Resolve the config path, creating the first-run default when it is absent.
///
/// `explicit` is the `--config` value (or the MCP server's `config_path`).
pub fn ensure_config_at(explicit: Option<&Path>) -> Result<PathBuf, BootstrapError> {
    let path = match explicit {
        Some(path) => path.to_path_buf(),
        None => paths::config_file().ok_or_else(|| {
            BootstrapError::NoProfile(
                "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set"
                    .to_string(),
            )
        })?,
    };

    if !path.exists() {
        if explicit.is_some() {
            return Err(BootstrapError::ExplicitPathMissing(
                path.display().to_string(),
            ));
        }
        materialize(&path)?;
    }

    // Parse check only: a config that is not valid YAML is a config error, and
    // Python exits 2 for it too.
    ConfigDocument::read(&path).map_err(|error| BootstrapError::Invalid(error.to_string()))?;
    Ok(path)
}

/// Resolve `<dir>/config.yaml`, materializing the first-run default when it is
/// absent.
///
/// The MCP server uses this: it is handed a directory (not a `--config` flag)
/// and must behave like `XPSTConfig.load` on a fresh profile.
pub fn ensure_config_in(dir: &Path) -> Result<PathBuf, BootstrapError> {
    let path = dir.join("config.yaml");
    if !path.exists() {
        materialize(&path)?;
    }
    ConfigDocument::read(&path).map_err(|error| BootstrapError::Invalid(error.to_string()))?;
    Ok(path)
}

/// Write the default document the way the Python engine does: owner-only mode,
/// atomic replacement, directory created if the profile is new.
fn materialize(path: &Path) -> Result<(), BootstrapError> {
    let dir = path.parent().ok_or_else(|| {
        BootstrapError::Io(format!("config path {} has no directory", path.display()))
    })?;
    let created = !dir.exists();
    if created {
        std::fs::create_dir_all(dir).map_err(|error| {
            BootstrapError::Io(format!(
                "XPST could not create its config directory {}: {error}. \
                 Fix: make its parent writable, or set XPST_CONFIG_DIR to a writable directory.",
                dir.display()
            ))
        })?;
    }
    xpst_state::store::write_atomic_bytes(path, DEFAULT_CONFIG_YAML.as_bytes()).map_err(
        |error| {
            BootstrapError::Io(format!(
                "XPST could not create its config file {}: {error}. \
             Fix: make {} writable, or set XPST_CONFIG_DIR to a writable directory.",
                path.display(),
                dir.display()
            ))
        },
    )?;
    restrict_to_owner(path);
    if created {
        restrict_to_owner_dir(dir);
    }
    Ok(())
}

#[cfg(unix)]
fn restrict_to_owner(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_to_owner(_path: &Path) {}

#[cfg(unix)]
fn restrict_to_owner_dir(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
}

#[cfg(not(unix))]
fn restrict_to_owner_dir(_path: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_embedded_document_is_the_python_first_run_file() {
        // A capture that silently produced a stub would make every fresh
        // profile look like a broken install, so assert the shape the engine
        // needs rather than only that the file exists.
        assert!(DEFAULT_CONFIG_YAML.starts_with("version: 4"));
        for key in ["accounts:", "youtube:", "monitoring:", "schedule:"] {
            assert!(
                DEFAULT_CONFIG_YAML.contains(key),
                "default config is missing `{key}`"
            );
        }
        assert!(
            DEFAULT_CONFIG_YAML.len() > 2_000,
            "default config looks truncated: {} bytes",
            DEFAULT_CONFIG_YAML.len()
        );
    }

    #[test]
    fn materialize_writes_an_owner_only_readable_document() {
        let dir = std::env::temp_dir().join(format!("xpst-bootstrap-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("config.yaml");
        materialize(&path).expect("writes the default document");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), DEFAULT_CONFIG_YAML);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600, "config holds credentials; keep it owner-only");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
