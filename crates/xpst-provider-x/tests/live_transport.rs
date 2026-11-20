//! The publishing path through the *live* transport's mapping.
//!
//! `tests/publish_path.rs` drives the protocol against a fake transport. This
//! file drives the same publisher through [`xpst_provider_x::HttpTransport`],
//! which is the code that will face the wire, and asserts what it actually
//! produced: method, URL, signed `Authorization`, `Content-Type` and body.
//!
//! Nothing here opens a socket — the exchange is performed by a recording
//! adapter, so there is no host to reach and no way to reach one.

use xpst_provider::http::fake::{reply, RecordingAdapter};
use xpst_provider::http::PreparedResponse;
use xpst_provider::http::{HttpFailure, HttpFailureKind, HttpMethod};
use xpst_provider_x::{
    Credentials, Endpoints, Media, PublishRequest, Response, XError, XPublisher,
};

fn credentials() -> Credentials {
    Credentials::new("ck-test", "cs-test", "at-test", "as-test")
}

fn publisher<A: xpst_provider::http::BlockingHttpAdapter>(
    transport: xpst_provider_x::HttpTransport<A>,
) -> XPublisher<xpst_provider_x::HttpTransport<A>> {
    XPublisher::new(transport, credentials()).with_endpoints(Endpoints::new(
        "https://api.test/2",
        "https://upload.test/1.1",
    ))
}

#[test]
fn a_text_post_is_a_signed_json_post_to_the_tweets_endpoint() {
    let adapter = RecordingAdapter::scripted(vec![Ok(reply(
        201,
        br#"{"data":{"id":"1790000000000000001","text":"hello"}}"#.to_vec(),
    )
    .with_header("Content-Type", "application/json"))]);
    let mut x = publisher(xpst_provider_x::HttpTransport::new(adapter));

    let published = x
        .publish(&PublishRequest::text(
            "acct-a",
            "hello from the live transport",
        ))
        .expect("the script answers");

    assert_eq!(published.post_id, "1790000000000000001");

    let sent = x.into_transport().adapter().requests();
    assert_eq!(sent.len(), 1, "a text post is one request");
    assert_eq!(sent[0].method, HttpMethod::Post);
    assert_eq!(sent[0].url, "https://api.test/2/tweets");
    let authorization = sent[0]
        .header("authorization")
        .expect("the request must be signed");
    assert!(authorization.starts_with("OAuth "), "{authorization}");
    assert!(
        authorization.contains("oauth_signature"),
        "an unsigned request is a 401 at the platform: {authorization}"
    );
    assert_eq!(sent[0].header("content-type"), Some("application/json"));
    let body: serde_json::Value = serde_json::from_slice(&sent[0].body).expect("the body is JSON");
    assert_eq!(body["text"], "hello from the live transport");
}

#[test]
fn the_media_protocol_survives_the_mapping_including_the_204_append() {
    let adapter = RecordingAdapter::scripted(vec![
        // INIT: 202 with the media id.
        Ok(reply(
            202,
            br#"{"media_id":123,"media_id_string":"123","expires_after_secs":86400}"#.to_vec(),
        )),
        // APPEND: 204 with no body at all. Anything else here would be a lie
        // about what the endpoint does.
        Ok(PreparedResponse::new(204)),
        // FINALIZE: processing already finished.
        Ok(reply(
            200,
            br#"{"media_id_string":"123","processing_info":{"state":"succeeded"}}"#.to_vec(),
        )),
        // POST /2/tweets.
        Ok(reply(
            201,
            br#"{"data":{"id":"1790000000000000002"}}"#.to_vec(),
        )),
    ]);
    let mut x = publisher(xpst_provider_x::HttpTransport::new(adapter));

    let published = x
        .publish(
            &PublishRequest::text("acct-a", "with media")
                .with_media(Media::video_bytes(vec![7_u8; 4]))
                .with_asset_key("sha256:live-transport"),
        )
        .expect("the script completes");

    assert_eq!(published.post_id, "1790000000000000002");

    let sent = x.into_transport().adapter().requests();
    assert_eq!(sent.len(), 4, "INIT, APPEND, FINALIZE, POST");

    assert_eq!(sent[0].url, "https://upload.test/1.1/media/upload.json");
    assert_eq!(
        String::from_utf8_lossy(&sent[0].body),
        "command=INIT&media_type=video%2Fmp4&total_bytes=4"
    );

    let append = &sent[1];
    let content_type = append.header("content-type").expect("a content type");
    let boundary = content_type
        .split("boundary=")
        .nth(1)
        .expect("the transport must state the boundary it wrote");
    assert!(
        content_type.starts_with("multipart/form-data; boundary="),
        "{content_type}"
    );
    let body = String::from_utf8_lossy(&append.body);
    assert!(
        body.contains("name=\"command\"\r\n\r\nAPPEND\r\n"),
        "{body}"
    );
    assert!(body.contains("name=\"media_id\"\r\n\r\n123\r\n"), "{body}");
    assert!(
        body.contains("name=\"segment_index\"\r\n\r\n0\r\n"),
        "{body}"
    );
    assert!(body.contains("name=\"media\"; filename="), "{body}");
    assert!(body.ends_with(&format!("--{boundary}--\r\n")), "{body}");

    assert_eq!(
        String::from_utf8_lossy(&sent[2].body),
        "command=FINALIZE&media_id=123"
    );
    assert_eq!(sent[3].url, "https://api.test/2/tweets");
    assert_eq!(sent[3].header("content-type"), Some("application/json"));
}

