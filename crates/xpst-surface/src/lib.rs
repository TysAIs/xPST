//! Payload builders shared by the CLI and the MCP server.
//!
//! `xpst-cli` depends on `xpst-mcp` (it serves the MCP surface from
//! `xpst mcp start`), so the MCP crate can never depend on the CLI. Every
//! document both surfaces serve therefore lives here, below both of them:
//! one implementation, two renderers, no second copy to drift.
//!
//! Nothing in this crate opens a socket, prompts a human, or invents a value it
//! could not read. A missing store is reported as missing, never as an empty
//! success.

pub mod analytics;
pub mod best_time;
pub mod bio;
pub mod bootstrap;
pub mod byo;
pub mod capabilities;
pub mod catalog;
pub mod chat;
pub mod config_view;
pub mod content_request;
pub mod cross_post;
pub mod followers;
pub mod generate;
pub mod isotime;
pub mod kb;
pub mod kb_store;
pub mod paths;
pub mod preflight;
pub mod provider_status;
pub mod readiness;
pub mod schedule;
pub mod security;
pub mod setup_transaction;
pub mod stream;
pub mod transcript;
