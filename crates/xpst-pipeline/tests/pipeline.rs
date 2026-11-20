//! The behaviours that were defects before they were tests.
//!
//! Each test below pins one of them:
//!
//! * a deferred destination is not a failure and stays retryable;
//! * a dedup no-op is `skipped_duplicate`, never `posted`;
//! * a limits refusal never reaches the provider;
//! * an unknown outcome is reconciled before any retry;
//! * re-running a partially completed plan publishes only what was left;
//! * one owner per destination per attempt, and a bounded retry budget.
//!
//! Everything runs against the in-crate fakes: no socket, no clock, no
//! credentials.

use xpst_core::content::ContentType;
use xpst_core::ingest::DestinationCapability;
use xpst_core::plan::{DestinationState, DestinationVerdict, Reason, RoutePlan, RouteRequest};

use xpst_pipeline::fake::{CallLog, FakeGate, FakePublisher};
use xpst_pipeline::{
    confirm, ConfirmError, ConfirmedPlan, DestinationOutcome, DestinationReport, FailureClass,
    GateDecision, Pipeline, PlanState, ProviderOutcome, PublicationContent, PublishContext,
    PublishError, PublisherSet, ReconcileOutcome, RefusalSource, RetryBudget, StateSink,
    StateWriteError,
};

const NOW: i64 = 1_700_000_000_000;
const OWNER: &str = "run-owner";

fn text_verdict(platform: &str) -> DestinationVerdict {
    DestinationVerdict {
        platform: platform.to_owned(),
        accepted: true,
        state: DestinationState::Posted,
        content_type: Some("text".to_owned()),
        mode: None,
        refusals: Vec::new(),
        advisories: Vec::new(),
        transforms: Vec::new(),
    }
}

fn refused_verdict(platform: &str, code: &str, message: &str) -> DestinationVerdict {
    DestinationVerdict {
        platform: platform.to_owned(),
        accepted: false,
        state: DestinationState::UnsupportedFormat,
        content_type: Some("text".to_owned()),
        mode: None,
        refusals: vec![Reason {
            code: code.to_owned(),
            message: message.to_owned(),
        }],
        advisories: Vec::new(),
        transforms: Vec::new(),
    }
}

fn route(verdicts: Vec<DestinationVerdict>) -> RoutePlan {
    let accepted = verdicts
        .iter()
        .filter(|verdict| verdict.accepted)
        .map(|verdict| verdict.platform.clone())
        .collect();
    RoutePlan {
        content_type: Some(ContentType::Text),
        accepted,
        destinations: verdicts,
        request_blockers: Vec::new(),
    }
}

fn content() -> PublicationContent {
    PublicationContent::new("fic-content-hash-0001", "hello from the pipeline")
}

fn confirmed(verdicts: Vec<DestinationVerdict>) -> ConfirmedPlan {
    confirm(&route(verdicts), &content()).expect("confirm")
}

/// A state sink that records what it saw, in the shared call log.
struct RecordingSink {
    log: CallLog,
    entries: Vec<DestinationReport>,
    claims: Vec<(String, String)>,
}

impl RecordingSink {
    fn new(log: CallLog) -> Self {
        Self {
            log,
            entries: Vec::new(),
            claims: Vec::new(),
        }
    }
}

impl StateSink for RecordingSink {
    fn record(
        &mut self,
        plan_id: &str,
        _now_ms: i64,
        record: &xpst_pipeline::DestinationRecord,
    ) -> Result<(), StateWriteError> {
        let _ = plan_id;
        let report = DestinationReport {
            platform: record.platform().to_owned(),
            outcome: record.outcome().cloned(),
            attempted_this_run: true,
            attempts_this_run: record.attempts(),
            state_write_error: None,
        };
        self.log.push(format!(
            "state:{}:{}",
            report.platform,
            report.outcome_word()
        ));
        self.entries.push(report);
        Ok(())
    }

    fn claim(
        &mut self,
        _plan_id: &str,
        _now_ms: i64,
        record: &xpst_pipeline::DestinationRecord,
    ) -> Result<(), StateWriteError> {
        if let Some(lease) = record.lease() {
            self.claims
                .push((record.platform().to_owned(), lease.owner.clone()));
        }
        Ok(())
    }
}

