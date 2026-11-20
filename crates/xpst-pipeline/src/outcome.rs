//! What happened at one destination, stated once and never folded together.
//!
//! The vocabulary is the contract. Each variant is a different fact about a
//! destination, and a surface that has to choose what to do next branches on it:
//!
//! | Outcome | Fact | What a scheduler does |
//! |---|---|---|
//! | [`DestinationOutcome::Published`] | the platform confirmed a post | nothing |
//! | [`DestinationOutcome::SkippedDuplicate`] | this exact content is already live there | nothing |
//! | [`DestinationOutcome::Deferred`] | waiting for a window; **not** a failure | retry after `resume_after_ms` |
//! | [`DestinationOutcome::Refused`] | nothing was attempted, and why | act on the code; a limits refusal lifts by itself |
//! | [`DestinationOutcome::Failed`] | an attempt failed | retry only when `Retryable` |
//! | [`DestinationOutcome::Unconfirmed`] | an attempt may have landed | reconcile, never retry blind |
//!
//! Two things this module deliberately refuses to do:
//!
//! * **It never counts a dedup no-op as a post.** `Published` means a platform
//!   returned a new post id. `SkippedDuplicate` means it did not, and saying
//!   "posted" there is the defect that made a re-run look like a new publish.
//! * **It never erases a destination.** [`OutcomeCounts`] has a bucket per
//!   variant plus one for "not attempted yet", and the buckets sum to the number
//!   of destinations. A destination can therefore never disappear from a report.

use std::fmt;

/// Whether an attempt that failed may be attempted again.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureClass {
    /// The attempt failed and nothing landed. Retrying is allowed.
    Retryable,
    /// The attempt failed and retrying cannot help (the platform rejected it).
    Permanent,
}

impl FailureClass {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Retryable => "retryable",
            Self::Permanent => "permanent",
        }
    }
}

impl fmt::Display for FailureClass {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// How a publication is known to have happened.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublishEvidence {
    /// The publish call itself returned a post id.
    Provider,
    /// A read-back found the post after an ambiguous attempt.
    Readback,
}

impl PublishEvidence {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Provider => "provider",
            Self::Readback => "readback",
        }
    }
}

/// Who decided a destination would not be published to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefusalSource {
    /// The route plan never accepted this destination (format, account state).
    Plan,
    /// The limits gate said REFUSE. This one lifts by itself.
    Limits,
    /// The provider refused without publishing (its own capability contract).
    Provider,
}

impl RefusalSource {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Plan => "plan",
            Self::Limits => "limits",
            Self::Provider => "provider",
        }
    }
}

/// Why a destination is waiting.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeferSource {
    /// The limits gate's pacing window (minimum interval, anti-bot hours).
    Pacing,
    /// The destination itself said "later".
    Provider,
    /// The destination answered with a rate limit; the gate has been told.
    RateLimit,
}

impl DeferSource {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Pacing => "pacing",
            Self::Provider => "provider",
            Self::RateLimit => "rate_limit",
        }
    }
}

/// A publish the platform acknowledged with a post id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishedOutcome {
    pub post_id: String,
    pub url: Option<String>,
    pub evidence: PublishEvidence,
}

/// The content is already live at this destination; nothing was posted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DuplicateOutcome {
    /// The id of the post that already exists, when the destination named one.
    pub existing_post_id: Option<String>,
}

/// Waiting, retryable after `resume_after_ms`. Not a failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeferredOutcome {
    pub resume_after_ms: i64,
    pub source: DeferSource,
    pub detail: String,
}

/// Nothing was attempted. `code` is stable and is what a caller branches on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RefusedOutcome {
    pub code: String,
    pub detail: String,
    pub source: RefusalSource,
    /// Set when the refusal is a timeout that will lift (a cooldown, a cap).
    pub retry_after_ms: Option<i64>,
}

/// An attempt that failed. `attempts` is how many provider calls were spent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailedOutcome {
    pub class: FailureClass,
    pub code: String,
    pub detail: String,
    pub attempts: u32,
}

/// An attempt whose result is unknown: it may have landed.
///
/// Retrying this without a read-back is how a retry double-posts, so the
/// pipeline keeps the attempt lease and refuses to retry until the destination
/// has been reconciled.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnconfirmedOutcome {
    pub code: String,
    pub detail: String,
    /// The attempt number that is unresolved (1-based, across runs).
    pub attempt: u32,
}

/// What happened at one destination.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DestinationOutcome {
    Published(PublishedOutcome),
    SkippedDuplicate(DuplicateOutcome),
    Deferred(DeferredOutcome),
    Refused(RefusedOutcome),
    Failed(FailedOutcome),
    Unconfirmed(UnconfirmedOutcome),
}

