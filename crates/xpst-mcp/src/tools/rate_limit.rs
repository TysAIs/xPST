//! `xpst_rate_limit_status` — active rate-limit cooldowns.
//!
//! The report itself lives in `xpst-limits::cooldowns` (the crate that owns
//! rate-limit state), so `xpst doctor`, `xpst health` and this tool render the
//! same document from one implementation. This module is the MCP adapter: it
//! resolves the profile's cooldown file and maps the read into the tool's error
//! type.
//!
//! Read-only by construction: one file read, no sleep, no platform probe.

use jiff::Timestamp;
use serde_json::Value;

use crate::error::ToolError;

use super::ToolContext;

pub use xpst_limits::cooldowns::{format_duration, parse_instant, COOLDOWN_SEMANTICS};

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    report(&ctx.paths.cooldown_file(), Timestamp::now())
}

/// Build the report for one cooldown file at a given instant.
///
/// A missing or corrupt file is reported as idle rather than raised: an
/// operator asking for health must never get an error because a cooldown file
/// is unreadable.
pub fn report(path: &std::path::Path, now: Timestamp) -> Result<Value, ToolError> {
    Ok(xpst_limits::cooldown_report(Some(path), now))
}
