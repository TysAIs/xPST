//! In-memory doubles for the two seams: the limits gate and a destination.
//!
//! They ship in the crate rather than behind `#[cfg(test)]` because a caller
//! building its own integration needs the same thing: a way to drive the
//! pipeline with no socket, no clock and no credentials. Every call is recorded
//! in a shared [`CallLog`] so a test can assert not only the outcome but the
//! *order* — that the gate ran before the provider, and that neither ran when a
//! refusal said not to.

use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;

use crate::gate::{GateDecision, GateRequest, LimitsGate};
use crate::intent::{DeliveryKey, DestinationIntent};
use crate::provider::{DestinationPublisher, ProviderOutcome, ReconcileOutcome};

/// An append-only record of what the doubles were asked to do.
///
/// Entries are `gate:<platform>`, `publish:<platform>`, `reconcile:<platform>`
/// and `rate-limit:<platform>`, so an assertion can read as the sequence the
/// pipeline promised.
#[derive(Debug, Clone, Default)]
pub struct CallLog {
    entries: Arc<Mutex<Vec<String>>>,
}

impl CallLog {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&self, entry: impl Into<String>) {
        self.entries
            .lock()
            .expect("call log mutex poisoned")
            .push(entry.into());
    }

    #[must_use]
    pub fn entries(&self) -> Vec<String> {
        self.entries
            .lock()
            .expect("call log mutex poisoned")
            .clone()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.lock().expect("call log mutex poisoned").len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// How many times `entry` appears.
    #[must_use]
    pub fn count(&self, entry: &str) -> usize {
        self.entries()
            .iter()
            .filter(|candidate| candidate.as_str() == entry)
            .count()
    }

    /// How many entries begin with `prefix`.
    #[must_use]
    pub fn count_prefix(&self, prefix: &str) -> usize {
        self.entries()
            .iter()
            .filter(|candidate| candidate.starts_with(prefix))
            .count()
    }
}

/// A gate with scripted answers.
///
/// `always` answers every call for a platform; `once` answers the next call and
/// then falls back. An unscripted platform is allowed, which keeps the happy
/// path free of ceremony.
pub struct FakeGate {
    log: CallLog,
    sticky: BTreeMap<String, GateDecision>,
    queued: BTreeMap<String, VecDeque<GateDecision>>,
    rate_limits: Vec<(String, Option<i64>)>,
}

impl FakeGate {
    #[must_use]
    pub fn new(log: CallLog) -> Self {
        Self {
            log,
            sticky: BTreeMap::new(),
            queued: BTreeMap::new(),
            rate_limits: Vec::new(),
        }
    }

    /// Answer every call for `platform` with `decision`.
    #[must_use]
    pub fn always(mut self, platform: &str, decision: GateDecision) -> Self {
        self.sticky.insert(platform.to_owned(), decision);
        self
    }

    /// Answer the next call for `platform` with `decision`, then fall back.
    #[must_use]
    pub fn once(mut self, platform: &str, decision: GateDecision) -> Self {
        self.queued
            .entry(platform.to_owned())
            .or_default()
            .push_back(decision);
        self
    }

    /// Every rate limit the pipeline reported, in order.
    #[must_use]
    pub fn rate_limits(&self) -> &[(String, Option<i64>)] {
        &self.rate_limits
    }
}

impl LimitsGate for FakeGate {
    fn check(&mut self, request: &GateRequest<'_>) -> GateDecision {
        self.log.push(format!("gate:{}", request.platform));
        if let Some(queue) = self.queued.get_mut(request.platform) {
            if let Some(next) = queue.pop_front() {
                return next;
            }
        }
        self.sticky
            .get(request.platform)
            .cloned()
            .unwrap_or(GateDecision::Allow)
    }

    fn note_rate_limit(
        &mut self,
        platform: &str,
        _account: &str,
        _at_ms: i64,
        retry_after_ms: Option<i64>,
        _detail: &str,
    ) {
        self.log.push(format!("rate-limit:{platform}"));
        self.rate_limits.push((platform.to_owned(), retry_after_ms));
    }
}

/// A destination that publishes what its script says, and records the calls.
///
/// [`FakePublisher::publishing`] acknowledges every publish with a deterministic
/// post id. [`FakePublisher::scripted`] answers only from its script and treats a
/// call past the script as a permanent failure, so a test that forgets to script
/// an attempt fails loudly instead of quietly succeeding.
pub struct FakePublisher {
    platform: String,
    log: CallLog,
    script: Mutex<VecDeque<ProviderOutcome>>,
    reconciles: Mutex<VecDeque<ReconcileOutcome>>,
    fallback_publish: Option<ProviderOutcome>,
    fallback_reconcile: Option<ReconcileOutcome>,
    publishes: AtomicU32,
    reconciliations: AtomicU32,
}

