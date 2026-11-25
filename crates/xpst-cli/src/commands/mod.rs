//! Commands the Rust engine actually implements.
//!
//! The list lives in [`crate::implemented::IMPLEMENTED`] — one table that both
//! `dispatch` and the generated parity ledger read, so an implemented command
//! cannot be missing from the doc and a doc row cannot invent one.
//!
//! Everything else in the tree is declared-but-unimplemented and answers with
//! `not implemented in the Rust engine yet` (see `crate::not_implemented`).

pub mod app;
pub mod best_time;
pub mod bio;
pub mod bootstrap;
pub mod byo;
pub mod capabilities;
pub mod chat;
pub mod config;
pub mod config_view;
pub mod devices;
pub mod doctor;
pub mod failures;
pub mod generate;
pub mod kb;
pub mod kb_store;
pub mod logs;
pub mod mcp;
pub mod media;
pub mod messenger;
pub mod providers;
pub mod quota;
pub mod schedule;
pub mod security;
pub mod state;
pub mod status;
pub mod stream;
pub mod version;
