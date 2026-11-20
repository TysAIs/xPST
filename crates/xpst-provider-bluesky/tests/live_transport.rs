//! The Bluesky publish path through the *live* transport's mapping.
//!
//! `tests/publish.rs` drives the provider against [`FakeTransport`]. This file
//! drives it through [`xpst_provider_bluesky::atrium::AtriumTransport`] — the
//! XRPC client that will face a PDS — with a recording adapter underneath, so
//! what is asserted is the request XRPC actually produced: NSID, method,
//! bearer token and record body.
//!
//! Nothing here opens a socket. The adapter is the only thing that could, and it
//! is a script.

use xpst_provider::http::fake::{reply, RecordingAdapter};
use xpst_provider::http::{HttpFailure, HttpFailureKind, HttpMethod, PreparedResponse};
use xpst_provider_bluesky::atrium::AtriumTransport;
use xpst_provider_bluesky::{BlueskyCredentials, BlueskyProvider, PublishOutcome, PublishedKind};

/// A real CIDv1, because the adapter re-encodes the reference the PDS returns
/// and a made-up string would fail that path instead of exercising it.
const RECORD_CID: &str = "bafkreibme22gw2h7y2h7tg2fhqotaqjucnbc24deqo72b6mkl2egezxhvy";

fn session_reply() -> PreparedResponse {
    reply(
        200,
        br#"{"accessJwt":"jwt-SECRET-TOKEN","refreshJwt":"refresh","handle":"bot.example","did":"did:plc:live"}"#
            .to_vec(),
    )
    .with_header("Content-Type", "application/json")
}

