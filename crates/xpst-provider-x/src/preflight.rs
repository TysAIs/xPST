//! Preflight: price every planned post before anything is sent.
//!
//! The report answers, per post: how long the text is, whether it contains a
//! URL (and so costs the URL price), what that price is, whether the asset has
//! already gone to another account, and whether the post will be published or
//! refused.
//!
//! **Refusal is opt-in on cost.** With no [`CostCap`], a URL post is reported as
//! more expensive and *still planned* — the caller asked for that post, and
//! silently dropping or editing its link would ship something the user did not
//! approve. Only when a cap is supplied, and the running total would exceed it,
//! does cost become a refusal.
//!
//! Preflight performs no I/O and spends nothing.

use crate::cost::{Cost, CostCap};
use crate::duplicate::DuplicateConflict;
use crate::error::XError;
use crate::request::PublishRequest;

/// The per-destination text limit, read from the one source of truth.
///
/// `xpst_core::content::TEXT_LIMITS` is the same table the Python preflight
/// reads, so the planner and the sender cannot disagree about 280.
#[must_use]
pub fn x_text_limit() -> Option<usize> {
    xpst_core::content::text_limit("x")
}

/// Why a planned post will not be sent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    TextEmpty,
    TextTooLong {
        measured: usize,
        limit: usize,
    },
    CostCapExceeded {
        planned_micros: u64,
        cap_micros: u64,
    },
    DuplicateAcrossAccounts {
        conflict: DuplicateConflict,
    },
    AssetKeyRequired,
}

impl Refusal {
    /// Stable reason code, identical to the [`XError`] the publish boundary
    /// raises for the same condition.
    #[must_use]
    pub const fn code(&self) -> &'static str {
        match self {
            Self::TextEmpty => "X_TEXT_EMPTY",
            Self::TextTooLong { .. } => "X_TEXT_TOO_LONG",
            Self::CostCapExceeded { .. } => "X_COST_CAP_EXCEEDED",
            Self::DuplicateAcrossAccounts { .. } => crate::duplicate::CROSS_ACCOUNT_CODE,
            Self::AssetKeyRequired => "X_ASSET_KEY_REQUIRED",
        }
    }

    /// The equivalent publish-time error, for callers that log refusals in one
    /// vocabulary.
    #[must_use]
    pub fn to_error(&self) -> XError {
        match self {
            Self::TextEmpty => XError::TextEmpty,
            Self::TextTooLong { measured, limit } => XError::TextTooLong {
                measured: *measured,
                limit: *limit,
            },
            Self::CostCapExceeded {
                planned_micros,
                cap_micros,
            } => XError::CostCapExceeded {
                planned_micros: *planned_micros,
                cap_micros: *cap_micros,
            },
            Self::DuplicateAcrossAccounts { conflict } => XError::DuplicateAcrossAccounts {
                asset_key: conflict.asset_key.clone(),
                other_account: conflict.other_account.clone(),
                posted_at: conflict.posted_at,
                window_seconds: conflict.window_seconds,
            },
            Self::AssetKeyRequired => XError::AssetKeyRequired,
        }
    }
}

/// Whether one plan will be sent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlanDecision {
    Publish,
    Refused(Refusal),
}

impl PlanDecision {
    #[must_use]
    pub const fn is_publish(&self) -> bool {
        matches!(self, Self::Publish)
    }
}

/// One planned post, fully priced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostPlan {
    /// Position in the batch, so a refusal is unambiguous.
    pub index: usize,
    pub account: String,
    /// Measured length, in Unicode scalar values.
    pub text_length: usize,
    /// Whether the text contains a URL — the reason for the price.
    pub has_url: bool,
    pub cost: Cost,
    /// Running cost of the batch including this post when it can be sent.
    pub cumulative_micros: u64,
    pub decision: PlanDecision,
}

impl PostPlan {
    /// The refusal, if this post will not be sent.
    #[must_use]
    pub const fn refusal(&self) -> Option<&Refusal> {
        match &self.decision {
            PlanDecision::Refused(refusal) => Some(refusal),
            PlanDecision::Publish => None,
        }
    }
}

