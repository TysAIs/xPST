//! The publish loop: gate, provider, honest outcome, state write — in that order.
//!
//! The order is the safety property, not an implementation detail:
//!
//! 1. **Limits decision.** [`LimitsGate::check`] answers ALLOW / DEFER / REFUSE
//!    for one destination. DEFER records a deferral; REFUSE records the refusal
//!    code. Neither reaches a provider — a provider call made against a REFUSE is
//!    how an account gets limited.
//! 2. **Provider call.** Only on ALLOW, and only under an attempt lease held by
//!    this run (see [`ConfirmedPlan::claim`]).
//! 3. **Honest outcome.** The provider's answer is mapped to exactly one
//!    [`DestinationOutcome`]. A dedupe no-op is `SkippedDuplicate`; an ambiguous
//!    answer is `Unconfirmed` and keeps the lease.
//! 4. **State write.** The settled destination is handed to the
//!    [`StateSink`] afterwards, so nothing is recorded as a fact the provider did
//!    not report.
//!
//! Retries are owned here, once, with a bounded budget: a retryable failure is
//! retried inside the same limits allowance (it is still one publish), and the
//! loop stops at [`RetryBudget::max_attempts`]. An `Unconfirmed` outcome is never
//! retried inside the loop: it is reconciled first, on a later pass, and only an
//! explicit [`ReconcileOutcome::Absent`] allows the provider to be called again.
//!
//! Re-running a plan is safe by construction. [`Pipeline::publish`] works only on
//! destinations that are still actionable, so a plan whose first run published
//! two of three destinations publishes only the third when it is run again.
//! "By construction" only holds across a restart when the record is durable, so
//! the confirmed plan is written to [`crate::journal::PlanJournal`] at confirm
//! time and every settle and claim goes through [`StateSink`] — the shipped
//! implementation is [`crate::journal::JournalSink`], over the same atomic,
//! lock-guarded document primitive the state file uses.

use std::fmt;

use xpst_core::ingest::DestinationCapability;
use xpst_core::plan::{plan as plan_routes, RoutePlan, RouteRequest};

use crate::gate::{GateDecision, GateRequest, LimitsGate};
use crate::intent::{
    confirm, readback_published, ClaimError, ConfirmError, ConfirmedPlan, DestinationRecord,
    PublicationContent,
};
use crate::outcome::{
    DeferSource, DeferredOutcome, DestinationOutcome, DuplicateOutcome, FailedOutcome,
    OutcomeCounts, PlanState, PublishEvidence, PublishedOutcome, RefusalSource, RefusedOutcome,
    UnconfirmedOutcome,
};
use crate::provider::{ProviderOutcome, PublisherSet, ReconcileOutcome};

/// How long to wait when a platform rate-limits without saying for how long.
pub const DEFAULT_RATE_LIMIT_WAIT_MS: i64 = 15 * 60 * 1000;

/// How many provider calls one destination may cost in one run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetryBudget {
    max_attempts_per_destination: u32,
}

impl RetryBudget {
    /// The default: three calls, enough for a transient blip and no more.
    pub const DEFAULT_MAX_ATTEMPTS: u32 = 3;

    #[must_use]
    pub fn new(max_attempts_per_destination: u32) -> Self {
        Self {
            max_attempts_per_destination: max_attempts_per_destination.max(1),
        }
    }

    #[must_use]
    pub const fn max_attempts(self) -> u32 {
        self.max_attempts_per_destination
    }
}

impl Default for RetryBudget {
    fn default() -> Self {
        Self::new(Self::DEFAULT_MAX_ATTEMPTS)
    }
}

/// Somewhere a settled destination is persisted.
///
/// The pipeline calls [`StateSink::record`] exactly once per destination it
/// settles, after the outcome is recorded, and [`StateSink::claim`] before each
/// provider call. A write that fails is reported on the destination entry and
/// does not change the outcome — the provider already did what it did.
///
/// Both methods receive the destination's **standing record** (intent, outcome,
/// attempt count and lease), not just the report entry: a durable sink has to
/// write the whole record or it cannot answer "what has already happened here"
/// after a restart. [`crate::journal::JournalSink`] is the implementation this
/// engine ships.
pub trait StateSink {
    /// Persist one destination's standing record after it settles.
    fn record(
        &mut self,
        plan_id: &str,
        now_ms: i64,
        record: &DestinationRecord,
    ) -> Result<(), StateWriteError>;

