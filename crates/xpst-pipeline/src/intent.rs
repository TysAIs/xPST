//! The frozen plan: what was confirmed, per destination, and what is left to do.
//!
//! [`ConfirmedPlan`] is the value a caller holds between "a human said yes" and
//! "this destination went out". It exists so that publishing is a sequence of
//! named steps over one immutable decision, not a walk over mutable
//! configuration that can change under it:
//!
//! * **It is frozen.** Every destination that the route plan accepted gets a
//!   [`DestinationIntent`] carrying the resolved format, mode, caption and
//!   media, plus a [`DeliveryKey`]. Every destination the plan refused gets a
//!   `Refused` outcome instead of an intent, so a refusal is reported rather
//!   than dropped.
//! * **It is idempotent per destination.** The record holds the standing
//!   outcome, the attempt count and the attempt lease. A second run only touches
//!   destinations that are still actionable, which is what makes a partially
//!   completed plan safe to re-run.
//! * **It is identifiable.** [`PlanId`] is derived from the content identity and
//!   the per-destination intents, so the same confirmation asked twice is
//!   recognisably the same plan.
//!
//! One rule about the lease: an attempt that came back *unconfirmed* keeps its
//! lease. Only the owner that made the ambiguous attempt may resolve it, through
//! a reconcile, so a second process cannot retry it into a double post. The
//! lease carries an expiry ([`DEFAULT_LEASE_TTL_MS`]) so a killed owner cannot
//! block a destination forever: after it lapses another run may claim the
//! destination, and it still reconciles before it retries.

use std::collections::BTreeMap;
use std::fmt;

use xpst_core::content::ContentType;
use xpst_core::envelope::VideoMode;
use xpst_core::plan::{DestinationVerdict, RoutePlan};

use crate::outcome::{
    DestinationOutcome, OutcomeCounts, PublishEvidence, PublishedOutcome, RefusalSource,
    RefusedOutcome,
};

/// How long an attempt lease lasts before another owner may claim it.
///
/// Five minutes: longer than any provider call this engine makes, short enough
/// that a killed process costs one coffee break rather than the plan.
pub const DEFAULT_LEASE_TTL_MS: i64 = 5 * 60 * 1000;

/// The identity of the bytes being published.
///
/// Callers pass the content hash the engine already computes for dedupe; it is
/// the same cross-flow identity that stops a manual re-post of identical bytes
/// from going out twice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaRef {
    pub path: String,
    pub content_hash: String,
}

impl MediaRef {
    #[must_use]
    pub fn new(path: impl Into<String>, content_hash: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            content_hash: content_hash.into(),
        }
    }
}

/// Everything a destination needs to publish, and nothing about how.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublicationContent {
    content_hash: String,
    caption: String,
    media: Vec<MediaRef>,
    captions: BTreeMap<String, String>,
}

impl PublicationContent {
    /// Content with one shared caption and no media.
    ///
    /// `content_hash` must be the content identity the engine computed; a plan
    /// without one cannot be made idempotent and is refused at confirmation.
    #[must_use]
    pub fn new(content_hash: impl Into<String>, caption: impl Into<String>) -> Self {
        Self {
            content_hash: content_hash.into(),
            caption: caption.into(),
            media: Vec::new(),
            captions: BTreeMap::new(),
        }
    }

    #[must_use]
    pub fn with_media(mut self, media: Vec<MediaRef>) -> Self {
        self.media = media;
        self
    }

    /// Override the caption for one destination (the CLI's `--caption-for`).
    #[must_use]
    pub fn with_caption_for(
        mut self,
        platform: impl Into<String>,
        caption: impl Into<String>,
    ) -> Self {
        self.captions
            .insert(normalize_platform(&platform.into()), caption.into());
        self
    }

    #[must_use]
    pub fn content_hash(&self) -> &str {
        &self.content_hash
    }

    #[must_use]
    pub fn caption(&self) -> &str {
        &self.caption
    }

    #[must_use]
    pub fn media(&self) -> &[MediaRef] {
        &self.media
    }

    /// The caption this destination is actually sent.
    #[must_use]
    pub fn caption_for(&self, platform: &str) -> &str {
        self.captions
            .get(&normalize_platform(platform))
            .map_or(self.caption.as_str(), String::as_str)
    }
}

/// Per-destination idempotency identity: the platform plus the content hash.
///
/// It is handed to providers (so a provider that supports idempotent creates can
/// use it) and to the reconcile path (so a read-back looks for the same thing
/// that was attempted).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct DeliveryKey(String);