impl FakePublisher {
    /// A destination that acknowledges every publish with a generated post id.
    #[must_use]
    pub fn publishing(platform: &str) -> Self {
        Self {
            platform: platform.to_owned(),
            log: CallLog::new(),
            script: Mutex::new(VecDeque::new()),
            reconciles: Mutex::new(VecDeque::new()),
            fallback_publish: None,
            fallback_reconcile: None,
            publishes: AtomicU32::new(0),
            reconciliations: AtomicU32::new(0),
        }
    }

    /// A destination that answers only from its script.
    #[must_use]
    pub fn scripted(platform: &str) -> Self {
        let mut publisher = Self::publishing(platform);
        publisher.fallback_publish = Some(ProviderOutcome::permanent(
            "FAKE_SCRIPT_EXHAUSTED",
            format!("{platform} was called more often than the test script allows"),
        ));
        publisher
    }

    /// Queue one publish answer.
    #[must_use]
    pub fn then(self, outcome: ProviderOutcome) -> Self {
        self.script
            .lock()
            .expect("fake publisher script poisoned")
            .push_back(outcome);
        self
    }

    /// Queue several publish answers, in order.
    #[must_use]
    pub fn then_all(self, outcomes: impl IntoIterator<Item = ProviderOutcome>) -> Self {
        {
            let mut script = self.script.lock().expect("fake publisher script poisoned");
            for outcome in outcomes {
                script.push_back(outcome);
            }
        }
        self
    }

    /// Queue one read-back answer.
    #[must_use]
    pub fn reconcile_once(self, outcome: ReconcileOutcome) -> Self {
        self.reconciles
            .lock()
            .expect("fake publisher reconciles poisoned")
            .push_back(outcome);
        self
    }

    /// Answer every read-back with `outcome`.
    #[must_use]
    pub fn reconcile_always(mut self, outcome: ReconcileOutcome) -> Self {
        self.fallback_reconcile = Some(outcome);
        self
    }

    /// Record calls in `log` instead of a private one.
    ///
    /// Sharing one log with the gate and the state sink is what lets a test
    /// assert the order the pipeline promised, rather than only the outcome.
    #[must_use]
    pub fn logging_on(mut self, log: CallLog) -> Self {
        self.log = log;
        self
    }

    /// How many publish calls this destination received.
    #[must_use]
    pub fn publish_calls(&self) -> u32 {
        self.publishes.load(Ordering::SeqCst)
    }

    /// How many read-back calls this destination received.
    #[must_use]
    pub fn reconcile_calls(&self) -> u32 {
        self.reconciliations.load(Ordering::SeqCst)
    }

    /// The shared call log.
    #[must_use]
    pub fn log(&self) -> CallLog {
        self.log.clone()
    }
}

#[async_trait]
impl DestinationPublisher for FakePublisher {
    fn platform(&self) -> &str {
        &self.platform
    }

    async fn publish(&self, intent: &DestinationIntent) -> ProviderOutcome {
        let _ = intent;
        self.log.push(format!("publish:{}", self.platform));
        let call = self.publishes.fetch_add(1, Ordering::SeqCst) + 1;
        let scripted = self
            .script
            .lock()
            .expect("fake publisher script poisoned")
            .pop_front();
        if let Some(outcome) = scripted.or_else(|| self.fallback_publish.clone()) {
            return outcome;
        }
        ProviderOutcome::Published {
            post_id: format!("{}-post-{call}", self.platform),
            url: None,
        }
    }

    async fn reconcile(&self, key: &DeliveryKey) -> ReconcileOutcome {
        let _ = key;
        self.log.push(format!("reconcile:{}", self.platform));
        self.reconciliations.fetch_add(1, Ordering::SeqCst);
        let scripted = self
            .reconciles
            .lock()
            .expect("fake publisher reconciles poisoned")
            .pop_front();
        scripted
            .or_else(|| self.fallback_reconcile.clone())
            .unwrap_or_else(|| {
                ReconcileOutcome::unknown(format!(
                    "{} has no scripted read-back, so the attempt stays blocked",
                    self.platform
                ))
            })
    }
}