impl DestinationOutcome {
    /// The stable word this outcome is reported and counted as.
    #[must_use]
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::Published(_) => "published",
            Self::SkippedDuplicate(_) => "skipped_duplicate",
            Self::Deferred(_) => "deferred",
            Self::Refused(_) => "refused",
            Self::Failed(failed) => match failed.class {
                FailureClass::Retryable => "failed_retryable",
                FailureClass::Permanent => "failed_permanent",
            },
            Self::Unconfirmed(_) => "unconfirmed",
        }
    }

    /// Whether this destination is finished for good.
    ///
    /// A `Refused` by the limits gate is *not* terminal: a cooldown expires and
    /// a daily cap rolls over, so a later run asks the gate again. A refusal by
    /// the plan or the provider is terminal — neither can change without the
    /// content or the account changing.
    #[must_use]
    pub const fn is_terminal(&self) -> bool {
        match self {
            Self::Published(_) | Self::SkippedDuplicate(_) => true,
            Self::Deferred(_) => false,
            Self::Refused(refused) => !matches!(refused.source, RefusalSource::Limits),
            Self::Failed(failed) => matches!(failed.class, FailureClass::Permanent),
            Self::Unconfirmed(_) => false,
        }
    }

    /// Whether a run at `now_ms` has work to do for this destination.
    #[must_use]
    pub const fn is_actionable(&self, now_ms: i64) -> bool {
        match self {
            Self::Published(_) | Self::SkippedDuplicate(_) => false,
            Self::Deferred(deferred) => now_ms >= deferred.resume_after_ms,
            Self::Refused(refused) => matches!(refused.source, RefusalSource::Limits),
            Self::Failed(failed) => matches!(failed.class, FailureClass::Retryable),
            Self::Unconfirmed(_) => true,
        }
    }
}

impl fmt::Display for DestinationOutcome {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One bucket per outcome, plus the destinations no run has reached yet.
///
/// The buckets sum to [`OutcomeCounts::total`]; a report can therefore state
/// "every destination is accounted for" as an equality rather than a promise.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct OutcomeCounts {
    pub published: usize,
    pub skipped_duplicate: usize,
    pub deferred: usize,
    pub refused: usize,
    pub failed_retryable: usize,
    pub failed_permanent: usize,
    pub unconfirmed: usize,
    /// Not an outcome: destinations with no attempt yet.
    pub pending: usize,
}

impl OutcomeCounts {
    /// Count one destination's standing outcome (`None` = never attempted).
    pub fn add(&mut self, outcome: Option<&DestinationOutcome>) {
        match outcome {
            None => self.pending += 1,
            Some(DestinationOutcome::Published(_)) => self.published += 1,
            Some(DestinationOutcome::SkippedDuplicate(_)) => self.skipped_duplicate += 1,
            Some(DestinationOutcome::Deferred(_)) => self.deferred += 1,
            Some(DestinationOutcome::Refused(_)) => self.refused += 1,
            Some(DestinationOutcome::Failed(failed)) => match failed.class {
                FailureClass::Retryable => self.failed_retryable += 1,
                FailureClass::Permanent => self.failed_permanent += 1,
            },
            Some(DestinationOutcome::Unconfirmed(_)) => self.unconfirmed += 1,
        }
    }

    /// Destinations accounted for.
    #[must_use]
    pub const fn total(&self) -> usize {
        self.published
            + self.skipped_duplicate
            + self.deferred
            + self.refused
            + self.failed_retryable
            + self.failed_permanent
            + self.unconfirmed
            + self.pending
    }

    /// Attempts known to have failed, retryable or not.
    #[must_use]
    pub const fn failures(&self) -> usize {
        self.failed_retryable + self.failed_permanent
    }
}

/// One posted plan, summarised in a single word.
///
/// Precedence is fixed, so the same counts always produce the same word:
/// published/partial, then unconfirmed, failed, refused, deferred, pending,
/// and finally "every destination was already live".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlanState {
    /// At least one post went live and nothing is left unresolved.
    Posted,
    /// Something went live and something did not finish.
    Partial,
    /// Nothing went live; at least one destination may have, and must be reconciled.
    Unconfirmed,
    /// Nothing went live; at least one attempt failed.
    Failed,
    /// Nothing went live and nothing was attempted, because something said no.
    Refused,
    /// Nothing went live yet; every destination is waiting out a window.
    Deferred,
    /// Nothing is settled yet.
    Pending,
    /// Every destination was already live — a replay, not a failure.
    SkippedDuplicate,
}

impl PlanState {
    #[must_use]
    pub fn from_counts(counts: &OutcomeCounts) -> Self {
        if counts.total() == 0 || counts.pending == counts.total() {
            return Self::Pending;
        }
        let failures = counts.failures();
        if counts.published > 0 {
            let clean = counts.pending == 0
                && failures == 0
                && counts.refused == 0
                && counts.deferred == 0
                && counts.unconfirmed == 0;
            return if clean { Self::Posted } else { Self::Partial };
        }
        if counts.unconfirmed > 0 {
            return Self::Unconfirmed;
        }
        if failures > 0 {
            return Self::Failed;
        }
        if counts.refused > 0 {
            return Self::Refused;
        }
        if counts.deferred > 0 {
            return Self::Deferred;
        }
        if counts.pending > 0 {
            return Self::Pending;
        }
        Self::SkippedDuplicate
    }

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Posted => "posted",
            Self::Partial => "partial",
            Self::Unconfirmed => "unconfirmed",
            Self::Failed => "failed",
            Self::Refused => "refused",
            Self::Deferred => "deferred",
            Self::Pending => "pending",
            Self::SkippedDuplicate => "skipped_duplicate",
        }
    }

    /// Whether this aggregate should be surfaced as a failure.
    ///
    /// A deferral is not a failure and neither is "already live": both are
    /// intact plans, and treating either as an error is what made a queued or
    /// deduplicated post look broken.
    #[must_use]
    pub const fn is_failure(self) -> bool {
        matches!(self, Self::Failed | Self::Unconfirmed)
    }
}

