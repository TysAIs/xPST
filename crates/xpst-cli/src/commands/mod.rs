//! Commands the Rust engine actually implements in P0.
//!
//! The list is short by design: `config show`, `config validate`, `doctor` and
//! `version`. Everything else in the tree is declared-but-unimplemented and
//! answers with `not implemented in the Rust engine yet` (see
//! `crate::not_implemented`).

pub mod config;
pub mod doctor;
pub mod version;
