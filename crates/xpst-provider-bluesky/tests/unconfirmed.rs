//! The duplicate-post question, asked of the Bluesky adapter.
//!
//! What this file is guarding: a lost response used to be reported as a
//! retryable failure, and because `createRecord` let the PDS choose the record
//! key, the retry wrote a *second* record. Both halves of that are asserted here
//! — the key is deterministic, so a retry rewrites the same record; and a request
//! whose answer was lost is reported as [`PublishOutcome::Unconfirmed`] with the
//! record key it used, so "did it land?" has an exact answer.
//!
//! The fake models the platform rather than what would be convenient: a create at
//! an existing key rewrites that record (the PDS does not reject it), and record
//! CIDs are content-addressed, so re-writing different bytes at the same key
//! changes the CID while leaving the record count unchanged.

use xpst_provider_bluesky::fake::FakeTransport;
use xpst_provider_bluesky::{
    BlueskyCredentials, BlueskyProvider, Operation, Platform, PublishOutcome, ReadbackState,
    Reconciliation, TransportError,
};

fn provider(fake: FakeTransport) -> BlueskyProvider<FakeTransport> {
    BlueskyProvider::new(
        fake,
        BlueskyCredentials::new("bot.example", "app-password-1234"),
    )
}

fn create_calls(provider: &BlueskyProvider<FakeTransport>) -> usize {
    provider
        .transport()
        .calls()
        .into_iter()
        .filter(|call| call.nsid() == "com.atproto.repo.createRecord")
        .count()
}

#[tokio::test]
async fn retrying_the_same_content_publishes_one_record() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let first = provider.publish_text("the same words").await;
    let PublishOutcome::Published(first) = first else {
        panic!("expected a published post, got {first:?}");
    };

    // The retry the old contract produced: same content, sent again.
    let second = provider.publish_text("the same words").await;
    let PublishOutcome::Published(second) = second else {
        panic!("expected the retry to be published, got {second:?}");
    };

    assert_eq!(
        first.uri, second.uri,
        "a retry of the same content must address the same record"
    );
    assert_eq!(
        provider.transport().record_count(),
        1,
        "one post, not two — this is the defect the audit found"
    );
    assert_eq!(
        provider.transport().duplicate_rkey_writes(),
        1,
        "the retry must actually have re-sent the write, not been skipped locally"
    );
    assert_eq!(create_calls(&provider), 2, "two attempts, one record");

    // Both requests named the same key, and that key is the record's key.
    let drafts = provider.transport().drafts();
    assert_eq!(drafts.len(), 2);
    assert_eq!(drafts[0].rkey, drafts[1].rkey);
    assert_eq!(
        first.uri,
        format!(
            "at://did:plc:xpstfakeaccount/app.bsky.feed.post/{}",
            drafts[0].rkey
        )
    );
}

#[tokio::test]
async fn different_content_is_still_a_different_record() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let first = provider.publish_text("first post").await;
    let second = provider.publish_text("second post").await;

    assert!(first.is_published() && second.is_published());
    assert_ne!(
        first.uri(),
        second.uri(),
        "determinism must not collapse two different posts into one"
    );
    assert_eq!(provider.transport().record_count(), 2);
    assert_eq!(provider.transport().duplicate_rkey_writes(), 0);
}

#[tokio::test]
async fn a_lost_response_is_unconfirmed_and_names_the_record_it_wrote() {
    let fake = FakeTransport::new();
    // The write lands; the answer does not come back.
    fake.lose_create_response(TransportError::Network {
        detail: "connection reset by peer".to_owned(),
    });
    let provider = provider(fake);

    let outcome = provider.publish_text("this one landed").await;

    assert!(!outcome.is_published(), "nothing confirmed it: {outcome:?}");
    assert!(
        !matches!(outcome, PublishOutcome::Failed { .. }),
        "it is not a failure either: {outcome:?}"
    );
    let PublishOutcome::Unconfirmed(unconfirmed) = &outcome else {
        panic!("a lost response must be reported as unconfirmed, got {outcome:?}");
    };
    assert_eq!(unconfirmed.platform, Platform::Bluesky);
    assert_eq!(unconfirmed.operation, Operation::CreateRecord);

    // The platform does hold the record, and the adapter named it exactly.
    assert_eq!(provider.transport().record_count(), 1);
    let candidate = unconfirmed
        .primary_candidate()
        .expect("an unconfirmed write names a candidate");
    assert!(
        provider.transport().has_post(candidate),
        "the candidate id must be the record the request created: {candidate}"
    );
    assert!(matches!(
        unconfirmed.reconciliation,
        Reconciliation::ReadRecord { .. }
    ));
    assert!(
        unconfirmed.detail.contains("connection reset"),
        "the platform's own words must survive: {}",
        unconfirmed.detail
    );

    // And the reconciliation the type promises really answers it.
    assert_eq!(
        provider.readback(candidate).await.state,
        ReadbackState::Verified
    );
}

#[tokio::test]
async fn an_unconfirmed_post_is_not_duplicated_by_the_next_attempt() {
    let fake = FakeTransport::new();
    fake.lose_create_response(TransportError::Network {
        detail: "connection reset by peer".to_owned(),
    });
    let provider = provider(fake);

    let outcome = provider.publish_text("posted once").await;
    let PublishOutcome::Unconfirmed(unconfirmed) = &outcome else {
        panic!("expected unconfirmed, got {outcome:?}");
    };
    let candidate = unconfirmed
        .primary_candidate()
        .unwrap_or_default()
        .to_owned();

    // The network recovers; the retry an operator would trigger goes out.
    provider.transport().clear_failures();
    let retried = provider.publish_text("posted once").await;
    assert_eq!(retried.uri(), Some(candidate.as_str()));
    assert_eq!(
        provider.transport().record_count(),
        1,
        "the retry must not create a second post"
    );
    assert_eq!(provider.transport().duplicate_rkey_writes(), 1);
}

#[tokio::test]
async fn a_rejected_record_is_a_failure_because_nothing_was_written() {
    let fake = FakeTransport::new();
    fake.fail_create(TransportError::Rejected {
        status: 400,
        detail: "InvalidRequest: record is not lexicon-shaped".to_owned(),
    });
    let provider = provider(fake);

    let outcome = provider.publish_text("rejected by the PDS").await;

    let PublishOutcome::Failed {
        retryable, detail, ..
    } = outcome
    else {
        panic!("a 400 rejection is a definite answer, got {outcome:?}");
    };
    assert!(
        !retryable,
        "an invalid record will not become valid on retry"
    );
    assert!(detail.contains("InvalidRequest"), "{detail}");
    assert_eq!(provider.transport().record_count(), 0);
}