/// A state sink that cannot write.
struct BrokenSink;

impl StateSink for BrokenSink {
    fn record(
        &mut self,
        _plan_id: &str,
        _now_ms: i64,
        _record: &xpst_pipeline::DestinationRecord,
    ) -> Result<(), StateWriteError> {
        Err(StateWriteError::new("disk full"))
    }
}

#[tokio::test]
async fn full_success_publishes_after_the_gate_and_records_before_the_state_write() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone());
    let alpha = FakePublisher::publishing("alpha").logging_on(log.clone());
    let beta = FakePublisher::publishing("beta").logging_on(log.clone());
    let publishers = PublisherSet::new().with(&alpha).with(&beta);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha"), text_verdict("beta")]);
    let mut sink = RecordingSink::new(log.clone());

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: Some(&mut sink),
            },
        )
        .await
        .expect("publish");

    assert_eq!(
        log.entries(),
        vec![
            "gate:alpha",
            "publish:alpha",
            "state:alpha:published",
            "gate:beta",
            "publish:beta",
            "state:beta:published",
        ],
        "the gate decides first, the provider is called second, the state write is last"
    );
    let counts = report.counts();
    assert_eq!(counts.published, 2);
    assert_eq!(counts.total(), 2, "every destination is accounted for");
    assert_eq!(counts.pending, 0);
    assert_eq!(report.state(), PlanState::Posted);
    assert!(!report.state().is_failure());
    assert_eq!(report.published(), vec!["alpha", "beta"]);
    assert!(plan.is_complete());
    assert_eq!(sink.entries.len(), 2);
    assert_eq!(
        sink.entries[0]
            .outcome
            .as_ref()
            .map(DestinationOutcome::as_str),
        Some("published")
    );
}

#[tokio::test]
async fn a_deferral_is_not_a_failure_and_stays_retryable() {
    let now = NOW;
    let resume = NOW + 600_000;
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone())
        .once("beta", GateDecision::defer(resume, "outside posting hours"));
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::publishing("beta");
    let publishers = PublisherSet::new().with(&alpha).with(&beta);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha"), text_verdict("beta")]);

    let first = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: now,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("first pass");

    let counts = first.counts();
    assert_eq!(counts.published, 1);
    assert_eq!(counts.deferred, 1);
    assert_eq!(counts.failures(), 0, "a deferral is never a failure");
    assert_eq!(first.state(), PlanState::Partial);
    assert_eq!(beta.publish_calls(), 0, "deferred means not attempted");
    assert_eq!(first.deferred(), vec!["beta"]);
    match first.entry("beta").and_then(|entry| entry.outcome.as_ref()) {
        Some(DestinationOutcome::Deferred(deferred)) => {
            assert_eq!(deferred.resume_after_ms, resume);
            assert_eq!(deferred.source, xpst_pipeline::DeferSource::Pacing);
        }
        other => panic!("expected a deferral, got {other:?}"),
    }

    // Second pass, still inside the window: nothing is asked of the gate and
    // nothing is asked of the provider.
    let calls_before = log.len();
    let second = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: now + 1_000,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("waiting pass");
    assert_eq!(second.counts(), counts, "waiting changes nothing");
    assert_eq!(
        log.len(),
        calls_before,
        "a waiting destination is not re-asked"
    );
    assert!(!second.entry("beta").expect("beta").attempted_this_run);

    // Third pass, at the window: only the destination that was waiting goes out.
    let third = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: resume,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("resumed pass");
    assert_eq!(third.counts().published, 2);
    assert_eq!(third.state(), PlanState::Posted);
    assert_eq!(alpha.publish_calls(), 1);
    assert_eq!(beta.publish_calls(), 1);
    assert!(plan.is_complete());
}

