//! Readback and delete against the fake transport.
//!
//! The point of every assertion here is that the adapter never claims more than
//! the platform confirmed: `Verified` only on a returned record, `Deleted` only
//! on an accepted delete (or a PDS that says the record is already gone), and
//! `SubmittedUnconfirmed` / `Pending` for everything else.

use xpst_provider_bluesky::fake::FakeTransport;
use xpst_provider_bluesky::{
    BlueskyCredentials, BlueskyProvider, DeleteOutcome, PublishOutcome, ReadbackState,
    TransportError,
};

const UNKNOWN_URI: &str = "at://did:plc:xpstfakeaccount/app.bsky.feed.post/3knothere";

fn provider(fake: FakeTransport) -> BlueskyProvider<FakeTransport> {
    BlueskyProvider::new(
        fake,
        BlueskyCredentials::new("bot.example", "app-password-1234"),
    )
}

#[tokio::test]
async fn a_published_post_reads_back_as_verified() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let outcome = provider.publish_text("read me back").await;
    let uri = outcome
        .uri()
        .expect("the fake returned a record URI")
        .to_owned();

    let readback = provider.readback(&uri).await;

    assert_eq!(readback.state, ReadbackState::Verified);
    assert!(readback.is_verified());
    assert_eq!(readback.post_uri, uri);
    assert!(
        readback
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("app.bsky.feed.post"),
        "{:?}",
        readback.detail
    );
}

#[tokio::test]
async fn an_absent_post_reads_back_as_not_found() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let readback = provider.readback(UNKNOWN_URI).await;

    assert_eq!(readback.state, ReadbackState::NotFound);
    assert!(!readback.is_verified());
    assert!(
        readback
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("RecordNotFound"),
        "{:?}",
        readback.detail
    );
}

#[tokio::test]
async fn a_failed_readback_is_unconfirmed_not_not_found() {
    let fake = FakeTransport::new();
    fake.seed_post(UNKNOWN_URI);
    fake.fail_get(TransportError::Network {
        detail: "connection reset".to_owned(),
    });
    let provider = provider(fake);

    let readback = provider.readback(UNKNOWN_URI).await;

    assert_eq!(
        readback.state,
        ReadbackState::SubmittedUnconfirmed,
        "a read that failed cannot prove absence"
    );
    assert!(!readback.is_verified());
    assert!(
        readback
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("connection reset"),
        "{:?}",
        readback.detail
    );
}

#[tokio::test]
async fn a_bsky_web_url_is_not_an_at_uri_and_is_never_asked_about() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let readback = provider
        .readback("https://bsky.app/profile/x/post/3kfake1")
        .await;

    assert_eq!(readback.state, ReadbackState::SubmittedUnconfirmed);
    assert_eq!(provider.transport().call_count(), 0);
}

#[tokio::test]
async fn deleting_a_post_is_confirmed_and_then_reads_back_as_not_found() {
    let fake = FakeTransport::new();
    let provider = provider(fake);
    let uri = provider
        .publish_text("soon gone")
        .await
        .uri()
        .expect("the fake returned a record URI")
        .to_owned();

    let report = provider.delete(&uri).await;

    assert_eq!(report.outcome, DeleteOutcome::Deleted);
    assert!(report.is_deleted());
    assert!(
        report.detail.is_none(),
        "a confirmed delete has nothing to explain"
    );
    assert!(!provider.transport().has_post(&uri));
    assert_eq!(provider.readback(&uri).await.state, ReadbackState::NotFound);
}

#[tokio::test]
async fn a_delete_the_pds_did_not_accept_is_pending() {
    let uri = "at://did:plc:xpstfakeaccount/app.bsky.feed.post/3kstubborn";
    let failing = FakeTransport::new();
    failing.fail_delete(TransportError::Rejected {
        status: 500,
        detail: "InternalServerError: could not apply delete".to_owned(),
    });
    failing.seed_post(uri);
    let provider = provider(failing);

    let report = provider.delete(uri).await;

    assert_eq!(report.outcome, DeleteOutcome::Pending);
    assert!(!report.is_deleted());
    assert!(
        report
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("not confirmed"),
        "{:?}",
        report.detail
    );
    assert!(
        provider.transport().has_post(uri),
        "a pending delete leaves the post alone"
    );
}

#[tokio::test]
async fn deleting_a_post_that_is_already_gone_is_reported_as_absent() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let report = provider.delete(UNKNOWN_URI).await;

    assert_eq!(report.outcome, DeleteOutcome::Deleted);
    assert!(
        report
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("already absent"),
        "{:?}",
        report.detail
    );
}

#[tokio::test]
async fn a_delete_that_could_not_ask_the_pds_is_pending() {
    let fake = FakeTransport::new();
    fake.fail_session(TransportError::CredentialsRejected {
        detail: "InvalidPassword: bad app password".to_owned(),
    });
    let provider = provider(fake);

    let report = provider.delete(UNKNOWN_URI).await;

    assert_eq!(report.outcome, DeleteOutcome::Pending);
    assert!(
        report
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("bad app password"),
        "{:?}",
        report.detail
    );
}

#[tokio::test]
async fn a_readback_that_cannot_log_in_is_unconfirmed_with_the_reason() {
    let fake = FakeTransport::new();
    fake.fail_session(TransportError::CredentialsRejected {
        detail: "InvalidIdentifier: no such handle".to_owned(),
    });
    let provider = provider(fake);

    let readback = provider.readback(UNKNOWN_URI).await;

    assert_eq!(readback.state, ReadbackState::SubmittedUnconfirmed);
    assert!(
        readback
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("no such handle"),
        "{:?}",
        readback.detail
    );
}

#[tokio::test]
async fn authenticate_reports_a_rejected_password_as_needs_reauth() {
    let fake = FakeTransport::new();
    fake.fail_session(TransportError::CredentialsRejected {
        detail: "InvalidPassword: invalid password".to_owned(),
    });
    let provider = provider(fake);

    let failure = provider.authenticate().await.expect_err("login must fail");

    assert!(
        matches!(
            failure,
            xpst_provider_bluesky::AuthFailure::NeedsReauth { .. }
        ),
        "{failure:?}"
    );
}

#[tokio::test]
async fn an_upstream_failure_is_unconfirmed_and_a_readback_settles_it() {
    let fake = FakeTransport::new();
    // The fake answers 5xx without writing: exactly the shape a client cannot
    // tell apart from "the commit was persisted and the answer was lost".
    fake.fail_create(TransportError::Upstream {
        status: 503,
        detail: "ServiceUnavailable: try again".to_owned(),
    });
    let provider = provider(fake);

    let outcome = provider.publish_text("will not land").await;

    assert!(!outcome.is_published(), "{outcome:?}");
    assert!(
        outcome.is_unconfirmed(),
        "a 5xx is not proof that nothing was written: {outcome:?}"
    );
    assert!(
        !matches!(
            outcome,
            PublishOutcome::Failed {
                retryable: true,
                ..
            }
        ),
        "reporting it as a retryable failure is what duplicates the post: {outcome:?}"
    );

    let candidate = outcome
        .unconfirmed_ids()
        .and_then(|ids| ids.first().copied().map(str::to_owned))
        .expect("an unconfirmed write names the record it would have created");
    assert!(candidate.contains("/app.bsky.feed.post/"));

    // Reconciled the way a caller should: ask the platform about that exact
    // record. Nothing is there, so the request certainly did not land and a retry
    // is safe — and the answer is a fact rather than an assumption.
    assert_eq!(
        provider.readback(&candidate).await.state,
        ReadbackState::NotFound
    );
    assert_eq!(provider.transport().record_count(), 0);
}
