//! The publishing pipeline: a frozen plan, published one destination at a time.
//!
//! ```text
//! RouteRequest ──plan──▶ RoutePlan ──confirm──▶ ConfirmedPlan ──publish──▶ PlanReport
//! ```
//!
//! Three steps, three values, no hidden state:
//!
//! * [`Pipeline::plan`] decides per destination whether this content may be
//!   published there at all (the route planner in `xpst-core`, unchanged).
//! * [`Pipeline::confirm`] freezes that decision into per-destination intents
//!   and gives each one an idempotency key. A refusal is frozen too — as a
//!   refusal, never as a pending attempt.
//! * [`Pipeline::publish`] executes what is left, in the only safe order:
//!   **limits decision → provider call → honest outcome → state write**. It
//!   reports one entry per destination, with a count bucket per outcome, and a
//!   dedupe no-op is never counted as a post.
//!
//! What holds across runs is the [`ConfirmedPlan`], so re-running a plan that
//! published two of three destinations publishes only the third — and for that
//! to be true across a restart or a second process, [`journal::PlanJournal`]
//! writes the plan record to disk at confirm time and [`journal::JournalSink`]
//! writes every settle and every attempt lease through the atomic,
//! lock-guarded document primitive in `xpst-state`. The journal's numbers are
//! the report's numbers: both are computed from the same records.
//!
//! What holds across processes is the attempt lease: an attempt is owned by
//! exactly one caller, an attempt whose result is unknown keeps its lease until
//! that same owner reconciles it, and a lease whose owner was killed lapses
//! after [`intent::DEFAULT_LEASE_TTL_MS`] so a destination is never blocked
//! forever.
//!
//! Everything external is a seam — [`LimitsGate`], [`DestinationPublisher`],
//! [`StateSink`] — so the whole pipeline is exercised in tests with no socket,
//! no clock and no credentials ([`fake`] ships the doubles).
//!
//! Scope: this crate does not speak HTTP, own a CLI, hold credentials, or render
//! anything. A provider crate adapts its platform to [`DestinationPublisher`];
//! the caller supplies the account, the clock and the state sink.
//!
//! Placement: `docs/PROPOSAL-RUST-REBUILD.md` §2 sketches orchestration under
//! `xpst-core`, which is deliberately dependency-free and pure (no runtime, no
//! I/O). The pipeline needs an async boundary, the rate-limit ledger and a
//! durable record, so it lives in its own crate that depends on `xpst-core` for
//! the route plan, `xpst-limits` for the gate and `xpst-state` for the journal.

#![forbid(unsafe_code)]

pub mod fake;
pub mod gate;
pub mod intent;
pub mod journal;
pub mod outcome;
pub mod pipeline;
pub mod provider;

pub use gate::{EnvelopeGate, GateDecision, GateRequest, LimitsGate};
pub use intent::{
    confirm, normalize_platform, ClaimError, ConfirmError, ConfirmedPlan, DeliveryKey,
    DestinationIntent, DestinationRecord, Lease, MediaRef, PlanId, PublicationContent,
    DEFAULT_LEASE_TTL_MS,
};
pub use journal::{
    decode_plan, encode_plan, JournalError, JournalSink, PlanJournal, PlanTimestamps,
    LEASE_OUTCOME_UNRECORDED_CODE, PLANS_DIR_NAME, PLAN_RECORD_VERSION,
};
pub use outcome::{
    DeferSource, DeferredOutcome, DestinationOutcome, DuplicateOutcome, FailedOutcome,
    FailureClass, OutcomeCounts, PlanState, PublishEvidence, PublishedOutcome, RefusalSource,
    RefusedOutcome, UnconfirmedOutcome,
};
pub use pipeline::{
    DestinationReport, Pipeline, PlanReport, PublishContext, PublishError, RetryBudget, StateSink,
    StateWriteError, DEFAULT_RATE_LIMIT_WAIT_MS,
};
pub use provider::{DestinationPublisher, ProviderOutcome, PublisherSet, ReconcileOutcome};