#[tokio::test]
async fn a_dedup_no_op_is_skipped_duplicate_and_never_counted_as_posted() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log);
    let alpha = FakePublisher::scripted("alpha").then(ProviderOutcome::Duplicate {
        existing_post_id: Some("old-post-1".to_owned()),
    });
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha")]);

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("publish");

    let counts = report.counts();
    assert_eq!(counts.skipped_duplicate, 1);
    assert_eq!(counts.published, 0, "a dedupe no-op is not a post");
    assert_eq!(counts.total(), 1);
    assert_eq!(report.state(), PlanState::SkippedDuplicate);
    assert!(!report.state().is_failure());
    assert!(report.published().is_empty());
    assert_eq!(report.skipped_duplicates(), vec!["alpha"]);
    assert_eq!(alpha.publish_calls(), 1);
    assert!(plan.is_complete());
}

#[tokio::test]
async fn a_duplicate_beside_a_post_does_not_inflate_the_post_count() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log);
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::scripted("beta").then(ProviderOutcome::Duplicate {
        existing_post_id: None,
    });
    let publishers = PublisherSet::new().with(&alpha).with(&beta);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha"), text_verdict("beta")]);

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("publish");

    let counts = report.counts();
    assert_eq!(counts.published, 1);
    assert_eq!(counts.skipped_duplicate, 1);
    assert_eq!(counts.total(), 2);
    assert_eq!(report.published(), vec!["alpha"]);
    assert_eq!(report.state(), PlanState::Posted);
}

#[tokio::test]
async fn a_limits_refusal_short_circuits_the_provider() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone()).always(
        "alpha",
        GateDecision::refuse_with_retry(
            "COOLDOWN_ACTIVE",
            "challenge-hard-stop: cooling for another 900s",
            Some(900_000),
        ),
    );
    let alpha = FakePublisher::publishing("alpha").logging_on(log.clone());
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha")]);

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("publish");

    assert_eq!(
        alpha.publish_calls(),
        0,
        "a refused destination never reaches the provider"
    );
    assert_eq!(log.entries(), vec!["gate:alpha"]);
    let counts = report.counts();
    assert_eq!(counts.refused, 1);
    assert_eq!(counts.published, 0);
    assert_eq!(report.state(), PlanState::Refused);
    match report
        .entry("alpha")
        .and_then(|entry| entry.outcome.as_ref())
    {
        Some(DestinationOutcome::Refused(refused)) => {
            assert_eq!(refused.code, "COOLDOWN_ACTIVE");
            assert_eq!(refused.source, RefusalSource::Limits);
            assert_eq!(refused.retry_after_ms, Some(900_000));
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
    // A limits refusal is not terminal: the next pass asks the gate again.
    assert!(!plan.is_complete());
    assert_eq!(plan.outstanding(NOW), vec!["alpha"]);
}

#[tokio::test]
async fn a_plan_refusal_is_reported_and_never_attempted() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone());
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::publishing("beta");
    let publishers = PublisherSet::new().with(&alpha).with(&beta);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![
        text_verdict("alpha"),
        refused_verdict(
            "beta",
            "PLATFORM_DISABLED",
            "beta is disabled in local configuration.",
        ),
    ]);

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("publish");

    assert_eq!(beta.publish_calls(), 0);
    assert_eq!(log.count("gate:beta"), 0, "a plan refusal is not re-asked");
    let counts = report.counts();
    assert_eq!(counts.published, 1);
    assert_eq!(counts.refused, 1);
    assert_eq!(counts.total(), 2);
    assert_eq!(report.state(), PlanState::Partial);
    match report
        .entry("beta")
        .and_then(|entry| entry.outcome.as_ref())
    {
        Some(DestinationOutcome::Refused(refused)) => {
            assert_eq!(refused.code, "PLATFORM_DISABLED");
            assert_eq!(refused.source, RefusalSource::Plan);
        }
        other => panic!("expected a plan refusal, got {other:?}"),
    }

    // Terminal: a second pass neither publishes nor re-asks.
    let second = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW + 1_000,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("second pass");
    assert_eq!(second.counts(), counts);
    assert_eq!(alpha.publish_calls(), 1);
    assert_eq!(log.count("gate:beta"), 0);
}