impl DeliveryKey {
    #[must_use]
    pub fn new(platform: &str, content_hash: &str) -> Self {
        Self(format!(
            "{}:{}",
            normalize_platform(platform),
            content_hash.trim()
        ))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for DeliveryKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// The identity of a confirmed plan.
///
/// Derived from the content hash and every destination's intent, so the same
/// content confirmed for the same destinations is the same plan. It is a
/// non-cryptographic FNV-1a tag used for logging and replay detection — not a
/// security boundary, and not a substitute for the content hash.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct PlanId(String);

impl PlanId {
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Adopt an id that came from a persisted record.
    #[must_use]
    pub(crate) fn from_stored(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    fn derive(records: &[DestinationRecord], content_hash: &str) -> Self {
        const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
        let mut hash = fnv1a(content_hash.as_bytes(), OFFSET);
        for record in records {
            hash = fnv1a(record.platform.as_bytes(), hash);
            if let Some(intent) = record.intent.as_ref() {
                hash = fnv1a(intent.content_type.as_str().as_bytes(), hash);
                hash = fnv1a(intent.caption.as_bytes(), hash);
            }
        }
        Self(format!("plan-{hash:016x}"))
    }
}

impl fmt::Display for PlanId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

fn fnv1a(bytes: &[u8], seed: u64) -> u64 {
    let mut hash = seed;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// The frozen instruction for one destination.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DestinationIntent {
    pub platform: String,
    pub content_type: ContentType,
    pub mode: Option<VideoMode>,
    /// The caption this destination is sent, already resolved.
    pub caption: String,
    pub media: Vec<MediaRef>,
    pub key: DeliveryKey,
}

/// Who holds the attempt in flight, and until when.
///
/// The lease is what stops two runs — in this process or another — from
/// publishing one destination twice. It carries an absolute expiry because a
/// lease outlives its owner whenever a process is killed: a claim that never
/// expires would block that destination forever, which trades a double post for
/// a post that can never be made.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Lease {
    pub owner: String,
    /// 1-based attempt number this lease belongs to.
    pub attempt_no: u32,
    /// Wall-clock milliseconds since the Unix epoch, from the caller's clock.
    pub expires_at_ms: i64,
}

impl Lease {
    /// Whether this lease has run out at `now_ms`.
    ///
    /// An expired lease is not "released": the attempt may still have landed,
    /// so a destination whose outcome is `Unconfirmed` is reconciled before it
    /// is retried, no matter who claims it next.
    #[must_use]
    pub const fn is_expired(&self, now_ms: i64) -> bool {
        now_ms >= self.expires_at_ms
    }
}

/// One destination's frozen intent plus what has happened to it.
#[derive(Debug, Clone)]
pub struct DestinationRecord {
    platform: String,
    intent: Option<DestinationIntent>,
    outcome: Option<DestinationOutcome>,
    attempts: u32,
    lease: Option<Lease>,
}

impl DestinationRecord {
    #[must_use]
    pub fn platform(&self) -> &str {
        &self.platform
    }

    /// The frozen intent, `None` for a destination the plan refused.
    #[must_use]
    pub fn intent(&self) -> Option<&DestinationIntent> {
        self.intent.as_ref()
    }

    /// The standing outcome, `None` until something has happened.
    #[must_use]
    pub fn outcome(&self) -> Option<&DestinationOutcome> {
        self.outcome.as_ref()
    }

    /// Provider calls spent on this destination, across runs.
    #[must_use]
    pub fn attempts(&self) -> u32 {
        self.attempts
    }

    /// The attempt currently in flight, if any.
    #[must_use]
    pub fn lease(&self) -> Option<&Lease> {
        self.lease.as_ref()
    }

    /// The attempt in flight that has not expired at `now_ms`.
    ///
    /// This is the only lease that may refuse a claim or a pass: an expired one
    /// belongs to a process that is gone.
    #[must_use]
    pub fn active_lease(&self, now_ms: i64) -> Option<&Lease> {
        self.lease
            .as_ref()
            .filter(|lease| !lease.is_expired(now_ms))
    }

    /// Whether this destination is finished for good.
    #[must_use]
    pub fn is_terminal(&self) -> bool {
        self.outcome
            .as_ref()
            .is_some_and(DestinationOutcome::is_terminal)
    }

    /// Whether a run at `now_ms` has work to do here.
    #[must_use]
    pub fn needs_work(&self, now_ms: i64) -> bool {
        match self.outcome.as_ref() {
            None => true,
            Some(outcome) => outcome.is_actionable(now_ms),
        }
    }

    /// Rebuild a record from its parts.
    ///
    /// Used by the journal when it loads a persisted plan: the record is
    /// reconstructed exactly as it was written, never re-derived from
    /// configuration that may have changed since.
    #[must_use]
    pub(crate) fn from_parts(
        platform: String,
        intent: Option<DestinationIntent>,
        outcome: Option<DestinationOutcome>,
        attempts: u32,
        lease: Option<Lease>,
    ) -> Self {
        Self {
            platform,
            intent,
            outcome,
            attempts,
            lease,
        }
    }
}

/// Why a destination cannot be attempted right now.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ClaimError {
    #[error("{platform} is already held by {owner} (attempt {attempt_no}); one owner per attempt")]
    Held {
        platform: String,
        owner: String,
        attempt_no: u32,
    },
    #[error("{platform} has settled as {outcome} and cannot be attempted again")]
    Settled { platform: String, outcome: String },
    #[error("the plan has no destination {platform}")]
    Unknown { platform: String },
}

/// The frozen plan. Re-running it publishes only what is still outstanding.
#[derive(Debug, Clone)]
pub struct ConfirmedPlan {
    plan_id: PlanId,
    content_hash: String,
    destinations: Vec<DestinationRecord>,
}

impl ConfirmedPlan {
    #[must_use]
    pub fn plan_id(&self) -> &str {
        self.plan_id.as_str()
    }

