//! Output helpers shared by the implemented read-only commands.
//!
//! The helpers live in `xpst-config::output` because the MCP server renders the
//! same Python-style JSON and `xpst-mcp` cannot depend on this crate. This
//! module is the CLI's name for them.

pub use xpst_config::output::*;
