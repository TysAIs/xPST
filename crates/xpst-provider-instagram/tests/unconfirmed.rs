//! The ambiguous Instagram outcome, and the rule that settles it.
//!
//! `media_publish` is the call that creates the post, and Meta gives it no
//! idempotency key: a second call for the same container is a second post. So a
//! lost answer cannot be reported as "failed" — that invites the retry that
//! duplicates the post — and it cannot be reported as published, because nobody
//! saw a media id.
//!
//! These tests pin both halves of the fix: the transport failure is reported as
//! [`PublishError::Unconfirmed`] carrying the container id, and the way to finish
//! the job reads the container back *before* publishing again.

use std::collections::VecDeque;

use xpst_provider_instagram::{
    Candidate, Clock, Credentials, HttpRequest, HttpResponse, InstagramPost, MediaMeasurements,
    MediaSource, PublishError, PublishRequest, Publisher, Transport, TransportError, Unconfirmed,
};

// ── a transport that can lose an answer ────────────────────────────────────

/// One scripted step: a reply, or a request that never gets an answer.
enum Step {
    Reply(HttpResponse),
    /// The request was written and no answer came back.
    NoAnswer(&'static str),
}

struct ScriptedTransport {
    steps: VecDeque<Step>,
    requests: Vec<HttpRequest>,
}

impl ScriptedTransport {
    fn new(steps: Vec<Step>) -> Self {
        Self {
            steps: steps.into(),
            requests: Vec::new(),
        }
    }

    fn requests_to(&self, needle: &str) -> usize {
        self.requests
            .iter()
            .filter(|request| request.url.contains(needle))
            .count()
    }
}

impl Transport for ScriptedTransport {
    fn send(&mut self, request: &HttpRequest) -> Result<HttpResponse, TransportError> {
        self.requests.push(request.clone());
        match self
            .steps
            .pop_front()
            .ok_or_else(|| TransportError::new("the fake transport had no step scripted"))?
        {
            Step::Reply(response) => Ok(response),
            Step::NoAnswer(message) => Err(TransportError::new(message)),
        }
    }
}

#[derive(Default)]
struct FakeClock {
    now: u64,
}

impl Clock for FakeClock {
    fn now_ms(&self) -> u64 {
        self.now
    }

