//! The pipeline against the real rate-limit ledger.
//!
//! The fake gate proves the pipeline obeys a decision. These tests prove the
//! decision comes from the policy crate in the shape the pipeline expects: a
//! cooldown refuses before the provider is called, and the minimum interval
//! turns a too-soon second post into a deferral rather than a failure.
//!
//! No network, no clock: the ledger is opened at a fixed instant in a throwaway
//! directory under the build's temp area.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

use xpst_core::content::ContentType;
use xpst_core::plan::{DestinationState, DestinationVerdict, RoutePlan};
use xpst_limits::{Platform, PlatformResponse};

use xpst_pipeline::fake::{CallLog, FakePublisher};
use xpst_pipeline::{
    confirm, DestinationOutcome, EnvelopeGate, Pipeline, PlanState, PublicationContent,
    PublishContext, PublisherSet, RefusalSource,
};

const NOW: i64 = 1_700_000_000_000;
const ACCOUNT: &str = "acct-pipeline";

struct TempDir {
    path: PathBuf,
}

impl TempDir {
    fn new(label: &str) -> Self {
        static COUNTER: AtomicU32 = AtomicU32::new(0);
        let id = COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = Path::new(env!("CARGO_TARGET_TMPDIR"))
            .join(format!("xpst-pipeline-{label}-{}-{id}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).expect("create temp dir");
        Self { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

fn accepted(platform: &str) -> DestinationVerdict {
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

fn plan_for(platform: &str, content_hash: &str) -> xpst_pipeline::ConfirmedPlan {
    let verdict = accepted(platform);
    let route = RoutePlan {
        content_type: Some(ContentType::Text),
        accepted: vec![platform.to_owned()],
        destinations: vec![verdict],
        request_blockers: Vec::new(),
    };
    confirm(
        &route,
        &PublicationContent::new(content_hash, "hello from the pipeline"),
    )
    .expect("confirm")
}

#[tokio::test]
async fn the_ledger_cooldown_refuses_before_the_provider_is_called() {
    let dir = TempDir::new("cooldown");
    let mut gate = EnvelopeGate::open_at(dir.path(), NOW).expect("open the rate-limit ledger");
    gate.store_mut()
        .record_hit(
            Platform::X,
            ACCOUNT,
            NOW,
            PlatformResponse::Throttled {
                retry_after_ms: Some(900_000),
                code: None,
            },
        )
        .expect("record the platform's rate limit");

    let log = CallLog::new();
    let x = FakePublisher::publishing("x");
    let publishers = PublisherSet::new().with(&x);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut plan = plan_for("x", "fic-content-hash-cooldown");

    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms: NOW,
                account: ACCOUNT,
                owner: "run-owner",
                state: None,
            },
        )
        .await
        .expect("publish");

    assert_eq!(
        x.publish_calls(),
        0,
        "a cooling account is never handed to a provider"
    );
    assert!(log.is_empty());
    assert_eq!(report.counts().refused, 1);
    assert_eq!(report.state(), PlanState::Refused);
    match report.entry("x").and_then(|entry| entry.outcome.as_ref()) {
        Some(DestinationOutcome::Refused(refused)) => {
            assert_eq!(refused.code, "COOLDOWN_ACTIVE");
            assert_eq!(refused.source, RefusalSource::Limits);
            assert_eq!(refused.retry_after_ms, Some(900_000));
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
}

#[tokio::test]
async fn the_ledger_turns_a_too_soon_second_post_into_a_deferral() {
    let dir = TempDir::new("interval");
    let mut gate = EnvelopeGate::open_at(dir.path(), NOW).expect("open the rate-limit ledger");

    let x = FakePublisher::publishing("x");
    let publishers = PublisherSet::new().with(&x);
    let mut pipeline = Pipeline::new(&mut gate, publishers);

    let mut first = plan_for("x", "fic-content-hash-first");
    let first_report = pipeline
        .publish(
            &mut first,
            PublishContext {
                now_ms: NOW,
                account: ACCOUNT,
                owner: "run-owner",
                state: None,
            },
        )
        .await
        .expect("first publish");
    assert_eq!(first_report.counts().published, 1);

    let mut second = plan_for("x", "fic-content-hash-second");
    let second_report = pipeline
        .publish(
            &mut second,
            PublishContext {
                now_ms: NOW + 1_000,
                account: ACCOUNT,
                owner: "run-owner",
                state: None,
            },
        )
        .await
        .expect("second publish");

    assert_eq!(
        x.publish_calls(),
        1,
        "the paced destination was not handed to the provider"
    );
    assert_eq!(second_report.counts().deferred, 1);
    assert_eq!(second_report.counts().failures(), 0);
    assert_eq!(second_report.state(), PlanState::Deferred);
    match second_report
        .entry("x")
        .and_then(|entry| entry.outcome.as_ref())
    {
        Some(DestinationOutcome::Deferred(deferred)) => {
            assert!(
                deferred.resume_after_ms > NOW + 1_000,
                "the deferral names the instant the window opens"
            );
        }
        other => panic!("expected a deferral, got {other:?}"),
    }
}
