//! Commands the Rust engine actually implements.
//!
//! The list lives in [`crate::implemented::IMPLEMENTED`] — one table that both
//! `dispatch` and the generated parity ledger read, so an implemented command
//! cannot be missing from the doc and a doc row cannot invent one.
//!
//! Everything else in the tree is declared-but-unimplemented and answers with
//! `not implemented in the Rust engine yet` (see `crate::not_implemented`).

pub mod bootstrap;
pub mod byo;
pub mod capabilities;
pub mod chat;
pub mod config;
pub mod doctor;
pub mod failures;
pub mod media;
pub mod schedule;
pub mod status;
pub mod version;
