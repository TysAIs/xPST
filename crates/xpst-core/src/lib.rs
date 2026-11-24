//! Engine orchestration: pipeline, retry ownership, cooldowns (P2+).
//!
//! P0 scope: this crate exists so the workspace shape from
//! `docs/PROPOSAL-RUST-REBUILD.md` §2 compiles and is reviewable. No behavior is
//! declared here until the phase that owns it lands a test for it.
//!
//! Declared so far: the content vocabulary and the per-destination video format
//! envelopes ([`content`], [`envelope`]), the ingest specs a file is measured
//! against ([`ingest`]), and the per-destination publish decision that consumes
//! them ([`plan`]) — all ported from `src/xpst/content.py`,
//! `src/xpst/media/specs.py` and `src/xpst/services/route_plan.py`, and pinned
//! row-by-row by `tests/content_parity.rs` and `tests/plan_parity.rs` against
//! fixtures captured by running the Python functions.

pub mod content;
pub mod envelope;
pub mod ingest;
pub mod plan;
pub mod preflight;
pub mod visibility;