    /// Persist the attempt lease **before** the provider is called.
    ///
    /// The default does nothing, which is honest for an in-memory sink: it has
    /// no crash to survive. A durable sink must implement it, because a claim
    /// that only lives in memory is a claim another process cannot see.
    fn claim(
        &mut self,
        _plan_id: &str,
        _now_ms: i64,
        _record: &DestinationRecord,
    ) -> Result<(), StateWriteError> {
        Ok(())
    }
}

/// A failed state write, with the reason a human needs.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct StateWriteError {
    pub message: String,
}

impl StateWriteError {
    #[must_use]
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

/// Everything one publish pass needs that is not the plan itself.
pub struct PublishContext<'a> {
    /// The caller's clock, milliseconds since the Unix epoch.
    pub now_ms: i64,
    /// The account publishes are charged to.
    pub account: &'a str,
    /// Who owns the attempts this pass makes. One owner per destination per
    /// attempt: a lease held by a different owner is refused, not stolen.
    pub owner: &'a str,
    /// Where settled destinations are persisted.
    pub state: Option<&'a mut dyn StateSink>,
}

/// What happened at one destination in this pass, and what stands for it now.
#[derive(Debug, Clone)]
pub struct DestinationReport {
    pub platform: String,
    /// The standing outcome for this destination, `None` if it has never been
    /// attempted and this pass did not attempt it either.
    pub outcome: Option<DestinationOutcome>,
    /// Whether this pass called the provider at least once.
    pub attempted_this_run: bool,
    /// Provider calls this pass spent.
    pub attempts_this_run: u32,
    /// Set when the state write after this destination failed.
    pub state_write_error: Option<String>,
}

impl DestinationReport {
    fn new(platform: impl Into<String>) -> Self {
        Self {
            platform: platform.into(),
            outcome: None,
            attempted_this_run: false,
            attempts_this_run: 0,
            state_write_error: None,
        }
    }

    /// The outcome word, or `"pending"` when nothing has happened yet.
    #[must_use]
    pub fn outcome_word(&self) -> &'static str {
        self.outcome
            .as_ref()
            .map_or("pending", DestinationOutcome::as_str)
    }
}

/// The result of one publish pass over one plan.
#[derive(Debug, Clone)]
pub struct PlanReport {
    plan_id: String,
    entries: Vec<DestinationReport>,
}

impl PlanReport {
    #[must_use]
    pub fn plan_id(&self) -> &str {
        &self.plan_id
    }

    #[must_use]
    pub fn entries(&self) -> &[DestinationReport] {
        &self.entries
    }

    #[must_use]
    pub fn entry(&self, platform: &str) -> Option<&DestinationReport> {
        let key = crate::intent::normalize_platform(platform);
        self.entries.iter().find(|entry| entry.platform == key)
    }

    /// One bucket per outcome, over the whole plan.
    ///
    /// The buckets sum to the number of destinations. Nothing is folded: a
    /// dedupe no-op is counted as `skipped_duplicate` and never as `published`.
    #[must_use]
    pub fn counts(&self) -> OutcomeCounts {
        let mut counts = OutcomeCounts::default();
        for entry in &self.entries {
            counts.add(entry.outcome.as_ref());
        }
        counts
    }

    #[must_use]
    pub fn state(&self) -> PlanState {
        PlanState::from_counts(&self.counts())
    }

    /// Destinations whose post is live now, because this plan put it there.
    #[must_use]
    pub fn published(&self) -> Vec<&str> {
        self.with_word("published")
    }

    /// Destinations whose content was already live.
    #[must_use]
    pub fn skipped_duplicates(&self) -> Vec<&str> {
        self.with_word("skipped_duplicate")
    }

    /// Destinations still waiting out a window.
    #[must_use]
    pub fn deferred(&self) -> Vec<&str> {
        self.with_word("deferred")
    }