    fn sleep_ms(&mut self, millis: u64) {
        self.now += millis;
    }
}

fn json(body: &str) -> HttpResponse {
    HttpResponse::new(
        200,
        vec![("content-type".to_string(), "application/json".to_string())],
        body,
    )
}

fn container(id: &str) -> HttpResponse {
    json(&format!(r#"{{"id":"{id}"}}"#))
}

fn status(status_code: &str) -> HttpResponse {
    json(&format!(r#"{{"status_code":"{status_code}"}}"#))
}

fn published(id: &str) -> HttpResponse {
    json(&format!(r#"{{"id":"{id}"}}"#))
}

fn credentials() -> Credentials {
    Credentials::new("test-token", "17841400000000000")
}

fn image_request() -> PublishRequest {
    PublishRequest {
        caption: "A caption".to_string(),
        post: InstagramPost::Image {
            source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_string()),
            measurements: MediaMeasurements::default(),
        },
        posts_in_last_24h: 0,
    }
}

/// The unconfirmed outcome of a publish whose `media_publish` was lost.
fn lost_publish(transport: &mut ScriptedTransport) -> Unconfirmed {
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(transport, &mut clock, &credentials);

    let error = publisher
        .publish(image_request())
        .expect_err("a lost media_publish answer cannot be a success");

    match error {
        PublishError::Unconfirmed(unconfirmed) => unconfirmed,
        other => panic!("expected an unconfirmed outcome, got {other:?}"),
    }
}

// ── the outcome itself ─────────────────────────────────────────────────────

#[test]
fn a_lost_media_publish_is_unconfirmed_with_the_container_id() {
    let mut transport = ScriptedTransport::new(vec![
        Step::Reply(container("container-1")),
        Step::Reply(status("FINISHED")),
        Step::NoAnswer("connection reset by peer"),
    ]);

    let unconfirmed = lost_publish(&mut transport);

    assert_eq!(
        unconfirmed.platform,
        xpst_provider_instagram::Platform::Instagram
    );
    assert_eq!(
        unconfirmed.operation,
        xpst_provider_instagram::Operation::PublishContainer
    );
    assert_eq!(
        unconfirmed.primary_candidate(),
        Some("container-1"),
        "the container id is the only id the caller can act on"
    );
    assert_eq!(unconfirmed.container_id(), Some("container-1"));
    assert!(
        unconfirmed.detail.contains("connection reset"),
        "{}",
        unconfirmed.detail
    );

    // The words matter: this request may have arrived.
    let message = PublishError::Unconfirmed(unconfirmed.clone()).to_string();
    assert!(
        !message.contains("could not be sent"),
        "a request that was written may have arrived: {message}"
    );
    assert!(
        message.contains("https://graph.facebook.com"),
        "the message must name the read-back that settles it: {message}"
    );
    assert!(
        message.contains("may have been accepted"),
        "the message must say the outcome is unknown, not that it failed: {message}"
    );

    // Nothing was retried behind our back.
    assert_eq!(transport.requests_to("/media_publish"), 1);
    assert_eq!(transport.requests.len(), 3);
}

#[test]
fn the_unconfirmed_code_is_its_own_surface_code() {
    let mut transport = ScriptedTransport::new(vec![
        Step::Reply(container("container-1")),
        Step::Reply(status("FINISHED")),
        Step::NoAnswer("connection reset by peer"),
    ]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher.publish(image_request()).expect_err("no answer");

    assert_eq!(error.code(), "instagram.unconfirmed");
    assert_ne!(
        error.code(),
        "instagram.transport",
        "a lost publish is not the same class as a lost status read"
    );
}

// ── settling it ────────────────────────────────────────────────────────────

#[test]
fn a_readback_that_finds_the_post_resolves_it_without_publishing_again() {
    let mut transport = ScriptedTransport::new(vec![
        Step::Reply(container("container-1")),
        Step::Reply(status("FINISHED")),
        Step::NoAnswer("connection reset by peer"),
        // The read-back: Meta did publish it.
        Step::Reply(status("PUBLISHED")),
    ]);

    let unconfirmed = lost_publish(&mut transport);

    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);
    let receipt = publisher
        .resume_publish(&unconfirmed)
        .expect("a PUBLISHED container is a resolved publish");

    assert_eq!(receipt.container_id, "container-1");
    assert_eq!(
        receipt.media_id, None,
        "the media id was never returned to us and must not be invented"
    );
    assert_eq!(
        transport.requests_to("/media_publish"),
        1,
        "publishing again would have created a second post"
    );
    assert_eq!(
        transport
            .requests
            .iter()
            .filter(|request| request.method == xpst_provider_instagram::Method::Get)
            .count(),
        2,
        "one status check before the publish, one read-back after"
    );
    assert_eq!(transport.requests.len(), 4);
}

#[test]
fn a_readback_that_finds_no_post_publishes_exactly_once() {
    let mut transport = ScriptedTransport::new(vec![
        Step::Reply(container("container-1")),
        Step::Reply(status("FINISHED")),
        Step::NoAnswer("connection reset by peer"),
        // The read-back: it was never published, so publishing is still needed.
        Step::Reply(status("FINISHED")),
        Step::Reply(published("media-9")),
    ]);

    let unconfirmed = lost_publish(&mut transport);

    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);
    let receipt = publisher
        .resume_publish(&unconfirmed)
        .expect("the retry after a proven non-publish must succeed");

    assert_eq!(receipt.media_id.as_deref(), Some("media-9"));
    assert_eq!(
        transport.requests_to("/media_publish"),
        2,
        "the read-back came first, then exactly one publish"
    );
    let order: Vec<&str> = transport
        .requests
        .iter()
        .map(|request| {
            if request.url.contains("media_publish") {
                "publish"
            } else {
                "read"
            }
        })
        .collect();
    assert_eq!(
        order,
        vec!["read", "read", "publish", "read", "publish"],
        "the read-back must precede the re-publish"
    );
}

#[test]
fn a_readback_that_also_fails_leaves_the_outcome_unconfirmed() {
    let mut transport = ScriptedTransport::new(vec![
        Step::Reply(container("container-1")),
        Step::Reply(status("FINISHED")),
        Step::NoAnswer("connection reset by peer"),
        Step::NoAnswer("still no route to host"),
    ]);

    let unconfirmed = lost_publish(&mut transport);

    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);
    let error = publisher
        .resume_publish(&unconfirmed)
        .expect_err("a failed read-back cannot be read as either answer");

    assert!(
        matches!(error, PublishError::Transport { .. }),
        "the check itself failed, and that is reported as such: {error:?}"
    );
    assert_eq!(
        transport.requests_to("/media_publish"),
        1,
        "nothing may be published while the container's state is unknown"
    );
}

#[test]
fn an_outcome_from_another_platform_is_not_reconciled_here() {
    let foreign = Unconfirmed::new(
        xpst_provider_instagram::Platform::Bluesky,
        xpst_provider_instagram::Operation::CreateRecord,
        vec![Candidate::at_uri(
            "at://did:plc:abc/app.bsky.feed.post/3kabc",
        )],
        xpst_provider_instagram::Reconciliation::ReadRecord {
            uri: "at://did:plc:abc/app.bsky.feed.post/3kabc".to_string(),
        },
        "the answer was lost",
    );

    let mut transport = ScriptedTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .resume_publish(&foreign)
        .expect_err("a Bluesky outcome has no container to read");

    assert_eq!(error.code(), "instagram.cannot_reconcile");
    assert_eq!(transport.requests.len(), 0);
}

// ── the wording of the ordinary transport failure ──────────────────────────

#[test]
fn a_lost_container_request_does_not_claim_the_request_was_not_sent() {
    let mut transport = ScriptedTransport::new(vec![Step::NoAnswer("connection reset by peer")]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher.publish(image_request()).expect_err("no answer");

    let PublishError::Transport { stage, detail } = &error else {
        panic!("a lost container request is a transport failure, got {error:?}");
    };
    assert_eq!(*stage, xpst_provider_instagram::Stage::CreateContainer);
    assert!(detail.contains("connection reset"), "{detail}");

    let message = error.to_string();
    assert!(
        !message.contains("could not be sent"),
        "the transport cannot know that: {message}"
    );
    assert!(
        message.contains("gave no answer"),
        "say what is actually known: {message}"
    );
    assert_eq!(error.code(), "instagram.transport");
}
