//! The auth / readiness TRUTH TABLE for the Rust engine (P1).
//!
//! xPST's most expensive historical bug class is a status surface that tells a
//! different story from the engine: an expired Instagram session reported as
//! `authenticated: true`, a healthy YouTube token demanding re-auth because it
//! had simply never been checked, a TikTok download source advertised as a
//! posting destination. Every one of those was a surface inventing a state
//! instead of reading it.
//!
//! This crate makes the states and the transitions between them typed data:
//!
//! * [`ProviderState`] — the canonical per-role verdict;
//! * [`TokenState`] / [`Badge`] — the credential truth and what a human sees;
//! * [`BanRisk`] / [`Severity`] — the advisory for an unofficial auth path;
//! * [`PlatformInput`] — the config-shaped facts (credential PRESENCE flags and
//!   a probe verdict) the table is allowed to depend on;
//! * [`evaluate`] — the pure function that maps one provider's facts onto all
//!   of the above.
//!
//! Scope is deliberately read-only. Nothing here refreshes a token, performs
//! HTTP, mutates a credential, or reads a clock — a caller supplies `now`. The
//! Python engine remains the executable specification until the phase that
//! owns the probes lands, so `tests/fixture_parity.rs` asserts every row
//! captured from it (`tests/fixtures/auth_truth_table.json`, produced by
//! `scripts/capture_auth_truth_table.py`) is reproduced exactly.

mod facts;
mod state;
mod table;

pub use facts::{CredentialFacts, PlatformInput, ProbeClass, ProbeDetails, ProbeResult};
pub use state::{Badge, BanRisk, Platform, ProviderState, Role, Severity, TokenState};
pub use table::{evaluate, BanRiskAdvisory, Verdict};
