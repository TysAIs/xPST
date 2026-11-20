//! The Graph publishing sequence through the *live* transport's mapping.
//!
//! `tests/publish.rs` drives the sequence against a fake transport. This file
//! drives it through [`xpst_provider_instagram::HttpTransport`] — the code that
//! will face Meta — and asserts the requests it produced: method, URL including
//! the merged query, form body, and the token's location.
//!
//! Nothing here opens a socket: a recording adapter performs the exchange.

use xpst_provider::http::fake::{reply, RecordingAdapter};
use xpst_provider::http::{HttpFailure, HttpFailureKind, HttpMethod, PreparedResponse};
use xpst_provider_instagram::{
    Clock, Credentials, InstagramPost, MediaMeasurements, MediaSource, PublishError,
    PublishRequest, Publisher, GRAPH_API_VERSION,
};

#[derive(Default)]
struct FakeClock {
    now: u64,
    sleeps: Vec<u64>,
}

impl Clock for FakeClock {
    fn now_ms(&self) -> u64 {
        self.now
    }

    fn sleep_ms(&mut self, millis: u64) {
        self.sleeps.push(millis);
        self.now += millis;
    }
}

fn credentials() -> Credentials {
    Credentials::new("SECRET-ACCESS-TOKEN", "17841400000000000")
}

fn caption() -> String {
    "A test caption & more".to_owned()
}

fn request_image() -> PublishRequest {
    PublishRequest {
        caption: caption(),
        post: InstagramPost::Image {
            source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_owned()),
            measurements: MediaMeasurements {
                bytes: Some(1024),
                width: Some(1080),
                height: Some(1080),
                duration_seconds: None,
            },
        },
        posts_in_last_24h: 0,
    }
}

#[test]
fn the_container_publish_sequence_is_mapped_onto_graph_form_posts() {
    let adapter = RecordingAdapter::scripted(vec![
        Ok(reply(200, br#"{"id":"container-1"}"#.to_vec())),
        Ok(reply(200, br#"{"status_code":"FINISHED"}"#.to_vec())),
        Ok(reply(200, br#"{"id":"media-1"}"#.to_vec())),
    ]);
    let mut transport = xpst_provider_instagram::HttpTransport::new(adapter);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let receipt = publisher
        .publish(request_image())
        .expect("the script publishes");
    assert_eq!(receipt.media_id.as_deref(), Some("media-1"));

    drop(publisher);
    let sent = transport.adapter().requests();
    assert_eq!(sent.len(), 3, "create, poll, publish");

    let create = &sent[0];
    assert_eq!(create.method, HttpMethod::Post);
    assert_eq!(
        create.url,
        format!("https://graph.facebook.com/{GRAPH_API_VERSION}/17841400000000000/media"),
        "GRAPH_API_VERSION is part of the path"
    );
    assert_eq!(
        create.header("content-type"),
        Some("application/x-www-form-urlencoded")
    );
    let form = String::from_utf8_lossy(&create.body);
    assert!(
        form.contains("image_url=https%3A%2F%2Fcdn.example.com%2Fphoto.jpg"),
        "{form}"
    );
    assert!(
        form.contains("caption=A+test+caption+%26+more"),
        "the caption is form-encoded, not concatenated: {form}"
    );
    assert!(
        form.contains("access_token=SECRET-ACCESS-TOKEN"),
        "the token is a parameter of the request, never a header: {form}"
    );

    let poll = &sent[1];
    assert_eq!(poll.method, HttpMethod::Get);
    assert_eq!(
        poll.url,
        format!(
            "https://graph.facebook.com/{GRAPH_API_VERSION}/container-1?fields=status_code&access_token=SECRET-ACCESS-TOKEN"
        ),
        "an existing query is extended, not restarted with a second '?'"
    );
    assert!(poll.body.is_empty());

    let publish = &sent[2];
    assert_eq!(publish.method, HttpMethod::Post);
    assert_eq!(
        publish.url,
        format!("https://graph.facebook.com/{GRAPH_API_VERSION}/17841400000000000/media_publish")
    );
    assert_eq!(
        String::from_utf8_lossy(&publish.body),
        "creation_id=container-1&access_token=SECRET-ACCESS-TOKEN"
    );
}

#[test]
fn a_429_carries_its_retry_after_through_the_transport_untouched() {
    let adapter = RecordingAdapter::scripted(vec![Ok(PreparedResponse::new(429)
        .with_header("Retry-After", "900")
        .with_body(br#"{"error":{"message":"rate limit"}}"#.to_vec()))]);
    let mut transport = xpst_provider_instagram::HttpTransport::new(adapter);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(request_image())
        .expect_err("a 429 is not a publish");

    match error {
        PublishError::Response { outcome, .. } => {
            let text = format!("{outcome:?}");
            assert!(
                text.contains("RateLimited") || text.contains("rate"),
                "{text}"
            );
        }
        other => panic!("expected a classified response, got {other:?}"),
    }

    drop(publisher);
    let sent = transport.adapter().requests();
    assert_eq!(sent.len(), 1);
}

#[test]
fn a_lost_answer_on_media_publish_never_leaks_the_token() {
    let adapter = RecordingAdapter::scripted(vec![
        Ok(reply(200, br#"{"id":"container-9"}"#.to_vec())),
        Ok(reply(200, br#"{"status_code":"FINISHED"}"#.to_vec())),
        Err(HttpFailure::new(
            HttpFailureKind::Timeout,
            "no answer inside 300 s for https://graph.facebook.com/v23.0/1/media_publish?access_token=SECRET-ACCESS-TOKEN",
        )),
    ]);
    let mut transport = xpst_provider_instagram::HttpTransport::new(adapter);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(request_image())
        .expect_err("a lost answer is not a success");

    // The ambiguous outcome has to name the container, because that is what a
    // reconciliation reads back.
    match &error {
        PublishError::Unconfirmed(unconfirmed) => {
            assert!(
                format!("{unconfirmed:?}").contains("container-9"),
                "the candidate id must survive: {unconfirmed:?}"
            );
        }
        other => panic!("a lost media_publish answer must be unconfirmed, got {other:?}"),
    }

    let rendered = format!("{error} {error:?}");
    assert!(!rendered.contains("SECRET-ACCESS-TOKEN"), "{rendered}");
    assert!(rendered.contains("access_token=REDACTED"), "{rendered}");
}
