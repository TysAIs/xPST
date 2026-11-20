//! What the smoke intends to do for one platform, decided without touching
//! the network.
//!
//! Building a plan is the only place credentials are read. A plan is either
//! already-decided (nothing to send: no credential, wrong credential shape) or
//! a single [`Request`] plus the context needed to classify its answer. There
//! is no third state and no retry loop, so "one attempt per platform" is a
//! property of the type rather than a rule to remember.

use crate::classify::{Attempt, Verdict};
use crate::platform::{Platform, Request};

/// The intended action for one platform.
#[derive(Debug)]
pub enum Action {
    /// No request will be made; the verdict is already known locally.
    Decided(Verdict),
    /// Send this request exactly once.
    Call {
        /// The read-only identity request.
        request: Request,
        /// Whether a refresh path exists (turns a 401 into `expired`).
        refreshable: bool,
        /// Non-secret local context for the report (e.g. a token expiry that
        /// has already passed). Never a credential.
        note: Option<String>,
    },
}

/// One platform's plan.
#[derive(Debug)]
pub struct Plan {
    /// Which platform.
    pub platform: Platform,
    /// What to do about it.
    pub action: Action,
}

impl Plan {
    /// A plan that performs no request.
    #[must_use]
    pub fn decided(platform: Platform, verdict: Verdict) -> Self {
        Self {
            platform,
            action: Action::Decided(verdict),
        }
    }

    /// A plan that sends one request.
    #[must_use]
    pub fn call(
        platform: Platform,
        request: Request,
        refreshable: bool,
        note: Option<String>,
    ) -> Self {
        Self {
            platform,
            action: Action::Call {
                request,
                refreshable,
                note,
            },
        }
    }

    /// The attempt a decided plan represents, for the classifier.
    #[must_use]
    pub fn attempt(&self) -> Option<Attempt> {
        match &self.action {
            Action::Decided(Verdict::NoCredential { detail }) => Some(Attempt::NoCredential {
                detail: detail.clone(),
            }),
            Action::Decided(Verdict::Unsupported { detail }) => Some(Attempt::Unsupported {
                detail: detail.clone(),
            }),
            _ => None,
        }
    }
}
