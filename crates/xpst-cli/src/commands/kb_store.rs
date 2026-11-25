//! The knowledge-base JSON store, read the way the Python engine reads it.
//!
//! The loader lives in `xpst-surface::kb_store` because the MCP tools
//! `kb_query` and `xpst_search` read the same store and `xpst-mcp` cannot
//! depend on this crate.

pub use xpst_surface::kb_store::*;