    /// Destinations that may have posted and must be reconciled.
    #[must_use]
    pub fn unconfirmed(&self) -> Vec<&str> {
        self.with_word("unconfirmed")
    }

    fn with_word(&self, word: &str) -> Vec<&str> {
        self.entries
            .iter()
            .filter(|entry| entry.outcome_word() == word)
            .map(|entry| entry.platform.as_str())
            .collect()
    }
}

/// Why a pass could not run at all.
///
/// These are refused *before* the first gate or provider call, so a plan is
/// never half-executed because of a wiring or ownership mistake.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PublishError {
    #[error(
        "cannot publish to {platform}: its attempt is leased to {owner} (attempt {attempt_no})"
    )]
    DestinationLeased {
        platform: String,
        owner: String,
        attempt_no: u32,
    },
    #[error("no publisher is registered for {platform}, which this plan would publish to")]
    NoPublisher { platform: String },
    #[error(transparent)]
    Claim(#[from] ClaimError),
}

/// The plan → confirm → publish pipeline.
///
/// It owns no state of its own: the plan carries per-destination progress, the
/// gate owns pacing, and the publishers own the platform protocols.
pub struct Pipeline<'a> {
    gate: &'a mut dyn LimitsGate,
    publishers: PublisherSet<'a>,
    budget: RetryBudget,
}

impl<'a> Pipeline<'a> {
    #[must_use]
    pub fn new(gate: &'a mut dyn LimitsGate, publishers: PublisherSet<'a>) -> Self {
        Self {
            gate,
            publishers,
            budget: RetryBudget::default(),
        }
    }

    #[must_use]
    pub fn with_budget(mut self, budget: RetryBudget) -> Self {
        self.budget = budget;
        self
    }

    #[must_use]
    pub fn budget(&self) -> RetryBudget {
        self.budget
    }

    /// Step one: decide, per destination, whether this content may be published.
    #[must_use]
    pub fn plan(
        &self,
        request: &RouteRequest,
        destinations: &[DestinationCapability],
    ) -> RoutePlan {
        plan_routes(request, destinations)
    }

    /// Step two: freeze the plan into per-destination intents.
    pub fn confirm(
        &self,
        plan: &RoutePlan,
        content: &PublicationContent,
    ) -> Result<ConfirmedPlan, ConfirmError> {
        confirm(plan, content)
    }

    /// Step three: publish what the plan still has outstanding.
    pub async fn publish(
        &mut self,
        plan: &mut ConfirmedPlan,
        context: PublishContext<'_>,
    ) -> Result<PlanReport, PublishError> {
        let PublishContext {
            now_ms,
            account,
            owner,
            mut state,
        } = context;

        // Preflight both blockers before anything is called: a live foreign
        // lease means another owner is mid-attempt on this destination, and a
        // missing publisher means this plan cannot be executed at all. Refusing
        // up front is what keeps a pass from publishing half a plan and
        // reporting the rest as something it never tried.
        //
        // Only a *live* lease blocks: one whose owner was killed must lapse, or
        // the destination could never be published again.
        for record in plan.destinations() {
            if !record.needs_work(now_ms) {
                continue;
            }
            if let Some(lease) = record.active_lease(now_ms) {
                if lease.owner != owner {
                    return Err(PublishError::DestinationLeased {
                        platform: record.platform().to_owned(),
                        owner: lease.owner.clone(),
                        attempt_no: lease.attempt_no,
                    });
                }
            }
            if record.intent().is_some() && self.publishers.get(record.platform()).is_none() {
                return Err(PublishError::NoPublisher {
                    platform: record.platform().to_owned(),
                });
            }
        }

        let mut entries = Vec::with_capacity(plan.destinations().len());
        for index in 0..plan.destinations().len() {
            let platform = plan.destinations()[index].platform().to_owned();
            let mut report = DestinationReport::new(platform);

            if !plan.destinations()[index].needs_work(now_ms) {
                report.outcome = plan.destinations()[index].outcome().cloned();
                entries.push(report);
                continue;
            }

            self.run_destination(
                plan,
                index,
                Run {
                    now_ms,
                    account,
                    owner,
                },
                &mut report,
                &mut state,
            )
            .await?;
            report.outcome = plan.destinations()[index].outcome().cloned();
            write_state(&mut state, plan, index, now_ms, &mut report);
            entries.push(report);
        }

        Ok(PlanReport {
            plan_id: plan.plan_id().to_owned(),
            entries,
        })
    }