#[tokio::test]
async fn an_unconfirmed_attempt_must_be_reconciled_before_it_is_retried() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone());
    let alpha = FakePublisher::scripted("alpha")
        .then(ProviderOutcome::Unknown {
            detail: "the socket closed before the response arrived".to_owned(),
        })
        .then(ProviderOutcome::Published {
            post_id: "alpha-post-2".to_owned(),
            url: None,
        })
        .reconcile_once(ReconcileOutcome::unknown("the read-back timed out"))
        .reconcile_once(ReconcileOutcome::Absent);
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha")]);

    let first = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("first pass");

    assert_eq!(first.counts().unconfirmed, 1);
    assert_eq!(first.counts().published, 0);
    assert_eq!(first.state(), PlanState::Unconfirmed);
    assert!(first.state().is_failure());
    assert_eq!(alpha.publish_calls(), 1);
    assert_eq!(
        plan.lease_owner("alpha"),
        Some(OWNER),
        "the ambiguous attempt keeps its owner"
    );

    // Second pass: the read-back cannot answer, so nothing is retried.
    let second = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW + 1_000,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("second pass");
    assert_eq!(second.counts().unconfirmed, 1);
    assert_eq!(
        alpha.publish_calls(),
        1,
        "an unresolved outcome is never retried blind"
    );
    assert_eq!(alpha.reconcile_calls(), 1);

    // Third pass: the read-back proves nothing landed, so the retry is safe.
    let third = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW + 2_000,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("third pass");
    assert_eq!(third.counts().published, 1);
    assert_eq!(third.state(), PlanState::Posted);
    assert_eq!(alpha.publish_calls(), 2);
    assert_eq!(alpha.reconcile_calls(), 2);
    assert!(plan.is_complete());
}

#[tokio::test]
async fn a_reconcile_that_finds_the_post_publishes_from_evidence_without_a_second_call() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log);
    let alpha = FakePublisher::scripted("alpha")
        .then(ProviderOutcome::Unknown {
            detail: "response lost".to_owned(),
        })
        .reconcile_always(ReconcileOutcome::Found {
            post_id: "alpha-already-there".to_owned(),
            url: Some("https://example.invalid/alpha/1".to_owned()),
        });
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha")]);

    pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("first pass");

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW + 1_000,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("reconcile pass");

    assert_eq!(alpha.publish_calls(), 1, "the read-back replaced the retry");
    match report
        .entry("alpha")
        .and_then(|entry| entry.outcome.as_ref())
    {
        Some(DestinationOutcome::Published(published)) => {
            assert_eq!(published.post_id, "alpha-already-there");
            assert_eq!(
                published.evidence,
                xpst_pipeline::PublishEvidence::Readback,
                "a read-back is never reported as a provider acknowledgement"
            );
        }
        other => panic!("expected a published outcome, got {other:?}"),
    }
    assert_eq!(report.state(), PlanState::Posted);
}

#[tokio::test]
async fn re_running_a_partial_plan_publishes_only_what_is_left() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone());
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::scripted("beta").then(ProviderOutcome::permanent(
        "MEDIA_REJECTED",
        "the platform refused the file",
    ));
    let gamma = FakePublisher::scripted("gamma")
        .then(ProviderOutcome::retryable(
            "GATEWAY_TIMEOUT",
            "504 from the upload endpoint",
        ))
        .then(ProviderOutcome::Published {
            post_id: "gamma-post-2".to_owned(),
            url: None,
        });
    let publishers = PublisherSet::new().with(&alpha).with(&beta).with(&gamma);
    let mut pipeline = Pipeline::new(&mut gate, publishers).with_budget(RetryBudget::new(1));
    let mut plan = confirmed(vec![
        text_verdict("alpha"),
        text_verdict("beta"),
        text_verdict("gamma"),
    ]);

    let first = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("first pass");
    let counts = first.counts();
    assert_eq!(counts.published, 1);
    assert_eq!(counts.failed_permanent, 1);
    assert_eq!(counts.failed_retryable, 1);
    assert_eq!(counts.total(), 3);
    assert_eq!(first.state(), PlanState::Partial);

    let second = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW + 60_000,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("second pass");

    assert_eq!(
        alpha.publish_calls(),
        1,
        "a published destination is left alone"
    );
    assert_eq!(
        beta.publish_calls(),
        1,
        "a permanent failure is not retried"
    );
    assert_eq!(
        gamma.publish_calls(),
        2,
        "only the unresolved one is retried"
    );
    assert_eq!(second.counts().published, 2);
    assert_eq!(second.counts().failed_permanent, 1);
    assert_eq!(second.counts().total(), 3);
    assert_eq!(second.published(), vec!["alpha", "gamma"]);
}

