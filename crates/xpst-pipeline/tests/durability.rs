//! Durability: the plan record survives a restart, a second process and a
//! killed owner.
//!
//! These tests exist because the earlier suite proved idempotence *in memory*:
//! it called `publish()` twice in one process against one `ConfirmedPlan`, which
//! would pass with no persistence at all. What is proven here is the claim the
//! engine actually makes:
//!
//! * a plan confirmed and partially published in one process is resumed by a
//!   **second OS process** (a child of this test binary), which publishes only
//!   the destinations that did not finish;
//! * a lease left behind by a killed owner blocks other owners while it is live
//!   and is reclaimed after it expires — never stolen early, never left forever;
//! * the outcome counts in the persisted record equal the counts in the report;
//! * re-confirming the same plan resumes it instead of resetting it to
//!   `outcome: None, attempts: 0`.
//!
//! Everything runs against the in-crate fakes: no socket, no clock, no
//! credentials.

use std::path::PathBuf;
use std::process::Command;

use xpst_core::content::ContentType;
use xpst_core::plan::{DestinationState, DestinationVerdict, RoutePlan};

use xpst_pipeline::fake::{FakeGate, FakePublisher};
use xpst_pipeline::{
    confirm, ConfirmedPlan, DestinationOutcome, JournalError, JournalSink, Pipeline, PlanJournal,
    ProviderOutcome, PublicationContent, PublishContext, PublishError, PublisherSet,
    ReconcileOutcome, StateSink, DEFAULT_LEASE_TTL_MS,
};

const NOW: i64 = 1_700_000_000_000;
const OWNER: &str = "run-owner";

fn temp_dir(label: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "xpst-pipeline-{label}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("create temp dir");
    path
}

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

fn route(platforms: &[&str]) -> RoutePlan {
    RoutePlan {
        content_type: Some(ContentType::Text),
        accepted: platforms.iter().map(|item| (*item).to_owned()).collect(),
        destinations: platforms.iter().map(|item| text_verdict(item)).collect(),
        request_blockers: Vec::new(),
    }
}

fn content() -> PublicationContent {
    PublicationContent::new("fic-content-hash-durable-0001", "durable from the pipeline")
}

fn confirmed(platforms: &[&str]) -> ConfirmedPlan {
    confirm(&route(platforms), &content()).expect("confirm")
}

/// Confirm the plan durably, exactly as a caller must: the returned plan is the
/// stored one, so a re-confirm resumes instead of resetting.
fn confirmed_in(journal: &PlanJournal, platforms: &[&str], now_ms: i64) -> ConfirmedPlan {
    journal
        .confirm(&confirmed(platforms), now_ms)
        .expect("the plan is stored")
}

fn report_json(counts: &xpst_pipeline::OutcomeCounts) -> serde_json::Value {
    serde_json::json!({
        "published": counts.published,
        "skipped_duplicate": counts.skipped_duplicate,
        "deferred": counts.deferred,
        "refused": counts.refused,
        "failed_retryable": counts.failed_retryable,
        "failed_permanent": counts.failed_permanent,
        "unconfirmed": counts.unconfirmed,
        "pending": counts.pending,
        "total": counts.total(),
    })
}

// ── the second process ────────────────────────────────────────────────────