    /// One destination, in the order that keeps the account safe.
    async fn run_destination(
        &mut self,
        plan: &mut ConfirmedPlan,
        index: usize,
        run: Run<'_>,
        report: &mut DestinationReport,
        state: &mut Option<&mut dyn StateSink>,
    ) -> Result<(), PublishError> {
        let platform = plan.destinations()[index].platform().to_owned();
        let Some(intent) = plan.destinations()[index].intent().cloned() else {
            // The plan refused this destination: nothing to hand a provider.
            return Ok(());
        };
        let publisher =
            self.publishers
                .get(&platform)
                .ok_or_else(|| PublishError::NoPublisher {
                    platform: platform.clone(),
                })?;

        // ── An unconfirmed attempt is reconciled before anything else. ──
        if matches!(
            plan.destinations()[index].outcome(),
            Some(DestinationOutcome::Unconfirmed(_))
        ) {
            let attempt_no = plan.claim(&platform, run.owner, run.now_ms)?;
            write_claim(state, plan, index, run.now_ms, report);
            match publisher.reconcile(&intent.key).await {
                ReconcileOutcome::Found { post_id, url } => {
                    plan.settle(
                        index,
                        DestinationOutcome::Published(readback_published(post_id, url)),
                    );
                    plan.release(&platform, run.owner);
                    return Ok(());
                }
                ReconcileOutcome::Unknown { detail } => {
                    // The lease stays with this owner: nobody else may resolve
                    // the same ambiguous attempt.
                    plan.settle(
                        index,
                        DestinationOutcome::Unconfirmed(UnconfirmedOutcome {
                            code: "RECONCILE_UNKNOWN".to_owned(),
                            detail,
                            attempt: attempt_no,
                        }),
                    );
                    return Ok(());
                }
                ReconcileOutcome::Absent => {
                    // Proven not published, so a retry cannot double-post.
                    plan.release(&platform, run.owner);
                }
            }
        }

        // ── The limits decision, always before a provider call. ──
        let decision = self.gate.check(&GateRequest {
            platform: &platform,
            account: run.account,
            now_ms: run.now_ms,
            asset: Some(plan.content_hash()),
        });
        match decision {
            GateDecision::Refuse {
                code,
                detail,
                retry_after_ms,
            } => {
                plan.settle(
                    index,
                    DestinationOutcome::Refused(RefusedOutcome {
                        code,
                        detail,
                        source: RefusalSource::Limits,
                        retry_after_ms,
                    }),
                );
                plan.release(&platform, run.owner);
                return Ok(());
            }
            GateDecision::Defer { until_ms, detail } => {
                plan.settle(
                    index,
                    DestinationOutcome::Deferred(DeferredOutcome {
                        resume_after_ms: until_ms,
                        source: DeferSource::Pacing,
                        detail,
                    }),
                );
                plan.release(&platform, run.owner);
                return Ok(());
            }
            GateDecision::Allow => {}
        }

        // ── Provider attempts, bounded, one lease at a time. ──
        loop {
            let attempt_no = plan.claim(&platform, run.owner, run.now_ms)?;
            plan.count_attempt(index);
            write_claim(state, plan, index, run.now_ms, report);
            report.attempted_this_run = true;
            report.attempts_this_run = report.attempts_this_run.saturating_add(1);

            let outcome = publisher.publish(&intent).await;
            let attempts = plan.attempts(index);
            match outcome {
                ProviderOutcome::Published { post_id, url } => {
                    plan.settle(
                        index,
                        DestinationOutcome::Published(PublishedOutcome {
                            post_id,
                            url,
                            evidence: PublishEvidence::Provider,
                        }),
                    );
                    plan.release(&platform, run.owner);
                    return Ok(());
                }
                ProviderOutcome::Duplicate { existing_post_id } => {
                    plan.settle(
                        index,
                        DestinationOutcome::SkippedDuplicate(DuplicateOutcome { existing_post_id }),
                    );
                    plan.release(&platform, run.owner);
                    return Ok(());
                }
                ProviderOutcome::Deferred {
                    resume_after_ms,
                    detail,
                } => {
                    plan.settle(
                        index,
                        DestinationOutcome::Deferred(DeferredOutcome {
                            resume_after_ms,
                            source: DeferSource::Provider,
                            detail,
                        }),
                    );
                    plan.release(&platform, run.owner);
                    return Ok(());
                }
                ProviderOutcome::Refused { code, detail } => {
                    plan.settle(
                        index,
                        DestinationOutcome::Refused(RefusedOutcome {
                            code,
                            detail,
                            source: RefusalSource::Provider,
                            retry_after_ms: None,
                        }),
                    );
                    plan.release(&platform, run.owner);
                    return Ok(());
                }
                ProviderOutcome::RateLimited {
                    retry_after_ms,
                    detail,
                } => {
                    self.gate.note_rate_limit(
                        &platform,
                        run.account,
                        run.now_ms,
                        retry_after_ms,
                        &detail,
                    );
                    let wait = retry_after_ms.unwrap_or(DEFAULT_RATE_LIMIT_WAIT_MS);
                    plan.settle(
                        index,
                        DestinationOutcome::Deferred(DeferredOutcome {
                            resume_after_ms: run.now_ms.saturating_add(wait),
                            source: DeferSource::RateLimit,
                            detail,
                        }),
                    );
                    plan.release(&platform, run.owner);
                    return Ok(());
                }
                ProviderOutcome::Unknown { detail } => {
                    // Possibly published. Keep the lease and make a retry go
                    // through reconcile first.
                    plan.settle(
                        index,
                        DestinationOutcome::Unconfirmed(UnconfirmedOutcome {
                            code: "PROVIDER_OUTCOME_UNKNOWN".to_owned(),
                            detail,
                            attempt: attempt_no,
                        }),
                    );
                    return Ok(());
                }
                ProviderOutcome::Failed {
                    class,
                    code,
                    detail,
                } => {
                    let retry = report.attempts_this_run < self.budget.max_attempts()
                        && class == crate::outcome::FailureClass::Retryable;
                    if retry {
                        // Still one publish under one limits allowance; the
                        // lease is re-taken so the attempt is owned throughout.
                        continue;
                    }
                    plan.settle(
                        index,
                        DestinationOutcome::Failed(FailedOutcome {
                            class,
                            code,
                            detail,
                            attempts,
                        }),
                    );
                    plan.release(&platform, run.owner);
                    return Ok(());
                }
            }
        }
    }
}

