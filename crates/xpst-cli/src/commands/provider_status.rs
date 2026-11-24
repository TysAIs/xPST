//! The canonical provider status shared by `providers`, `readiness` and
//! `diagnostics`.
//!
//! The implementation lives in `xpst-surface::provider_status` because the MCP
//! server serves the same status and `xpst-mcp` cannot depend on this crate.

pub use xpst_surface::provider_status::*;