fn created_record_reply(uri: &str) -> PreparedResponse {
    reply(
        200,
        format!(r#"{{"uri":"{uri}","cid":"{RECORD_CID}"}}"#).into_bytes(),
    )
    .with_header("Content-Type", "application/json")
}

#[tokio::test]
async fn publishing_a_post_is_a_bearer_authenticated_create_record() {
    let adapter = RecordingAdapter::scripted(vec![
        Ok(session_reply()),
        Ok(created_record_reply(
            "at://did:plc:live/app.bsky.feed.post/3kabcdefghijk",
        )),
    ]);
    let transport = AtriumTransport::with_adapter("https://bsky.social", adapter);
    let provider = BlueskyProvider::new(
        transport,
        BlueskyCredentials::new("bot.example", "app-password-1234"),
    );

    let outcome = provider.publish_text("hello from the live transport").await;

    let PublishOutcome::Published(post) = outcome else {
        panic!("expected a published post, got {outcome:?}");
    };
    assert_eq!(post.kind, PublishedKind::Text);
    assert_eq!(
        post.uri,
        "at://did:plc:live/app.bsky.feed.post/3kabcdefghijk"
    );

    let sent = provider.transport().adapter().requests();
    assert_eq!(sent.len(), 2, "createSession then createRecord");

    let session = &sent[0];
    assert_eq!(session.method, HttpMethod::Post);
    assert_eq!(
        session.url,
        "https://bsky.social/xrpc/com.atproto.server.createSession"
    );
    let session_body: serde_json::Value =
        serde_json::from_slice(&session.body).expect("the session body is JSON");
    assert_eq!(session_body["identifier"], "bot.example");
    assert_eq!(session_body["password"], "app-password-1234");
    assert_eq!(
        session.header("authorization"),
        None,
        "createSession is the one call that cannot be authenticated"
    );

    let record = &sent[1];
    assert_eq!(record.method, HttpMethod::Post);
    assert_eq!(
        record.url, "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        "the NSID is in the URL, not in the body"
    );
    assert_eq!(
        record.header("authorization"),
        Some("Bearer jwt-SECRET-TOKEN"),
        "the access JWT is the Authorization header"
    );
    let body: serde_json::Value =
        serde_json::from_slice(&record.body).expect("the record body is JSON");
    assert_eq!(body["repo"], "did:plc:live");
    assert_eq!(body["collection"], "app.bsky.feed.post");
    assert_eq!(body["record"]["$type"], "app.bsky.feed.post");
    assert_eq!(body["record"]["text"], "hello from the live transport");
    assert!(
        body["rkey"]
            .as_str()
            .is_some_and(xpst_provider_bluesky::is_tid),
        "the write must name its own record key: {}",
        body["rkey"]
    );
}

#[tokio::test]
async fn a_401_from_the_pds_is_needs_reauth_and_not_a_retry() {
    let adapter = RecordingAdapter::scripted(vec![
        Ok(session_reply()),
        Ok(reply(
            401,
            br#"{"error":"InvalidToken","message":"Token has been revoked"}"#.to_vec(),
        )
        .with_header("Content-Type", "application/json")),
    ]);
    let transport = AtriumTransport::with_adapter("https://bsky.social", adapter);
    let provider = BlueskyProvider::new(
        transport,
        BlueskyCredentials::new("bot.example", "app-password-1234"),
    );

    let outcome = provider.publish_text("hello again").await;

    let PublishOutcome::NeedsReauth { .. } = outcome else {
        panic!("a 401 must ask for a human, got {outcome:?}");
    };
    // Exactly two requests: the rejected write is not re-sent by the transport.
    assert_eq!(provider.transport().adapter().requests().len(), 2);
}

#[tokio::test]
async fn a_failed_exchange_never_echoes_the_access_jwt() {
    let adapter = RecordingAdapter::scripted(vec![
        Ok(session_reply()),
        Err(HttpFailure::new(
            HttpFailureKind::Connect,
            "connection reset sending https://bsky.social/xrpc/com.atproto.repo.createRecord",
        )),
    ]);
    let transport = AtriumTransport::with_adapter("https://bsky.social", adapter);
    let provider = BlueskyProvider::new(
        transport,
        BlueskyCredentials::new("bot.example", "app-password-1234"),
    );

    let outcome = provider.publish_text("this answer is lost").await;

    let rendered = format!("{outcome:?}");
    assert!(!rendered.contains("jwt-SECRET-TOKEN"), "{rendered}");
    assert!(!rendered.contains("app-password-1234"), "{rendered}");
    assert!(
        outcome.is_unconfirmed(),
        "a lost answer is unconfirmed, not failed: {outcome:?}"
    );
}

#[tokio::test]
async fn a_read_back_is_a_get_with_the_bearer_token() {
    let adapter = RecordingAdapter::scripted(vec![
        Ok(session_reply()),
        Ok(reply(
            200,
            format!(
                r#"{{"uri":"at://did:plc:live/app.bsky.feed.post/3kabcdefghijk","cid":"{RECORD_CID}","value":{{"$type":"app.bsky.feed.post","text":"hi","createdAt":"2026-11-17T12:00:00.000Z"}}}}"#
            )
            .into_bytes(),
        )
        .with_header("Content-Type", "application/json")),
    ]);
    let transport = AtriumTransport::with_adapter("https://bsky.social", adapter);
    let provider = BlueskyProvider::new(
        transport,
        BlueskyCredentials::new("bot.example", "app-password-1234"),
    );

    let readback = provider
        .readback("at://did:plc:live/app.bsky.feed.post/3kabcdefghijk")
        .await;
    assert!(readback.is_verified(), "{readback:?}");

    let sent = provider.transport().adapter().requests();
    let get = sent.last().expect("a request");
    assert_eq!(get.method, HttpMethod::Get);
    assert!(
        get.url
            .starts_with("https://bsky.social/xrpc/com.atproto.repo.getRecord?"),
        "{}",
        get.url
    );
    assert!(
        get.url.contains("collection=app.bsky.feed.post"),
        "{}",
        get.url
    );
    assert_eq!(get.header("authorization"), Some("Bearer jwt-SECRET-TOKEN"));
}
