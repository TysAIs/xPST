//! The limits decision, as the pipeline asks for it.
//!
//! The pipeline calls exactly one gate method before it calls a provider, and it
//! obeys the answer:
//!
//! * **ALLOW** — attempt the destination now.
//! * **DEFER until** — record a deferral and stop. Nothing is attempted, nothing
//!   is failed, and the destination stays retryable from that instant.
//! * **REFUSE** — record the refusal code and stop. The provider is never
//!   called. Some refusals are timeouts (a cooldown, a daily cap) and lift by
//!   themselves; the rest need a human or different content.
//!
//! [`GateDecision`] is deliberately narrower than [`xpst_limits::Decision`]: it
//! carries only what a publish decision needs, which keeps a test fake trivial
//! and stops provider code from reaching into the rate-limit policy.
//!
//! The human sentence carries the policy rule id when the gate knows one
//! (`rule-2-jittered-minimum-interval`), so a log line explains itself without a
//! parallel machine field nobody reads.

use std::path::Path;

use xpst_limits::{
    Decision, LimitsStore, Platform, PlatformResponse, Request as LimitsRequest, StoreError,
};

/// What the gate is being asked about.
#[derive(Debug, Clone, Copy)]
pub struct GateRequest<'a> {
    pub platform: &'a str,
    /// The account the publish would be charged to.
    pub account: &'a str,
    /// The caller's clock, milliseconds since the Unix epoch.
    pub now_ms: i64,
    /// Content identity. The same-asset rule cannot be enforced without it, and
    /// the policy refuses an unidentified publish rather than guessing.
    pub asset: Option<&'a str>,
}

/// The gate's answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GateDecision {
    /// Publish now.
    Allow,
    /// Publish nothing until `until_ms`.
    Defer { until_ms: i64, detail: String },
    /// Publish nothing, and do not retry until something changes.
    Refuse {
        /// Stable code, e.g. `COOLDOWN_ACTIVE`.
        code: String,
        detail: String,
        /// How long until the refusal could lift, when it is a timeout at all.
        retry_after_ms: Option<i64>,
    },
}

impl GateDecision {
    #[must_use]
    pub fn allow() -> Self {
        Self::Allow
    }

    #[must_use]
    pub fn defer(until_ms: i64, detail: impl Into<String>) -> Self {
        Self::Defer {
            until_ms,
            detail: detail.into(),
        }
    }

    #[must_use]
    pub fn refuse(code: impl Into<String>, detail: impl Into<String>) -> Self {
        Self::Refuse {
            code: code.into(),
            detail: detail.into(),
            retry_after_ms: None,
        }
    }

    #[must_use]
    pub fn refuse_with_retry(
        code: impl Into<String>,
        detail: impl Into<String>,
        retry_after_ms: Option<i64>,
    ) -> Self {
        Self::Refuse {
            code: code.into(),
            detail: detail.into(),
            retry_after_ms,
        }
    }

    #[must_use]
    pub const fn is_allowed(&self) -> bool {
        matches!(self, Self::Allow)
    }
}

/// The policy boundary the pipeline obeys.
///
/// Implemented by [`EnvelopeGate`] over the durable rate-limit ledger, and by a
/// scripted fake in tests. Nothing else may decide whether a provider is called.
pub trait LimitsGate {
    fn check(&mut self, request: &GateRequest<'_>) -> GateDecision;

    /// The destination answered with a rate limit.
    ///
    /// Gates that own durable state record a strike and cool the account; a gate
    /// that owns none ignores this. The pipeline calls it before it records the
    /// deferral, so the next decision sees what this one learned.
    #[allow(unused_variables)]
    fn note_rate_limit(
        &mut self,
        platform: &str,
        account: &str,
        at_ms: i64,
        retry_after_ms: Option<i64>,
        detail: &str,
    ) {
    }
}

/// [`LimitsGate`] over a [`LimitsStore`] — the real policy, no network.
///
/// An ALLOW records the publish slot through [`LimitsStore::claim`], which is
/// the only correct way to gate a publish: the decision and the counter
/// increment happen inside one cross-process critical section, so two processes
/// cannot both be told there is one slot left.
///
/// An unreadable ledger fails closed — a gate that cannot tell is a gate that
/// says no.
pub struct EnvelopeGate {
    store: LimitsStore,
}

impl EnvelopeGate {
    /// Open the ledger in `dir`, creating it if absent.
    pub fn open(dir: impl AsRef<Path>) -> Result<Self, StoreError> {
        Ok(Self {
            store: LimitsStore::open(dir)?,
        })
    }

    /// Open the ledger with an explicit initial instant.
    pub fn open_at(dir: impl AsRef<Path>, now_ms: i64) -> Result<Self, StoreError> {
        Ok(Self {
            store: LimitsStore::open_at(dir, now_ms)?,
        })
    }

    /// The ledger underneath, for status reads.
    #[must_use]
    pub fn store(&self) -> &LimitsStore {
        &self.store
    }

    /// The ledger underneath, for recording a hit a test or another surface
    /// observed outside this gate.
    #[must_use]
    pub fn store_mut(&mut self) -> &mut LimitsStore {
        &mut self.store
    }
}

impl LimitsGate for EnvelopeGate {
    fn check(&mut self, request: &GateRequest<'_>) -> GateDecision {
        let Some(platform) = Platform::parse(request.platform) else {
            return GateDecision::refuse(
                "UNKNOWN_PLATFORM",
                format!(
                    "{platform} has no rate envelope, so xPST cannot pace a publish to it; \
                     refusing rather than posting unmeasured",
                    platform = request.platform
                ),
            );
        };
        let limits_request = LimitsRequest {
            platform,
            account: request.account,
            now_ms: request.now_ms,
            asset: request.asset,
        };
        match self.store.claim(&limits_request) {
            Ok(Decision::Allow { .. }) => GateDecision::Allow,
            Ok(Decision::Defer { until_ms, rule, .. }) => GateDecision::Defer {
                until_ms,
                detail: format!(
                    "{rule}: {platform} is inside its minimum interval; the next slot is at \
                     {until_ms} ms",
                    platform = request.platform
                ),
            },
            Ok(Decision::Refuse {
                code,
                rule,
                retry_after_ms,
                detail,
            }) => GateDecision::Refuse {
                code: code.as_str().to_owned(),
                detail: format!("{rule}: {detail}"),
                retry_after_ms,
            },
            Err(error) => GateDecision::Refuse {
                code: "STATE_CORRUPT_FAIL_CLOSED".to_owned(),
                detail: format!(
                    "state-integrity-fail-closed: the rate-limit ledger is unusable ({error}); \
                     refusing to publish without pacing"
                ),
                retry_after_ms: None,
            },
        }
    }

    fn note_rate_limit(
        &mut self,
        platform: &str,
        account: &str,
        at_ms: i64,
        retry_after_ms: Option<i64>,
        detail: &str,
    ) {
        let Some(parsed) = Platform::parse(platform) else {
            return;
        };
        let response = PlatformResponse::Throttled {
            retry_after_ms,
            code: None,
        };
        // A ledger write that fails is not allowed to lose the deferral the
        // caller is about to record; the next decision re-reads the same file
        // and fails closed if it is still broken.
        let _ = self.store.record_hit(parsed, account, at_ms, response);
        let _ = detail;
    }
}