/// Child-process mode: a separate OS process resuming the stored plan.
///
/// Returns immediately when the environment does not name a child run, so this
/// "test" is inert in the parent.
#[tokio::test]
async fn second_process_child_resumes() {
    let Ok(dir) = std::env::var("XPST_PLAN_CHILD_DIR") else {
        return;
    };
    let plan_id = std::env::var("XPST_PLAN_CHILD_PLAN").expect("child plan id");
    let now_ms: i64 = std::env::var("XPST_PLAN_CHILD_NOW")
        .expect("child clock")
        .parse()
        .expect("child clock is milliseconds");
    let out_path = std::env::var("XPST_PLAN_CHILD_OUT").expect("child output path");

    let journal = PlanJournal::open(dir.clone());
    let mut plan = journal
        .load(&plan_id)
        .expect("the child can read the record")
        .expect("the parent stored a record before spawning");
    let outstanding: Vec<String> = plan
        .outstanding(now_ms)
        .into_iter()
        .map(str::to_owned)
        .collect();
    assert_eq!(
        outstanding,
        vec!["beta".to_owned()],
        "the child must see exactly the destination the parent left unfinished"
    );
    assert!(
        plan.destination("alpha")
            .is_some_and(xpst_pipeline::DestinationRecord::is_terminal),
        "alpha was published by the other process and must stay settled"
    );

    let mut gate = FakeGate::new(xpst_pipeline::fake::CallLog::new());
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::publishing("beta");
    let publishers = PublisherSet::new().with(&alpha).with(&beta);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut sink = JournalSink::new(PlanJournal::open(dir));
    let report = pipeline
        .publish(
            &mut plan,
            PublishContext {
                now_ms,
                account: "acct-1",
                owner: "child-process",
                state: Some(&mut sink),
            },
        )
        .await
        .expect("the child publishes what is left");

    assert_eq!(alpha.publish_calls(), 0, "alpha must not be re-published");
    assert_eq!(beta.publish_calls(), 1, "beta is the one thing left to do");

    let record = journal
        .load(&plan_id)
        .expect("the child can re-read the record")
        .expect("the record is still there");
    let stamps = journal
        .timestamps(&plan_id)
        .expect("the child can read the timestamps")
        .expect("the record is still there");
    let document = serde_json::json!({
        "pid": std::process::id(),
        "outstanding_before": outstanding,
        "alpha_calls": alpha.publish_calls(),
        "beta_calls": beta.publish_calls(),
        "report_counts": report_json(&report.counts()),
        "stored_counts": report_json(&record.counts()),
        "stored_complete": record.is_complete(),
        "beta_updated_at_ms": stamps.per_destination.get("beta"),
        "alpha_updated_at_ms": stamps.per_destination.get("alpha"),
    });
    std::fs::write(out_path, serde_json::to_string(&document).expect("json"))
        .expect("write report");
}