/// The per-pass facts every destination needs.
#[derive(Clone, Copy)]
struct Run<'a> {
    now_ms: i64,
    account: &'a str,
    owner: &'a str,
}

fn write_state(
    state: &mut Option<&mut dyn StateSink>,
    plan: &ConfirmedPlan,
    index: usize,
    now_ms: i64,
    report: &mut DestinationReport,
) {
    let Some(sink) = state.as_mut() else {
        return;
    };
    let Some(record) = plan.destinations().get(index) else {
        return;
    };
    if let Err(error) = sink.record(plan.plan_id(), now_ms, record) {
        report.state_write_error = Some(error.to_string());
    }
}

/// Persist the attempt lease before the provider is called.
///
/// This is the write that makes the lease mean something across processes: if
/// this process dies mid-attempt, the record already says who was attempting
/// what, so the next run waits for the lease to lapse instead of publishing a
/// second copy.
fn write_claim(
    state: &mut Option<&mut dyn StateSink>,
    plan: &ConfirmedPlan,
    index: usize,
    now_ms: i64,
    report: &mut DestinationReport,
) {
    let Some(sink) = state.as_mut() else {
        return;
    };
    let Some(record) = plan.destinations().get(index) else {
        return;
    };
    if let Err(error) = sink.claim(plan.plan_id(), now_ms, record) {
        report.state_write_error = Some(error.to_string());
    }
}

impl fmt::Display for DestinationReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.platform, self.outcome_word())
    }
}
