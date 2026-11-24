//! Default runtime paths.
//!
//! The resolver lives in `xpst-surface::paths` because the MCP server resolves
//! the same profile and `xpst-mcp` cannot depend on this crate. This module is
//! the CLI's name for it.

pub use xpst_surface::paths::*;
