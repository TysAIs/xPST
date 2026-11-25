//! Tool handlers for the read-only, network-free subset of the surface.
//!
//! Every handler here answers from files the Python engine already wrote. None
//! of them opens a socket, prompts a human, or invents a value it could not
//! read — a missing store is reported as missing, never as an empty success.
//!
//! The payload builders live in `xpst-surface` because the CLI serves the same
//! documents and `xpst-cli` depends on this crate, not the other way round.

pub mod activity;
pub mod auth_start;
pub mod best_time;
pub mod bio;
pub mod byo_app;
pub mod capabilities;
pub mod chat;
pub mod config_show;
pub mod followers;
pub mod kb;
pub mod preflight;
pub mod providers;
pub mod rate_limit;
pub mod readiness;
pub mod schedule;
pub mod security;
pub mod setup;
mod stamp;
pub mod status;
pub mod stream;
pub mod transcript;

use std::sync::OnceLock;

use serde_json::Value;
use xpst_surface::config_view::ConfigView;

use crate::error::ToolError;
use crate::store::StorePaths;

/// What a handler is allowed to see: resolved local paths, plus the config view
/// the config-reading tools share.
///
/// The view is loaded on first use, not at startup: the Python server loads its
/// config once (`xpst.mcp.cli_main` → `XPSTConfig.load`), and a tool that never
/// reads config must not fail because the file is broken. A load failure is
/// cached too, so a broken config reports the same error on every call instead
/// of re-reading the file.
pub struct ToolContext {
    pub paths: StorePaths,
    config: OnceLock<Result<ConfigView, String>>,
}

impl std::fmt::Debug for ToolContext {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ToolContext")
            .field("paths", &self.paths)
            .field("config_loaded", &self.config.get().is_some())
            .finish()
    }
}

impl ToolContext {
    pub fn new(paths: StorePaths) -> Self {
        Self {
            paths,
            config: OnceLock::new(),
        }
    }

    /// Resolve the real config dir. Only the binary does this; tests pass an
    /// explicit directory.
    pub fn discover() -> Result<Self, ToolError> {
        StorePaths::discover()
            .map(Self::new)
            .map_err(|detail| ToolError::Malformed {
                kind: "config directory",
                path: String::new(),
                detail,
            })
    }

    /// The operator's config, resolved the way `XPSTConfig.load` resolves it:
    /// a first run materializes the default `config.yaml`, then the file is
    /// parsed and the environment overrides applied.
    pub fn config(&self) -> Result<&ConfigView, ToolError> {
        let loaded = self.config.get_or_init(|| {
            let dir = self.paths.config_dir();
            let path =
                xpst_surface::bootstrap::ensure_config_in(dir).map_err(|error| error.message())?;
            let document = xpst_config::ConfigDocument::read(&path)
                .map_err(|error| error.to_string())?
                .raw()
                .clone();
            let env = |name: &str| std::env::var(name).ok();
            ConfigView::load(&document, dir, &env)
        });
        loaded.as_ref().map_err(|detail| ToolError::Malformed {
            kind: "config file",
            path: self.paths.config_file().display().to_string(),
            detail: detail.clone(),
        })
    }
}

/// A ported tool's implementation.
pub type Handler = fn(&ToolContext, &Value) -> Result<Value, ToolError>;
