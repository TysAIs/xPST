//! Consent tiering, ported from `_guardrail_block()` in
//! `src/xpst/mcp/server.py`.
//!
//! The MCP surface is local and unauthenticated; any connected agent can call
//! it. Mutating tools are therefore refused unless the operator opted in, and
//! the refusal text is copied verbatim from Python so an agent that already
//! handles the Python server's advice handles this one unchanged.

use std::collections::BTreeSet;

use crate::error::ToolError;
use crate::registry::{Consent, ToolEntry};

/// Tools that mutate state or post to REAL accounts (`_MUTATING_TOOLS` in the
/// Python registry). Kept as one list here and asserted against the Python
/// source by `tests/parity.rs`.
pub const MUTATING_TOOLS: &[&str] = &[
    "xpst_run",
    "xpst_post",
    "xpst_backfill",
    "xpst_delete",
    "xpst_schedule_add",
    "xpst_disconnect",
    "messenger_send",
    "messenger_set_rules",
    "xpst_messenger_check_comments",
    "kb_add",
    "kb_organize",
    "xpst_schedule_cancel",
    "xpst_failures_retry",
    "xpst_schedule_resume",
];

/// The consent decision for one call, decided from the operator's environment.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ConsentPolicy {
    /// `XPST_MCP_READONLY`: refuse every mutating tool outright.
    pub read_only: bool,
    /// `XPST_MCP_ALLOW_MUTATIONS`: the operator accepted mutations.
    pub allow_mutations: bool,
    /// `XPST_MCP_REQUIRE_CONFIRM`: accept a per-call `"confirm": true`.
    pub require_confirm: bool,
}

fn truthy(value: Option<&str>) -> bool {
    matches!(
        value.map(str::to_ascii_lowercase).as_deref(),
        Some("1" | "true" | "yes")
    )
}

impl ConsentPolicy {
    /// Read the three variables the Python server reads.
    pub fn from_env() -> Self {
        Self {
            read_only: truthy(std::env::var("XPST_MCP_READONLY").ok().as_deref()),
            allow_mutations: truthy(std::env::var("XPST_MCP_ALLOW_MUTATIONS").ok().as_deref()),
            require_confirm: truthy(std::env::var("XPST_MCP_REQUIRE_CONFIRM").ok().as_deref()),
        }
    }

    /// `read_only` wins, exactly as in Python.
    pub fn permits(&self, entry: &ToolEntry) -> Result<(), ToolError> {
        if entry.consent == Consent::ReadOnly {
            return Ok(());
        }
        let name = entry.name;
        if self.read_only {
            return Err(ToolError::ConsentDenied {
                tool: name.to_string(),
                message: format!(
                    "Blocked: {name} is disabled (XPST_MCP_READONLY is set). \
                     Unset it to allow posting/mutating tools."
                ),
            });
        }
        if !self.allow_mutations && !self.require_confirm {
            return Err(ToolError::ConsentDenied {
                tool: name.to_string(),
                message: format!(
                    "{name} posts to or mutates REAL accounts. \
                     Set XPST_MCP_ALLOW_MUTATIONS=1 to allow, or \
                     XPST_MCP_REQUIRE_CONFIRM=1 to require per-call confirmation."
                ),
            });
        }
        Ok(())
    }

    /// Per-call confirmation, checked after [`Self::permits`].
    pub fn confirms(
        &self,
        entry: &ToolEntry,
        arguments: &serde_json::Value,
    ) -> Result<(), ToolError> {
        if entry.consent == Consent::ReadOnly || !self.require_confirm {
            return Ok(());
        }
        if arguments
            .get("confirm")
            .and_then(serde_json::Value::as_bool)
            == Some(true)
        {
            return Ok(());
        }
        Err(ToolError::ConsentDenied {
            tool: entry.name.to_string(),
            message: format!(
                "{} posts to or mutates REAL accounts and XPST_MCP_REQUIRE_CONFIRM is set. \
                 Re-call with \"confirm\": true to proceed.",
                entry.name
            ),
        })
    }
}

/// Names classified as mutating, for the parity test.
pub fn mutating_names() -> BTreeSet<&'static str> {
    MUTATING_TOOLS.iter().copied().collect()
}