#[tokio::test]
async fn a_second_process_publishes_only_what_the_first_process_left() {
    let dir = temp_dir("second-process");
    let journal = PlanJournal::open(dir.clone());
    let mut plan = confirmed_in(&journal, &["alpha", "beta"], NOW);
    let plan_id = plan.plan_id().to_owned();

    let mut gate = FakeGate::new(xpst_pipeline::fake::CallLog::new());
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::scripted("beta").then(ProviderOutcome::Deferred {
        resume_after_ms: NOW + 600_000,
        detail: "beta is outside its posting window".to_owned(),
    });
    let publishers = PublisherSet::new().with(&alpha).with(&beta);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut sink = JournalSink::new(PlanJournal::open(dir.clone()));
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
        .expect("the first pass publishes alpha and defers beta");

    assert_eq!(report.counts().published, 1);
    assert_eq!(report.counts().deferred, 1);
    assert_eq!(
        beta.publish_calls(),
        1,
        "the deferral came from the provider"
    );

    // The first process is done — nothing else in this test holds the plan.
    drop(pipeline);
    drop(sink);
    drop(plan);

    let out = dir.join("child-report.json");
    let exe = std::env::current_exe().expect("current test binary");
    let output = Command::new(&exe)
        .args([
            "--exact",
            "second_process_child_resumes",
            "--nocapture",
            "--test-threads=1",
        ])
        .env("XPST_PLAN_CHILD_DIR", &dir)
        .env("XPST_PLAN_CHILD_PLAN", &plan_id)
        .env("XPST_PLAN_CHILD_NOW", (NOW + 900_000).to_string())
        .env("XPST_PLAN_CHILD_OUT", &out)
        .output()
        .expect("spawn the second process");
    assert!(
        output.status.success(),
        "the second process failed: {}\n--- stdout ---\n{}\n--- stderr ---\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let text = std::fs::read_to_string(&out).expect("the child wrote its report");
    let child: serde_json::Value = serde_json::from_str(&text).expect("child report is JSON");
    assert_ne!(
        child["pid"].as_u64(),
        Some(u64::from(std::process::id())),
        "the resume has to happen in a different OS process"
    );
    assert_eq!(child["beta_calls"].as_u64(), Some(1));
    assert_eq!(child["alpha_calls"].as_u64(), Some(0));
    assert_eq!(
        child["report_counts"]["published"].as_u64(),
        Some(2),
        "the child's report counts the destination the parent published too: it is one plan"
    );
    assert_eq!(
        child["report_counts"], child["stored_counts"],
        "the persisted counts and the reported counts must be the same numbers"
    );
    assert_eq!(child["stored_complete"].as_bool(), Some(true));
    assert_eq!(
        child["alpha_updated_at_ms"].as_i64(),
        Some(NOW),
        "the child did not touch alpha, so its timestamp is still the parent's"
    );
    assert_eq!(
        child["beta_updated_at_ms"].as_i64(),
        Some(NOW + 900_000),
        "beta's row is stamped with the clock of the process that changed it"
    );

    // And the parent can see the child's work, because it is on disk.
    let final_record = journal
        .load(&plan_id)
        .expect("reload after the second process")
        .expect("the record survives");
    assert_eq!(final_record.counts().published, 2);
    assert!(final_record.is_complete());
    assert_eq!(
        final_record
            .destination("beta")
            .map(xpst_pipeline::DestinationRecord::attempts),
        Some(2),
        "one provider call from each process — the deferral was a real call, not a skip"
    );
}

// ── leases ────────────────────────────────────────────────────────────────

#[tokio::test]
async fn a_lease_from_a_killed_owner_blocks_until_it_expires_then_is_reclaimed() {
    let dir = temp_dir("lease");
    let journal = PlanJournal::open(dir.clone());
    let mut plan = confirmed_in(&journal, &["alpha"], NOW);
    let plan_id = plan.plan_id().to_owned();

    // An owner takes the attempt and then "dies": the claim is on disk, and no
    // live process holds it any more.
    plan.claim("alpha", "killed-owner", NOW).expect("the claim");
    journal
        .record_destination(&plan_id, NOW, &plan.destinations()[0])
        .expect("the claim reaches the record");
    drop(plan);

    let mut reloaded = journal
        .load(&plan_id)
        .expect("reload")
        .expect("the record survives the owner");
    assert_eq!(reloaded.lease_owner("alpha"), Some("killed-owner"));
    let expiry = reloaded.destinations()[0]
        .lease()
        .expect("the lease is stored")
        .expires_at_ms;
    assert_eq!(expiry, NOW + DEFAULT_LEASE_TTL_MS);
    assert_eq!(
        reloaded
            .destination("alpha")
            .and_then(|record| record.outcome())
            .map(DestinationOutcome::as_str),
        Some("unconfirmed"),
        "a claimed attempt with no recorded outcome may have landed, so it reads as unconfirmed"
    );

    // While the lease is live, a different owner is refused — before any gate
    // or provider call.
    let mut gate = FakeGate::new(xpst_pipeline::fake::CallLog::new());
    let alpha = FakePublisher::scripted("alpha")
        // The takeover must prove the ambiguous attempt produced nothing before
        // it is allowed to publish again.
        .reconcile_once(ReconcileOutcome::Absent)
        .then(ProviderOutcome::Published {
            post_id: "alpha-post-takeover".to_owned(),
            url: None,
        });
    let publishers = PublisherSet::new().with(&alpha);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut sink = JournalSink::new(PlanJournal::open(dir.clone()));
    let error = pipeline
        .publish(
            &mut reloaded,
            PublishContext {
                now_ms: expiry - 1,
                account: "acct-1",
                owner: "live-owner",
                state: Some(&mut sink),
            },
        )
        .await
        .expect_err("a live lease is not stolen");
    assert!(
        matches!(error, PublishError::DestinationLeased { ref owner, attempt_no, .. } if owner == "killed-owner" && attempt_no == 1),
        "{error:?}"
    );
    assert_eq!(alpha.publish_calls(), 0);
    assert_eq!(
        journal
            .load(&plan_id)
            .expect("reload")
            .expect("record")
            .lease_owner("alpha"),
        Some("killed-owner"),
        "the refused attempt must not have rewritten the lease"
    );

    // After the expiry the destination is claimable again, because its owner is
    // gone: the plan is not blocked forever.
    let report = pipeline
        .publish(
            &mut reloaded,
            PublishContext {
                now_ms: expiry,
                account: "acct-1",
                owner: "live-owner",
                state: Some(&mut sink),
            },
        )
        .await
        .expect("an expired lease is reclaimed");
    assert_eq!(
        alpha.reconcile_calls(),
        1,
        "the ambiguous attempt was read back before anything was published"
    );
    assert_eq!(alpha.publish_calls(), 1);
    assert_eq!(report.counts().published, 1);
    let stored = journal.load(&plan_id).expect("reload").expect("record");
    assert_eq!(stored.counts().published, 1);
    assert_eq!(
        stored.destination("alpha").map(|record| record.attempts()),
        Some(1),
        "one provider call: the dead owner's claim never reached one, and attempts counts calls"
    );
    assert_eq!(
        stored.lease_owner("alpha"),
        None,
        "a settled destination holds no lease"
    );
}

// ── the record is the report ──────────────────────────────────────────────

#[tokio::test]
async fn the_persisted_record_counts_match_the_report() {
    let dir = temp_dir("counts");
    let journal = PlanJournal::open(dir.clone());
    let mut plan = confirmed_in(&journal, &["alpha", "beta", "gamma"], NOW);
    let plan_id = plan.plan_id().to_owned();

    let mut gate = FakeGate::new(xpst_pipeline::fake::CallLog::new());
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::scripted("beta").then(ProviderOutcome::permanent(
        "MEDIA_REJECTED",
        "the platform refused the file",
    ));
    let gamma = FakePublisher::scripted("gamma").then(ProviderOutcome::Deferred {
        resume_after_ms: NOW + 3_600_000,
        detail: "not yet".to_owned(),
    });
    let publishers = PublisherSet::new().with(&alpha).with(&beta).with(&gamma);
    let mut pipeline =
        Pipeline::new(&mut gate, publishers).with_budget(xpst_pipeline::RetryBudget::new(1));
    let mut sink = JournalSink::new(PlanJournal::open(dir.clone()));
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

    let stored = journal
        .load(&plan_id)
        .expect("reload")
        .expect("the record exists");
    assert_eq!(
        report.counts(),
        stored.counts(),
        "what is on disk must be what was reported"
    );
    assert_eq!(stored.counts().published, 1);
    assert_eq!(stored.counts().failed_permanent, 1);
    assert_eq!(stored.counts().deferred, 1);
    assert_eq!(stored.counts().total(), 3);
    assert_eq!(
        stored.destination("alpha").map(|record| record.attempts()),
        Some(1)
    );
    assert_eq!(
        stored.destination("beta").map(|record| record.attempts()),
        Some(1)
    );
    match stored
        .destination("gamma")
        .and_then(|record| record.outcome())
    {
        Some(DestinationOutcome::Deferred(deferred)) => {
            assert_eq!(deferred.resume_after_ms, NOW + 3_600_000);
        }
        other => panic!("expected the stored deferral, got {other:?}"),
    }
    // The intent is stored too, so a resume does not have to re-derive it from
    // configuration that may have changed.
    let intent = stored
        .destination("alpha")
        .and_then(|record| record.intent())
        .expect("the frozen intent survives");
    assert_eq!(intent.caption, "durable from the pipeline");
    assert_eq!(intent.content_type, ContentType::Text);
    assert_eq!(intent.key.as_str(), "alpha:fic-content-hash-durable-0001");
}

#[tokio::test]
async fn confirming_the_same_plan_again_resumes_it_instead_of_resetting_it() {
    let dir = temp_dir("reconfirm");
    let journal = PlanJournal::open(dir.clone());
    let mut plan = confirmed_in(&journal, &["alpha", "beta"], NOW);
    let plan_id = plan.plan_id().to_owned();

    let mut gate = FakeGate::new(xpst_pipeline::fake::CallLog::new());
    let alpha = FakePublisher::publishing("alpha");
    let beta = FakePublisher::scripted("beta").then(ProviderOutcome::Deferred {
        resume_after_ms: NOW + 600_000,
        detail: "later".to_owned(),
    });
    let publishers = PublisherSet::new().with(&alpha).with(&beta);
    let mut pipeline = Pipeline::new(&mut gate, publishers);
    let mut sink = JournalSink::new(PlanJournal::open(dir.clone()));
    pipeline
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
        .expect("first pass");
    drop(plan);

    // A restart re-confirms the same content for the same destinations. The
    // stored record must win: this is the defect that made a restart republish
    // everything.
    let after_restart = journal
        .confirm(&confirmed(&["alpha", "beta"]), NOW + 1_000)
        .expect("re-confirm resumes");
    assert_eq!(after_restart.plan_id(), plan_id);
    assert_eq!(
        after_restart
            .destination("alpha")
            .and_then(|record| record.outcome())
            .map(DestinationOutcome::as_str),
        Some("published"),
        "a settled destination must not be reset to pending by re-confirming"
    );
    assert_eq!(
        after_restart
            .destination("alpha")
            .map(|record| record.attempts()),
        Some(1),
        "the attempt count is history, not a default"
    );
    assert_eq!(
        after_restart.outstanding(NOW + 600_000),
        vec!["beta"],
        "beta is still waiting out its window; it is not lost and not re-published early"
    );
    assert!(
        after_restart.outstanding(NOW + 1_000).is_empty(),
        "and it is not actionable until the window it recorded has passed"
    );
    assert_eq!(after_restart.counts().pending, 0, "nothing was reset");
}

#[test]
fn a_damaged_record_fails_closed_and_is_never_reset() {
    let dir = temp_dir("corrupt");
    let journal = PlanJournal::open(dir.clone());
    let plan = confirmed_in(&journal, &["alpha"], NOW);
    let path = journal.path_for(plan.plan_id()).expect("plan path");
    let damaged = b"{\"version\": 1, \"plan_id\": ".to_vec();
    std::fs::write(&path, &damaged).expect("damage the record");

    let error = journal
        .load(plan.plan_id())
        .expect_err("a truncated record is an error");
    assert!(
        matches!(error, JournalError::Corrupt { .. }),
        "expected a corruption error, got {error:?}"
    );
    assert_eq!(
        std::fs::read(&path).expect("the file is still there"),
        damaged,
        "a damaged record must be reported, not emptied"
    );

    // And it is not replaced by a fresh confirmation either.
    let error = journal
        .confirm(&confirmed(&["alpha"]), NOW + 1_000)
        .expect_err("confirming over a damaged record is refused");
    assert!(matches!(error, JournalError::Corrupt { .. }), "{error:?}");
    assert_eq!(
        std::fs::read(&path).expect("the file is still there"),
        damaged
    );
}

#[test]
fn an_erroring_sink_is_reported_on_the_destination_and_never_rewrites_the_outcome() {
    struct FailingSink;
    impl StateSink for FailingSink {
        fn record(
            &mut self,
            _plan_id: &str,
            _now_ms: i64,
            _record: &xpst_pipeline::DestinationRecord,
        ) -> Result<(), xpst_pipeline::StateWriteError> {
            Err(xpst_pipeline::StateWriteError::new("disk full"))
        }
    }

    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("runtime");
    runtime.block_on(async {
        let mut gate = FakeGate::new(xpst_pipeline::fake::CallLog::new());
        let alpha = FakePublisher::publishing("alpha");
        let publishers = PublisherSet::new().with(&alpha);
        let mut pipeline = Pipeline::new(&mut gate, publishers);
        let mut plan = confirmed(&["alpha"]);
        let mut sink = FailingSink;
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
            .expect("the publish itself succeeded");
        assert_eq!(report.counts().published, 1);
        assert_eq!(
            report
                .entry("alpha")
                .and_then(|entry| entry.state_write_error.as_deref()),
            Some("disk full")
        );
    });
}

#[test]
fn a_freshly_confirmed_plan_is_pending_not_published() {
    let dir = temp_dir("fresh");
    let journal = PlanJournal::open(dir);
    let plan = confirmed_in(&journal, &["alpha"], NOW);
    let stored = journal.load(plan.plan_id()).expect("load").expect("record");
    assert_eq!(stored.counts().pending, 1);
    assert_eq!(stored.counts().published, 0);
    assert_eq!(
        stored.destination("alpha").map(|record| record.attempts()),
        Some(0),
        "confirming is not attempting"
    );
}
