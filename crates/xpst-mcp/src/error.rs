//! Typed tool failures.
//!
//! A tool that cannot answer says so in a machine-readable way instead of
//! returning a plausible empty payload. `code` is the stable string an agent
//! can branch on; the human sentence is for the caller's log.

use serde_json::{json, Value};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ToolError {
    /// The caller asked for a tool this server does not serve.
    #[error("{name} is not a tool this server serves")]
    UnknownTool { name: String },

    /// The name is part of the surface (so `tools/list` is honest) but the
    /// behaviour is not ported yet. Never reported as success.
    #[error("{tool} is not implemented yet: {reason}")]
    NotImplemented {
        tool: &'static str,
        reason: &'static str,
    },

    /// The local file this tool reads is missing. Not an error in the state
    /// machine sense, but the tool cannot answer without inventing data.
    #[error("{kind} not found at {path}")]
    NotFound { kind: &'static str, path: String },

    /// A local file exists but cannot be understood.
    #[error("cannot read {kind} at {path}: {detail}")]
    Malformed {
        kind: &'static str,
        path: String,
        detail: String,
    },

    /// Arguments that reached a handler did not satisfy its schema.
    #[error("invalid arguments for {tool}: {detail}")]
    InvalidArguments { tool: &'static str, detail: String },

    /// Refused by the consent tier, not by the tool.
    #[error("{message}")]
    ConsentDenied { tool: String, message: String },
}

impl ToolError {
    /// Stable, machine-readable discriminant.
    pub fn code(&self) -> &'static str {
        match self {
            ToolError::UnknownTool { .. } => "unknown_tool",
            ToolError::NotImplemented { .. } => "not_implemented",
            ToolError::NotFound { .. } => "not_found",
            ToolError::Malformed { .. } => "malformed_store",
            ToolError::InvalidArguments { .. } => "invalid_arguments",
            ToolError::ConsentDenied { .. } => "consent_denied",
        }
    }

    /// The tool this failure belongs to, when the error knows it.
    pub fn tool(&self) -> Option<&str> {
        match self {
            ToolError::UnknownTool { name } => Some(name),
            ToolError::NotImplemented { tool, .. } => Some(tool),
            ToolError::InvalidArguments { tool, .. } => Some(tool),
            ToolError::ConsentDenied { tool, .. } => Some(tool),
            ToolError::NotFound { .. } | ToolError::Malformed { .. } => None,
        }
    }

    /// The text an MCP caller receives. One JSON object, so a caller never has
    /// to parse prose to learn whether something happened.
    pub fn to_payload(&self) -> Value {
        json!({
            "ok": false,
            "error": self.code(),
            "tool": self.tool(),
            "detail": self.to_string(),
        })
    }
}
