//! Tool handlers for the read-only, network-free subset of the surface.
//!
//! Every handler here answers from files the Python engine already wrote. None
//! of them opens a socket, prompts a human, or invents a value it could not
//! read — a missing store is reported as missing, never as an empty success.

pub mod config_show;
pub mod providers;
pub mod rate_limit;
pub mod schedule;
mod stamp;
pub mod status;

use serde_json::Value;

use crate::error::ToolError;
use crate::store::StorePaths;

/// What a handler is allowed to see: resolved local paths. Handlers are plain
/// functions over this, so a future phase can swap in another store without
/// touching the registry.
#[derive(Debug, Clone)]
pub struct ToolContext {
    pub paths: StorePaths,
}

impl ToolContext {
    pub fn new(paths: StorePaths) -> Self {
        Self { paths }
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
}

/// A ported tool's implementation.
pub type Handler = fn(&ToolContext, &Value) -> Result<Value, ToolError>;