#[tokio::test]
async fn retryable_failures_stop_at_the_retry_budget() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log);
    let alpha = FakePublisher::scripted("alpha").then_all(vec![
        ProviderOutcome::retryable("GATEWAY_TIMEOUT", "504"),
        ProviderOutcome::retryable("GATEWAY_TIMEOUT", "504"),
        ProviderOutcome::retryable("GATEWAY_TIMEOUT", "504"),
    ]);
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers).with_budget(RetryBudget::new(2));
    let mut plan = confirmed(vec![text_verdict("alpha")]);

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("publish");

    assert_eq!(alpha.publish_calls(), 2, "the budget is a hard ceiling");
    assert_eq!(report.counts().failed_retryable, 1);
    assert_eq!(report.state(), PlanState::Failed);
    match report
        .entry("alpha")
        .and_then(|entry| entry.outcome.as_ref())
    {
        Some(DestinationOutcome::Failed(failed)) => {
            assert_eq!(failed.class, FailureClass::Retryable);
            assert_eq!(failed.attempts, 2, "the outcome states what it cost");
        }
        other => panic!("expected a failure, got {other:?}"),
    }
}

#[tokio::test]
async fn a_rate_limit_defers_and_tells_the_gate() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log);
    let alpha = FakePublisher::scripted("alpha").then(ProviderOutcome::RateLimited {
        retry_after_ms: Some(120_000),
        detail: "429 with Retry-After: 120".to_owned(),
    });
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha")]);

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("publish");

    assert_eq!(report.counts().deferred, 1);
    assert_eq!(
        report.counts().failures(),
        0,
        "a rate limit is not a failure"
    );
    match report
        .entry("alpha")
        .and_then(|entry| entry.outcome.as_ref())
    {
        Some(DestinationOutcome::Deferred(deferred)) => {
            assert_eq!(deferred.resume_after_ms, NOW + 120_000);
            assert_eq!(deferred.source, xpst_pipeline::DeferSource::RateLimit);
        }
        other => panic!("expected a deferral, got {other:?}"),
    }
    assert_eq!(gate.rate_limits(), [("alpha".to_owned(), Some(120_000))]);
}

#[tokio::test]
async fn a_destination_leased_by_another_owner_is_refused_not_stolen() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone());
    let alpha = FakePublisher::publishing("alpha").logging_on(log.clone());
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha")]);
    plan.claim("alpha", "another-agent", NOW).expect("lease");

    let error = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect_err("a foreign lease must stop the pass");

    assert_eq!(
        error,
        PublishError::DestinationLeased {
            platform: "alpha".to_owned(),
            owner: "another-agent".to_owned(),
            attempt_no: 1,
        }
    );
    assert!(
        log.is_empty(),
        "nothing was decided and nothing was published"
    );
    assert_eq!(alpha.publish_calls(), 0);
    assert_eq!(plan.lease_owner("alpha"), Some("another-agent"));
}

#[tokio::test]
async fn a_plan_that_needs_an_unregistered_publisher_fails_before_anything_is_called() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log.clone());
    let alpha = FakePublisher::publishing("alpha").logging_on(log.clone());
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha"), text_verdict("gamma")]);

    let error = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect_err("gamma has no publisher");

    assert_eq!(
        error,
        PublishError::NoPublisher {
            platform: "gamma".to_owned()
        }
    );
    assert!(
        log.is_empty(),
        "the wiring mistake is found before any call"
    );
    assert_eq!(alpha.publish_calls(), 0);
}

