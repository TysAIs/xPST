//! The resumable upload protocol through the *live* transport's mapping.
//!
//! `tests/upload_protocol.rs` proves the state machine against a fake
//! [`Transport`]. This file proves the other half: that the production
//! transport — [`xpst_provider_youtube::HttpTransport`] — turns what the driver
//! sends into the request a real endpoint would receive, and turns the answer
//! back into what the driver expects.
//!
//! The exchange is performed by a recording adapter, so the mapping is the real
//! code and the network is not. No test here opens a socket: that is enforced by
//! the adapter, not by a flag.

use std::future::Future;
use std::sync::Mutex;
use std::time::Duration;

use xpst_core::envelope::VideoMode;
use xpst_provider::http::fake::{reply, RecordingAdapter};
use xpst_provider::http::{HttpMethod, PreparedResponse};
use xpst_provider_youtube::token::{AccessToken, TokenError, TokenSource};
use xpst_provider_youtube::transport::MemoryMedia;
use xpst_provider_youtube::upload::{Sleeper, UploadPlan, Uploader, VideoMetadata};
use xpst_provider_youtube::HttpTransport;

const SESSION: &str = "https://upload.example/videos?uploadType=resumable&upload_id=S1";

struct FixedToken;

impl TokenSource for FixedToken {
    async fn access_token(&self) -> Result<AccessToken, TokenError> {
        Ok(AccessToken::new("ya29.live-token", Some(1_800_000_000)))
    }
}

#[derive(Default)]
struct RecordingSleeper {
    slept: Mutex<Vec<Duration>>,
}

impl Sleeper for RecordingSleeper {
    async fn sleep(&self, duration: Duration) {
        self.slept.lock().expect("sleeper lock").push(duration);
    }
}

fn block_on<F: Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("tokio runtime")
        .block_on(future)
}

fn plan(total_bytes: u64) -> UploadPlan {
    UploadPlan::new(
        VideoMode::Shorts,
        VideoMetadata::from_caption("A live transport test", VideoMode::Shorts)
            .expect("the caption fits"),
        total_bytes,
    )
    .expect("plan builds")
}

#[test]
fn the_whole_upload_is_mapped_onto_the_wire() {
    let total = 1_024u64;
    let adapter = RecordingAdapter::scripted(vec![
        // INIT answers 200 with the session URI in `Location`.
        Ok(reply(200, Vec::new()).with_header("Location", SESSION)),
        // The single chunk completes the upload.
        Ok(
            reply(200, br#"{"kind":"youtube#video","id":"vid-live"}"#.to_vec())
                .with_header("Content-Type", "application/json"),
        ),
    ]);
    let transport = HttpTransport::new(adapter);
    let tokens = FixedToken;
    let sleeper = RecordingSleeper::default();
    let media = MemoryMedia::zeroed(total);
    let uploader = Uploader::new(&transport, &tokens, &sleeper);

    let outcome = block_on(uploader.upload(&media, &plan(total))).expect("the script completes");

    assert_eq!(outcome.video_id, "vid-live");
    assert_eq!(outcome.mode, VideoMode::Shorts);

    let sent = transport.adapter().requests();
    assert_eq!(sent.len(), 2, "one initiate, one chunk");

    let initiate = &sent[0];
    assert_eq!(initiate.method, HttpMethod::Post);
    assert!(
        initiate.url.contains("uploadType=resumable"),
        "{}",
        initiate.url
    );
    assert_eq!(
        initiate.header("authorization"),
        Some("Bearer ya29.live-token"),
        "the access token must be the actual Authorization header"
    );
    assert_eq!(
        initiate.header("x-upload-content-length"),
        Some("1024"),
        "the declared length is what the endpoint sizes the session by"
    );
    let body: serde_json::Value =
        serde_json::from_slice(&initiate.body).expect("the metadata body is JSON");
    assert_eq!(body["status"]["privacyStatus"], "public");

    let chunk = &sent[1];
    assert_eq!(chunk.method, HttpMethod::Put);
    assert_eq!(chunk.url, SESSION, "the chunk goes to the session URI");
    assert_eq!(chunk.header("content-range"), Some("bytes 0-1023/1024"));
    assert_eq!(chunk.body.len(), total as usize);
}

#[test]
fn a_308_is_handed_back_as_a_response_rather_than_followed() {
    // With redirects followed, this exchange would become a second request and
    // the driver would never learn the offset the server actually holds.
    let total = 1_024u64;
    let adapter = RecordingAdapter::scripted(vec![
        Ok(reply(200, Vec::new()).with_header("Location", SESSION)),
        Ok(reply(308, Vec::new()).with_header("Range", "bytes=0-511")),
        Ok(reply(
            200,
            br#"{"kind":"youtube#video","id":"vid-live"}"#.to_vec(),
        )),
    ]);
    let transport = HttpTransport::new(adapter);
    let tokens = FixedToken;
    let sleeper = RecordingSleeper::default();
    let media = MemoryMedia::zeroed(total);
    let uploader = Uploader::new(&transport, &tokens, &sleeper);

    let outcome = block_on(uploader.upload(&media, &plan(total))).expect("the script completes");
    assert_eq!(outcome.video_id, "vid-live");

    let sent = transport.adapter().requests();
    assert_eq!(sent.len(), 3);
    assert_eq!(
        sent[2].header("content-range"),
        Some("bytes 512-1023/1024"),
        "the resume offset comes from the 308's Range, which only exists if the \
         308 reached the driver"
    );
}

#[test]
fn a_transport_failure_reaches_the_driver_without_the_token_in_it() {
    let adapter = RecordingAdapter::scripted(vec![Err(xpst_provider::http::HttpFailure::new(
        xpst_provider::http::HttpFailureKind::Connect,
        "connection refused sending https://upload.example/init?access_token=SECRET-TOKEN",
    ))]);
    let transport = HttpTransport::new(adapter);
    let tokens = FixedToken;
    let sleeper = RecordingSleeper::default();
    let media = MemoryMedia::zeroed(64);
    let uploader = Uploader::new(&transport, &tokens, &sleeper);

    let failure = block_on(uploader.upload(&media, &plan(64)))
        .expect_err("a refused initiate cannot publish");

    let rendered = format!("{failure:?} {failure}");
    assert!(!rendered.contains("ya29.live-token"), "{rendered}");
    assert!(!rendered.contains("SECRET-TOKEN"), "{rendered}");
}

/// The recorded requests are the same ones the fake path asserts, which is the
/// point: the live transport must not change the protocol, only the carrier.
#[test]
fn the_recorded_response_shape_round_trips() {
    let adapter = RecordingAdapter::scripted(vec![Ok(PreparedResponse::new(200)
        .with_header("Content-Type", "application/json")
        .with_body(br#"{"kind":"youtube#video","id":"x"}"#.to_vec()))]);
    let transport = HttpTransport::new(adapter);
    let request = xpst_provider_youtube::transport::HttpRequest::new(
        xpst_provider_youtube::transport::Method::Post,
        "https://upload.example/x",
    );
    let response = block_on(xpst_provider_youtube::transport::Transport::send(
        &transport, request,
    ))
    .expect("the script answered");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("content-type"), Some("application/json"));
    assert_eq!(response.body_text(), r#"{"kind":"youtube#video","id":"x"}"#);
}
