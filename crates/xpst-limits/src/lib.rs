//! Publishing rate discipline: the policy in `docs/technical/safe-operating-envelope.md`
//! as typed data, one pure decision function, and a durable cooldown ledger.
//!
//! The point of the crate is the thing that stops an account being limited, so
//! every rule is a value a test can hold, not a comment a reader has to trust:
//!
//! | Rule (policy document) | Where it lives |
//! |---|---|
//! | per-platform interval, cap, ceiling, challenge behaviour | [`envelope`] |
//! | never the same asset on two accounts of one platform | [`decision`] — `SAME_ASSET_ACROSS_ACCOUNTS` |
//! | jitter every interval ±30–50 %, log-normal | [`jitter`] |
//! | hard stop on a challenge, never retry inside a cooldown | [`decision`], [`store`] |
//! | escalation ladder, reset on success, Meta family sharing | [`store`] |
//! | corruption fails closed | [`store`] |
//!
//! The flow a caller is expected to implement:
//!
//! ```no_run
//! use xpst_limits::{Decision, LimitsStore, Platform, PlatformResponse, Request};
//!
//! # fn main() -> Result<(), xpst_limits::StoreError> {
//! let mut store = LimitsStore::open("/tmp/xpst-config")?;
//! let request = Request::new(Platform::X, "fic-account", 1_700_000_000_000)
//!     .with_asset("fic-content-hash-0001");
//!
//! match store.claim(&request)? {
//!     Decision::Allow { .. } => { /* publish, then: */ }
//!     Decision::Defer { until_ms, .. } => { /* requeue at until_ms */ }
//!     Decision::Refuse { code, .. } => { /* surface `code` and stop */ }
//! }
//!
//! // On a rate-limit answer from the platform:
//! store.record_hit(
//!     Platform::X,
//!     "fic-account",
//!     1_700_000_000_000,
//!     PlatformResponse::Throttled { retry_after_ms: Some(900_000), code: None },
//! )?;
//! # Ok(())
//! # }
//! ```
//!
//! Two guarantees worth stating out loud, because both are load-bearing:
//!
//! * **No network, no HTTP, no platform adapters, no CLI.** This crate is
//!   policy and state; translating a real response body into a
//!   [`PlatformResponse`] belongs to the provider crates.
//! * **Determinism.** The decision function takes the clock as an argument and
//!   the jitter is a hash of the key rather than a random draw, so the same
//!   question has the same answer in two processes and two polls.
//!
//! Scope note: "one in-flight write per account per platform" (policy rule 3,
//! first clause) is enforced by the caller serialising its own publishes; this
//! crate contributes the challenge half of that rule. A cross-process in-flight
//! lock is a separate concern from a rate envelope and is deliberately not
//! folded in here.
//!
//! Timestamps are `i64` milliseconds since the Unix epoch throughout.

#![forbid(unsafe_code)]

pub mod clock;
pub mod decision;
pub mod envelope;
pub mod jitter;
pub mod store;

pub use clock::{next_pacific_midnight_ms, now_ms, DAY_MS, HOUR_MS, MINUTE_MS, SECOND_MS};
pub use decision::{evaluate, AccountView, Decision, RefusalCode, Request, Rule, View};
pub use envelope::{
    envelope, Ceiling, ChallengeBehaviour, Confidence, Envelope, Family, Platform, SameAssetPolicy,
    SameAssetRisk, ENVELOPES,
};
pub use jitter::{jittered_interval_ms, multiplier};
pub use store::{
    ladder_pause_ms, AccountStatus, AppliedCooldown, HitOutcome, Integrity, LimitsStore,
    PlatformResponse, StoreError, ASSET_RETENTION_MS, LADDER_CAP_MS, LADDER_MS, LOCK_FILE_NAME,
    MAX_DEFERRED_RETRY_MS, STATE_FILE_NAME, STATE_VERSION, STRIKE_DECAY_MS,
};