/// The whole batch, priced and decided.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreflightReport {
    pub plans: Vec<PostPlan>,
    /// Sum of the plans that will actually be sent.
    pub total_micros: u64,
    /// The cap that was in force, when the caller set one.
    pub cap_micros: Option<u64>,
}

impl PreflightReport {
    pub fn publishable(&self) -> impl Iterator<Item = &PostPlan> {
        self.plans.iter().filter(|plan| plan.decision.is_publish())
    }

    pub fn refused(&self) -> impl Iterator<Item = &PostPlan> {
        self.plans.iter().filter(|plan| plan.refusal().is_some())
    }

    #[must_use]
    pub fn is_fully_publishable(&self) -> bool {
        self.plans.iter().all(|plan| plan.decision.is_publish())
    }

    /// Count of posts that contain a URL.
    #[must_use]
    pub fn url_post_count(&self) -> usize {
        self.plans.iter().filter(|plan| plan.has_url).count()
    }
}

/// Build the report. Pure: no transport, no clock, no ledger mutation.
pub(crate) fn report(
    requests: &[PublishRequest],
    cap: Option<CostCap>,
    duplicate: impl Fn(&PublishRequest) -> Option<DuplicateConflict>,
) -> PreflightReport {
    let limit = x_text_limit();
    let mut plans = Vec::with_capacity(requests.len());
    let mut cumulative = 0_u64;

    for (index, request) in requests.iter().enumerate() {
        let text_length = request.text_length();
        let cost = request.cost();
        let refusal = if request.is_empty() {
            Some(Refusal::TextEmpty)
        } else if let Some(limit) = limit.filter(|limit| text_length > *limit) {
            Some(Refusal::TextTooLong {
                measured: text_length,
                limit,
            })
        } else if request.media.is_some()
            && request
                .asset_key
                .as_deref()
                .map(str::trim)
                .is_none_or(str::is_empty)
        {
            Some(Refusal::AssetKeyRequired)
        } else if let Some(conflict) = duplicate(request) {
            Some(Refusal::DuplicateAcrossAccounts { conflict })
        } else if let Some(cap) = cap {
            let planned = cumulative.saturating_add(cost.micros);
            if planned > cap.max_micros {
                Some(Refusal::CostCapExceeded {
                    planned_micros: planned,
                    cap_micros: cap.max_micros,
                })
            } else {
                None
            }
        } else {
            None
        };

        if refusal.is_none() {
            cumulative = cumulative.saturating_add(cost.micros);
        }
        plans.push(PostPlan {
            index,
            account: request.account.clone(),
            text_length,
            has_url: cost.has_url,
            cost,
            cumulative_micros: cumulative,
            decision: refusal.map_or(PlanDecision::Publish, PlanDecision::Refused),
        });
    }

    PreflightReport {
        plans,
        total_micros: cumulative,
        cap_micros: cap.map(|cap| cap.max_micros),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost::MICROS_PER_POST_WITH_URL;

    fn no_duplicates(_: &PublishRequest) -> Option<DuplicateConflict> {
        None
    }

    #[test]
    fn without_a_cap_a_url_post_is_flagged_and_still_planned() {
        let requests = ["plain update", "with a link https://example.com/x"]
            .iter()
            .map(|text| PublishRequest::text("acct", *text))
            .collect::<Vec<_>>();
        let report = report(&requests, None, no_duplicates);
        assert!(report.is_fully_publishable());
        assert!(!report.plans[0].has_url);
        assert!(report.plans[1].has_url);
        assert_eq!(report.total_micros, 15_000 + MICROS_PER_POST_WITH_URL);
        assert_eq!(report.plans[1].cost.usd(), "$0.200");
    }

    #[test]
    fn a_cap_refuses_the_post_that_would_exceed_it_and_keeps_the_text_intact() {
        let requests = vec![
            PublishRequest::text("acct", "plain"),
            PublishRequest::text("acct", "https://example.com/link"),
        ];
        let report = report(&requests, Some(CostCap::from_micros(50_000)), no_duplicates);
        assert!(report.plans[0].decision.is_publish());
        let refusal = report.plans[1]
            .refusal()
            .expect("second post must be refused");
        assert_eq!(refusal.code(), "X_COST_CAP_EXCEEDED");
        assert_eq!(report.total_micros, 15_000);
        assert_eq!(requests[1].text, "https://example.com/link");
    }
}