#[test]
fn every_request_in_the_sequence_is_signed_with_its_own_nonce() {
    let adapter = RecordingAdapter::scripted(vec![
        Ok(reply(202, br#"{"media_id_string":"9"}"#.to_vec())),
        Ok(PreparedResponse::new(204)),
        Ok(reply(
            200,
            br#"{"processing_info":{"state":"succeeded"}}"#.to_vec(),
        )),
        Ok(reply(201, br#"{"data":{"id":"1"}}"#.to_vec())),
    ]);
    let mut x = publisher(xpst_provider_x::HttpTransport::new(adapter));
    x.publish(
        &PublishRequest::text("acct-a", "nonces")
            .with_media(Media::video_bytes(vec![1_u8; 4]))
            .with_asset_key("sha256:nonces"),
    )
    .expect("the script completes");

    let sent = x.into_transport().adapter().requests();
    let nonces: Vec<String> = sent
        .iter()
        .map(|request| {
            request
                .header("authorization")
                .expect("signed")
                .split("\"oauth_nonce\"=\"")
                .nth(1)
                .expect("a nonce")
                .split('"')
                .next()
                .expect("a nonce value")
                .to_owned()
        })
        .collect();
    assert_eq!(nonces.len(), 4);
    let mut unique = nonces.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(
        unique.len(),
        4,
        "a reused nonce is a replayed request: {nonces:?}"
    );
}

#[test]
fn a_transport_failure_is_reported_without_the_signature_in_it() {
    let adapter = RecordingAdapter::scripted(vec![Err(HttpFailure::new(
        HttpFailureKind::Connect,
        "connection refused sending https://api.x.com/2/tweets?access_token=SECRET-TOKEN",
    ))]);
    let mut x = publisher(xpst_provider_x::HttpTransport::new(adapter));

    let error = x
        .publish(&PublishRequest::text("acct-a", "hello"))
        .expect_err("a refused connection cannot publish");

    assert!(matches!(error, XError::Transport { .. }), "{error:?}");
    let rendered = error.to_string();
    assert!(!rendered.contains("SECRET-TOKEN"), "{rendered}");
    assert!(!rendered.contains("at-test"), "{rendered}");
    assert!(!rendered.contains("oauth_signature"), "{rendered}");
}

/// The response type is the crate's own, not the adapter's.
#[test]
fn the_response_shape_is_the_crates_own_type() {
    let adapter =
        RecordingAdapter::scripted(vec![Ok(reply(429, b"Too Many Requests".to_vec())
            .with_header("x-rate-limit-reset", "1780000000"))]);
    let mut transport = xpst_provider_x::HttpTransport::new(adapter);
    let response: Response = xpst_provider_x::Transport::send(
        &mut transport,
        &xpst_provider_x::Request::new(xpst_provider_x::Method::Post, "https://api.test/2/tweets"),
    )
    .expect("scripted");
    assert_eq!(response.status, 429);
    assert_eq!(response.header("x-rate-limit-reset"), Some("1780000000"));
    assert_eq!(response.body_text(), "Too Many Requests");
}