impl fmt::Display for PlanState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn published() -> DestinationOutcome {
        DestinationOutcome::Published(PublishedOutcome {
            post_id: "post-1".to_owned(),
            url: None,
            evidence: PublishEvidence::Provider,
        })
    }

    fn duplicate() -> DestinationOutcome {
        DestinationOutcome::SkippedDuplicate(DuplicateOutcome {
            existing_post_id: None,
        })
    }

    fn deferred() -> DestinationOutcome {
        DestinationOutcome::Deferred(DeferredOutcome {
            resume_after_ms: 10,
            source: DeferSource::Pacing,
            detail: String::new(),
        })
    }

    fn refused(source: RefusalSource) -> DestinationOutcome {
        DestinationOutcome::Refused(RefusedOutcome {
            code: "CODE".to_owned(),
            detail: String::new(),
            source,
            retry_after_ms: None,
        })
    }

    fn failed(class: FailureClass) -> DestinationOutcome {
        DestinationOutcome::Failed(FailedOutcome {
            class,
            code: "CODE".to_owned(),
            detail: String::new(),
            attempts: 1,
        })
    }

    fn unconfirmed() -> DestinationOutcome {
        DestinationOutcome::Unconfirmed(UnconfirmedOutcome {
            code: "CODE".to_owned(),
            detail: String::new(),
            attempt: 1,
        })
    }

    fn counts(outcomes: &[DestinationOutcome]) -> OutcomeCounts {
        let mut counts = OutcomeCounts::default();
        for outcome in outcomes {
            counts.add(Some(outcome));
        }
        counts
    }

    #[test]
    fn every_outcome_has_its_own_bucket_and_none_is_folded() {
        let counts = counts(&[
            published(),
            duplicate(),
            deferred(),
            refused(RefusalSource::Limits),
            failed(FailureClass::Retryable),
            failed(FailureClass::Permanent),
            unconfirmed(),
        ]);
        assert_eq!(counts.published, 1);
        assert_eq!(counts.skipped_duplicate, 1);
        assert_eq!(counts.deferred, 1);
        assert_eq!(counts.refused, 1);
        assert_eq!(counts.failed_retryable, 1);
        assert_eq!(counts.failed_permanent, 1);
        assert_eq!(counts.unconfirmed, 1);
        assert_eq!(counts.pending, 0);
        assert_eq!(counts.total(), 7, "a bucket per destination, always");
        assert_eq!(counts.failures(), 2);

        let mut with_pending = counts;
        with_pending.add(None);
        assert_eq!(with_pending.pending, 1);
        assert_eq!(with_pending.total(), 8);
    }

    #[test]
    fn aggregate_precedence_is_fixed() {
        assert_eq!(
            PlanState::from_counts(&OutcomeCounts::default()),
            PlanState::Pending
        );
        assert_eq!(
            PlanState::from_counts(&counts(&[published(), duplicate()])),
            PlanState::Posted
        );
        assert_eq!(
            PlanState::from_counts(&counts(&[published(), deferred()])),
            PlanState::Partial
        );
        assert_eq!(
            PlanState::from_counts(&counts(&[duplicate(), duplicate()])),
            PlanState::SkippedDuplicate
        );
        assert_eq!(
            PlanState::from_counts(&counts(&[deferred()])),
            PlanState::Deferred
        );
        assert_eq!(
            PlanState::from_counts(&counts(&[refused(RefusalSource::Plan)])),
            PlanState::Refused
        );
        assert_eq!(
            PlanState::from_counts(&counts(&[failed(FailureClass::Retryable)])),
            PlanState::Failed
        );
        // An unconfirmed destination outranks a plain failure: it needs an
        // action before any retry is allowed.
        assert_eq!(
            PlanState::from_counts(&counts(&[failed(FailureClass::Retryable), unconfirmed()])),
            PlanState::Unconfirmed
        );
    }

    #[test]
    fn only_a_limits_refusal_is_re_askable() {
        assert!(!refused(RefusalSource::Limits).is_terminal());
        assert!(refused(RefusalSource::Plan).is_terminal());
        assert!(refused(RefusalSource::Provider).is_terminal());
        assert!(!deferred().is_terminal());
        assert!(deferred().is_actionable(10));
        assert!(!deferred().is_actionable(9));
        assert!(!failed(FailureClass::Retryable).is_terminal());
        assert!(failed(FailureClass::Permanent).is_terminal());
        assert!(!unconfirmed().is_terminal());
        assert!(!duplicate().is_actionable(i64::MAX));
        assert!(!published().is_actionable(i64::MAX));
    }
}