    /// The content identity every destination's [`DeliveryKey`] is built from.
    #[must_use]
    pub fn content_hash(&self) -> &str {
        &self.content_hash
    }

    #[must_use]
    pub fn destinations(&self) -> &[DestinationRecord] {
        &self.destinations
    }

    #[must_use]
    pub fn destination(&self, platform: &str) -> Option<&DestinationRecord> {
        let key = normalize_platform(platform);
        self.destinations
            .iter()
            .find(|record| record.platform == key)
    }

    /// Every destination is finished for good.
    #[must_use]
    pub fn is_complete(&self) -> bool {
        self.destinations.iter().all(DestinationRecord::is_terminal)
    }

    /// Destinations a run at `now_ms` would work on, in plan order.
    #[must_use]
    pub fn outstanding(&self, now_ms: i64) -> Vec<&str> {
        self.destinations
            .iter()
            .filter(|record| record.needs_work(now_ms))
            .map(DestinationRecord::platform)
            .collect()
    }

    /// The standing per-outcome tally for the whole plan.
    #[must_use]
    pub fn counts(&self) -> OutcomeCounts {
        let mut counts = OutcomeCounts::default();
        for record in &self.destinations {
            counts.add(record.outcome.as_ref());
        }
        counts
    }

    /// Take the attempt lease for `platform`.
    ///
    /// The returned attempt number is the one a new provider call would be.
    /// A live lease held by another owner is refused, never stolen: that is what
    /// stops two runs from publishing one destination twice. A lease that has
    /// *expired* is taken over, because its owner is gone — and because the
    /// outcome it left behind decides what happens next, a takeover never
    /// retries an unconfirmed attempt blind.
    pub fn claim(&mut self, platform: &str, owner: &str, now_ms: i64) -> Result<u32, ClaimError> {
        let key = normalize_platform(platform);
        let record = self
            .destinations
            .iter_mut()
            .find(|record| record.platform == key)
            .ok_or_else(|| ClaimError::Unknown {
                platform: key.clone(),
            })?;
        if record.is_terminal() {
            return Err(ClaimError::Settled {
                platform: key,
                outcome: record
                    .outcome
                    .as_ref()
                    .map_or_else(String::new, |outcome| outcome.as_str().to_owned()),
            });
        }
        if let Some(lease) = record.active_lease(now_ms) {
            if lease.owner != owner {
                return Err(ClaimError::Held {
                    platform: key,
                    owner: lease.owner.clone(),
                    attempt_no: lease.attempt_no,
                });
            }
        }
        let attempt_no = record.attempts.saturating_add(1);
        record.lease = Some(Lease {
            owner: owner.to_owned(),
            attempt_no,
            expires_at_ms: now_ms.saturating_add(DEFAULT_LEASE_TTL_MS),
        });
        Ok(attempt_no)
    }

    /// Release the lease, if `owner` holds it.
    pub fn release(&mut self, platform: &str, owner: &str) {
        let key = normalize_platform(platform);
        if let Some(record) = self
            .destinations
            .iter_mut()
            .find(|record| record.platform == key)
        {
            if record
                .lease
                .as_ref()
                .is_some_and(|lease| lease.owner == owner)
            {
                record.lease = None;
            }
        }
    }

    /// The owner holding this destination's attempt, if any.
    #[must_use]
    pub fn lease_owner(&self, platform: &str) -> Option<&str> {
        self.destination(platform)
            .and_then(DestinationRecord::lease)
            .map(|lease| lease.owner.as_str())
    }

    /// Rebuild a plan from a persisted record.
    ///
    /// The only caller is the journal, and it passes back exactly what it wrote:
    /// the plan id, the content hash and the per-destination records. Nothing is
    /// re-derived, so a resume cannot silently differ from the confirmation it
    /// claims to continue.
    #[must_use]
    pub(crate) fn from_parts(
        plan_id: PlanId,
        content_hash: String,
        destinations: Vec<DestinationRecord>,
    ) -> Self {
        Self {
            plan_id,
            content_hash,
            destinations,
        }
    }

