//! The resolved config view, plus the CLI's `--config` entry point.
//!
//! The view itself lives in `xpst-surface::config_view` because the MCP server
//! builds the same view and `xpst-mcp` cannot depend on this crate. This module
//! re-exports it and adds [`resolve`], which needs the CLI's `--config` flag.

pub use xpst_surface::config_view::*;

use std::path::{Path, PathBuf};

use crate::commands::bootstrap;
use crate::GlobalOptions;

/// Resolve the config view for a command: materialize/parse the config file the
/// way [`bootstrap::ensure_config`] does, then build the view with the process
/// environment.
///
/// This is the one entry point every ported command that reads config should
/// use, so the Python load order (`file → legacy fix → env → rebase → expand`)
/// is applied exactly once and in one place.
pub fn resolve(global: &GlobalOptions) -> Result<ConfigView, bootstrap::BootstrapError> {
    let path = bootstrap::ensure_config(global)?;
    let document = xpst_config::ConfigDocument::read(&path)
        .map_err(|error| bootstrap::BootstrapError::Invalid(error.to_string()))?
        .raw()
        .clone();
    let config_dir = path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let env = |name: &str| std::env::var(name).ok();
    ConfigView::load(&document, &config_dir, &env).map_err(bootstrap::BootstrapError::Invalid)
}