#[tokio::test]
async fn a_failed_state_write_is_reported_without_rewriting_the_outcome() {
    let log = CallLog::new();
    let mut gate = FakeGate::new(log);
    let alpha = FakePublisher::publishing("alpha");
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = confirmed(vec![text_verdict("alpha")]);
    let mut sink = BrokenSink;

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: Some(&mut sink),
            },
        )
        .await
        .expect("publish");

    let entry = report.entry("alpha").expect("alpha");
    assert_eq!(entry.outcome_word(), "published");
    assert_eq!(entry.state_write_error.as_deref(), Some("disk full"));
}

#[test]
fn confirm_refuses_a_blocked_request_and_an_empty_plan() {
    let blocked = RoutePlan {
        content_type: Some(ContentType::Text),
        accepted: Vec::new(),
        destinations: Vec::new(),
        request_blockers: vec![Reason {
            code: "content_type.unknown".to_owned(),
            message: "Unknown content type 'sticker': choose one of text.".to_owned(),
        }],
    };
    let error = confirm(&blocked, &content()).expect_err("blocked");
    assert!(matches!(error, ConfirmError::RequestBlocked { .. }));

    let empty = RoutePlan {
        content_type: Some(ContentType::Text),
        accepted: Vec::new(),
        destinations: Vec::new(),
        request_blockers: Vec::new(),
    };
    assert_eq!(
        confirm(&empty, &content()).expect_err("empty"),
        ConfirmError::NoDestinations
    );

    let no_identity = PublicationContent::new("  ", "hello");
    assert_eq!(
        confirm(&route(vec![text_verdict("alpha")]), &no_identity).expect_err("no hash"),
        ConfirmError::MissingContentHash
    );
}

#[test]
fn the_plan_id_is_derived_from_the_content_and_the_intents() {
    let verdicts = || vec![text_verdict("alpha"), text_verdict("beta")];
    let first = confirmed(verdicts());
    let again = confirmed(verdicts());
    assert_eq!(
        first.plan_id(),
        again.plan_id(),
        "the same confirmation is one plan"
    );

    let renamed = confirm(
        &route(verdicts()),
        &PublicationContent::new("fic-content-hash-0001", "hello from the pipeline!"),
    )
    .expect("confirm");
    assert_ne!(
        first.plan_id(),
        renamed.plan_id(),
        "different copy is a different plan"
    );

    let per_destination = confirm(
        &route(verdicts()),
        &content().with_caption_for("beta", "a different caption for beta"),
    )
    .expect("confirm");
    assert_ne!(first.plan_id(), per_destination.plan_id());
    assert_eq!(
        per_destination
            .destination("beta")
            .and_then(|record| record.intent())
            .map(|intent| intent.caption.as_str()),
        Some("a different caption for beta")
    );
    assert_eq!(
        per_destination.content_hash(),
        "fic-content-hash-0001",
        "the idempotency identity never changes with the caption"
    );
}

#[tokio::test]
async fn the_route_planner_confirms_and_publishes_a_text_post_end_to_end() {
    let request = RouteRequest::new(Vec::new(), "hello from the rust pipeline");
    let destinations =
        vec![DestinationCapability::built_in("bluesky", true, true).expect("bluesky capability")];

    let log = CallLog::new();
    let mut gate = FakeGate::new(log);
    let bluesky = FakePublisher::publishing("bluesky");
    let publishers = PublisherSet::new().with(&bluesky);
    let mut pipeline = Pipeline::new(&mut gate, publishers);

    let route_plan = pipeline.plan(&request, &destinations);
    assert!(route_plan.ready());
    let mut plan = pipeline
        .confirm(&route_plan, &content())
        .expect("confirm a ready plan");
    assert_eq!(plan.destinations().len(), 1);

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: "acct-1",
                owner: OWNER,
                state: None,
            },
        )
        .await
        .expect("publish");

    assert_eq!(report.counts().published, 1);
    assert_eq!(report.state(), PlanState::Posted);
    let intent = plan
        .destination("bluesky")
        .and_then(|record| record.intent())
        .expect("intent");
    assert_eq!(intent.content_type, ContentType::Text);
    assert_eq!(intent.key.as_str(), "bluesky:fic-content-hash-0001");
}