    pub(crate) fn settle(&mut self, index: usize, outcome: DestinationOutcome) {
        if let Some(record) = self.destinations.get_mut(index) {
            record.outcome = Some(outcome);
        }
    }

    pub(crate) fn count_attempt(&mut self, index: usize) {
        if let Some(record) = self.destinations.get_mut(index) {
            record.attempts = record.attempts.saturating_add(1);
        }
    }

    pub(crate) fn attempts(&self, index: usize) -> u32 {
        self.destinations
            .get(index)
            .map_or(0, |record| record.attempts)
    }
}

/// Why a route plan cannot be confirmed.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ConfirmError {
    #[error("the route plan carries no destinations, so there is nothing to publish")]
    NoDestinations,
    #[error("the route plan is blocked before any destination: {detail}")]
    RequestBlocked { detail: String },
    #[error("the content has no identity hash, so the plan cannot be made idempotent")]
    MissingContentHash,
    #[error("the plan accepted {platform} but resolved no content type")]
    UnresolvedContentType { platform: String },
}

/// Freeze a route plan into a [`ConfirmedPlan`].
///
/// This is the "confirm" step: it is where a human/agent decision becomes the
/// thing that will be executed. It never invents a destination, never drops one,
/// and never turns a refusal into a pending attempt.
pub fn confirm(
    plan: &RoutePlan,
    content: &PublicationContent,
) -> Result<ConfirmedPlan, ConfirmError> {
    if let Some(blocker) = plan.request_blockers.first() {
        return Err(ConfirmError::RequestBlocked {
            detail: if blocker.message.starts_with(&blocker.code) {
                blocker.message.clone()
            } else {
                format!("{}: {}", blocker.code, blocker.message)
            },
        });
    }
    if plan.destinations.is_empty() {
        return Err(ConfirmError::NoDestinations);
    }
    if content.content_hash().trim().is_empty() {
        return Err(ConfirmError::MissingContentHash);
    }

    let mut destinations = Vec::with_capacity(plan.destinations.len());
    for verdict in &plan.destinations {
        let platform = normalize_platform(&verdict.platform);
        if verdict.accepted {
            let content_type =
                plan.content_type
                    .ok_or_else(|| ConfirmError::UnresolvedContentType {
                        platform: platform.clone(),
                    })?;
            let intent = DestinationIntent {
                platform: platform.clone(),
                content_type,
                mode: verdict.mode,
                caption: content.caption_for(&platform).to_owned(),
                media: content.media().to_vec(),
                key: DeliveryKey::new(&platform, content.content_hash()),
            };
            destinations.push(DestinationRecord {
                platform,
                intent: Some(intent),
                outcome: None,
                attempts: 0,
                lease: None,
            });
        } else {
            let (code, detail) = refusal_of(verdict);
            destinations.push(DestinationRecord {
                platform,
                intent: None,
                outcome: Some(DestinationOutcome::Refused(RefusedOutcome {
                    code,
                    detail,
                    source: RefusalSource::Plan,
                    retry_after_ms: None,
                })),
                attempts: 0,
                lease: None,
            });
        }
    }

    let plan_id = PlanId::derive(&destinations, content.content_hash());
    Ok(ConfirmedPlan {
        plan_id,
        content_hash: content.content_hash().trim().to_owned(),
        destinations,
    })
}

/// A confirmed plan in which one destination is re-confirmed from a read-back.
///
/// Used by the reconcile path only: it is the one way a `Published` outcome may
/// be produced without a provider publish call, and it always says so through
/// [`PublishedOutcome::evidence`].
pub(crate) fn readback_published(post_id: String, url: Option<String>) -> PublishedOutcome {
    PublishedOutcome {
        post_id,
        url,
        evidence: PublishEvidence::Readback,
    }
}

/// The decisive refusal of a verdict: first code, one sentence.
///
/// Python's rule is preserved: the refusal sentence is the message, prefixed
/// with the code only when the message does not already begin with it.
fn refusal_of(verdict: &DestinationVerdict) -> (String, String) {
    match verdict.refusals.first() {
        Some(reason) => {
            let code = reason.code.clone();
            let detail = if reason.message.starts_with(&code) {
                reason.message.clone()
            } else {
                format!("{code}: {}", reason.message)
            };
            (code, detail)
        }
        None => {
            let code = verdict.state.as_str().to_owned();
            (
                code.clone(),
                format!("{code}: the route plan refused this destination without a stated reason"),
            )
        }
    }
}

/// Platform ids are lower-case everywhere they are compared or persisted.
#[must_use]
pub fn normalize_platform(platform: &str) -> String {
    platform.trim().to_ascii_lowercase()
}
