//! The provider boundary: what a destination reports, and what the pipeline
//! will accept as an answer.
//!
//! A provider is a platform adapter. It knows how to put one intent on one
//! platform and it knows nothing about pacing — the pipeline has already made
//! the limits decision before the call, and this trait has no way to check a
//! limit again. That asymmetry is deliberate: pacing lives in exactly one place.
//!
//! Two kinds of answer matter more than the rest:
//!
//! * [`ProviderOutcome::Duplicate`] — the platform already has this content.
//!   Reporting it as `Published` would be a lie that inflates the post count and
//!   hides a dedupe no-op, so it is its own variant.
//! * [`ProviderOutcome::Unknown`] — the call may have landed. The pipeline marks
//!   the destination `Unconfirmed` and keeps the attempt lease; the next run
//!   reconciles before it retries. Retrying this blind is how a retry
//!   double-posts.
//!
//! [`DestinationPublisher::reconcile`] is the read-back half. Its default is
//! `Unknown`, which is the honest answer for an adapter with no read-only lookup
//! and the safe one: an outcome nobody can verify stays blocked rather than
//! being retried.

use std::collections::BTreeMap;

use async_trait::async_trait;

use crate::intent::{normalize_platform, DeliveryKey, DestinationIntent};
use crate::outcome::FailureClass;

/// What a provider reports about one attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProviderOutcome {
    /// The platform acknowledged a new post.
    Published {
        post_id: String,
        url: Option<String>,
    },
    /// The platform already holds this content; nothing was posted.
    Duplicate { existing_post_id: Option<String> },
    /// The platform said "not now" (a window, a maintenance pause).
    Deferred {
        resume_after_ms: i64,
        detail: String,
    },
    /// The provider refused the request locally, without publishing anything.
    Refused { code: String, detail: String },
    /// The attempt failed.
    Failed {
        class: FailureClass,
        code: String,
        detail: String,
    },
    /// The platform answered with a rate limit. Not a failure; the account has
    /// to wait, and the gate is told.
    RateLimited {
        retry_after_ms: Option<i64>,
        detail: String,
    },
    /// The call's result cannot be determined locally.
    Unknown { detail: String },
}

impl ProviderOutcome {
    /// A retryable failure.
    #[must_use]
    pub fn retryable(code: impl Into<String>, detail: impl Into<String>) -> Self {
        Self::Failed {
            class: FailureClass::Retryable,
            code: code.into(),
            detail: detail.into(),
        }
    }

    /// A failure retrying cannot fix.
    #[must_use]
    pub fn permanent(code: impl Into<String>, detail: impl Into<String>) -> Self {
        Self::Failed {
            class: FailureClass::Permanent,
            code: code.into(),
            detail: detail.into(),
        }
    }
}

/// What a read-back found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReconcileOutcome {
    /// The post is there: the ambiguous attempt did land.
    Found {
        post_id: String,
        url: Option<String>,
    },
    /// Nothing is there: the ambiguous attempt did not land, so a retry is safe.
    Absent,
    /// The read-back could not answer. The destination stays unconfirmed and is
    /// not retried.
    Unknown { detail: String },
}

impl ReconcileOutcome {
    /// A read-back that cannot answer.
    #[must_use]
    pub fn unknown(detail: impl Into<String>) -> Self {
        Self::Unknown {
            detail: detail.into(),
        }
    }
}

/// One publishing destination.
#[async_trait]
pub trait DestinationPublisher: Send + Sync {
    /// The platform id this adapter publishes to (`"x"`, `"bluesky"`, …).
    fn platform(&self) -> &str;

    /// Publish one intent.
    ///
    /// Called only after the limits gate said ALLOW.
    async fn publish(&self, intent: &DestinationIntent) -> ProviderOutcome;

    /// Read back whether the attempt for `key` landed.
    async fn reconcile(&self, key: &DeliveryKey) -> ReconcileOutcome {
        let _ = key;
        ReconcileOutcome::unknown(format!(
            "{} cannot read back a publish, so an unknown outcome stays blocked",
            self.platform()
        ))
    }
}

/// The publishers available to one pipeline run, keyed by platform id.
#[derive(Default)]
pub struct PublisherSet<'a> {
    by_platform: BTreeMap<String, &'a dyn DestinationPublisher>,
}

impl<'a> PublisherSet<'a> {
    #[must_use]
    pub fn new() -> Self {
        Self {
            by_platform: BTreeMap::new(),
        }
    }

    /// Add a publisher, replacing any previous one for the same platform.
    pub fn insert(&mut self, publisher: &'a dyn DestinationPublisher) {
        self.by_platform
            .insert(normalize_platform(publisher.platform()), publisher);
    }

    /// Add a publisher and return the set, for a builder chain.
    #[must_use]
    pub fn with(mut self, publisher: &'a dyn DestinationPublisher) -> Self {
        self.insert(publisher);
        self
    }

    #[must_use]
    pub fn get(&self, platform: &str) -> Option<&'a dyn DestinationPublisher> {
        self.by_platform.get(&normalize_platform(platform)).copied()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.by_platform.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.by_platform.is_empty()
    }

    /// The platforms this set can publish to, sorted.
    #[must_use]
    pub fn platforms(&self) -> Vec<&str> {
        self.by_platform.keys().map(String::as_str).collect()
    }
}
