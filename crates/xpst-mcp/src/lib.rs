//! rmcp tool surface, same tool names and schemas (P4).
//!
//! The product rule this crate implements: *every capability the UI has, the
//! agent has.* The Python engine serves 46 MCP tools; the Rust surface must
//! eventually serve the same names with the same semantics, and it may never
//! claim a tool it does not implement.
//!
//! That rule is enforced structurally, not by review:
//!
//! * [`registry`] embeds the tool definitions captured from the Python
//!   registry (`tests/fixtures/python_tool_registry.json`), so names and input
//!   schemas cannot be hand-written into drift.
//! * Every row states its [`registry::Implementation`]: a ported handler, or a
//!   stub with the reason it is not ported. A stub can only answer with a
//!   typed `not_implemented` error — there is no path from a stub to a
//!   plausible-looking success.
//! * [`guard`] carries the consent tier (read-only vs mutating) and refuses
//!   mutating calls unless the operator opted in, matching the Python server's
//!   `_guardrail_block`.
//! * `tests/parity.rs` fails on drift against `src/xpst/mcp/server.py`.
//!
//! This slice ports the read-only tools that need no network and no platform
//! credentials: providers, config show (masked), status, rate-limit status and
//! schedule list. Everything else is an honest stub.

pub mod error;
pub mod guard;
pub mod registry;
pub mod server;
pub mod store;
pub mod tools;

pub use error::ToolError;
pub use registry::{registry, Consent, Implementation, Registry, ToolEntry};
pub use server::{XpstMcp, SUPPORTED_PROTOCOL_VERSIONS};
pub use store::StorePaths;
pub use tools::ToolContext;
