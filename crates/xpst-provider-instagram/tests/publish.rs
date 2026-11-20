//! Publishing tests against a fake transport.
//!
//! Nothing here touches the network: [`FakeTransport`] replays scripted
//! responses and records the requests it saw, so every assertion is about the
//! request sequence and the classified outcome.
//!
//! The clock is fake too. That is what makes the status-timeout test prove a
//! five-minute bound in microseconds, and it is why the poll policy can be
//! asserted on *elapsed* time rather than on how many loop iterations ran.

use std::collections::VecDeque;
use std::path::PathBuf;

use xpst_provider_instagram::classify::ResponseOutcome;
use xpst_provider_instagram::{
    CapabilityDeclaration, CarouselItem, Clock, Credentials, HttpRequest, HttpResponse,
    InstagramPost, MediaMeasurements, MediaSource, Policy, PollPolicy, PublishError,
    PublishRequest, Publisher, Stage, Transport, TransportError,
};

// ── the seam's two fakes ────────────────────────────────────────────────────

/// Replays scripted responses and records every request.
struct FakeTransport {
    script: VecDeque<HttpResponse>,
    requests: Vec<HttpRequest>,
}

impl FakeTransport {
    fn new(script: Vec<HttpResponse>) -> Self {
        Self {
            script: script.into(),
            requests: Vec::new(),
        }
    }

    fn request_count(&self) -> usize {
        self.requests.len()
    }
}

impl Transport for FakeTransport {
    fn send(&mut self, request: &HttpRequest) -> Result<HttpResponse, TransportError> {
        self.requests.push(request.clone());
        self.script
            .pop_front()
            .ok_or_else(|| TransportError::new("the fake transport had no response scripted"))
    }
}

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

// ── response fixtures ───────────────────────────────────────────────────────

fn json(status: u16, body: &str) -> HttpResponse {
    HttpResponse::new(
        status,
        vec![("content-type".to_string(), "application/json".to_string())],
        body,
    )
}

fn container_created(id: &str) -> HttpResponse {
    json(200, &format!(r#"{{"id":"{id}"}}"#))
}

fn container_created_with_uri(id: &str, uri: &str) -> HttpResponse {
    json(200, &format!(r#"{{"id":"{id}","uri":"{uri}"}}"#))
}

fn upload_ok() -> HttpResponse {
    json(200, r#"{"success":true,"message":"Upload successful."}"#)
}

fn status(status_code: &str) -> HttpResponse {
    json(200, &format!(r#"{{"status_code":"{status_code}"}}"#))
}

fn published(id: &str) -> HttpResponse {
    json(200, &format!(r#"{{"id":"{id}"}}"#))
}

fn html_login_page() -> HttpResponse {
    HttpResponse::new(
        200,
        vec![("content-type".to_string(), "text/html".to_string())],
        "<!DOCTYPE html><html><head><title>Login • Instagram</title></head><body></body></html>",
    )
}

fn credentials() -> Credentials {
    Credentials::new("test-token", "17841400000000000")
}

fn caption() -> String {
    "A test caption".to_string()
}

fn video_file(tag: &str, bytes: &[u8]) -> (PathBuf, MediaSource) {
    let path = std::env::temp_dir().join(format!("xpst-ig-test-{}-{tag}.mp4", std::process::id()));
    std::fs::write(&path, bytes).expect("write temp video");
    (path.clone(), MediaSource::LocalFile(path))
}

// ── the happy path: local video through the resumable endpoint ─────────────

#[test]
fn happy_video_publish_uploads_resumably_then_polls_then_publishes() {
    let bytes = b"fake mp4 bytes".to_vec();
    let (path, source) = video_file("happy", &bytes);

    let mut transport = FakeTransport::new(vec![
        container_created_with_uri(
            "container-1",
            "https://rupload.facebook.com/ig-api-upload/v26.0/container-1",
        ),
        upload_ok(),
        status("FINISHED"),
        published("media-1"),
    ]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let receipt = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Reel {
                source,
                measurements: MediaMeasurements::from_bytes(bytes.len() as u64),
            },
            posts_in_last_24h: 0,
        })
        .expect("the scripted publish must succeed");

    assert_eq!(receipt.media_id.as_deref(), Some("media-1"));
    assert_eq!(receipt.container_id, "container-1");
    assert_eq!(receipt.status_polls, 1);
    assert_eq!(transport.request_count(), 4);

    // 1. Create the resumable container.
    let create = &transport.requests[0];
    assert_eq!(create.method, xpst_provider_instagram::Method::PostForm);
    assert!(create.url.ends_with("/17841400000000000/media"));
    assert_eq!(create.form_value("media_type"), Some("REELS"));
    assert_eq!(create.form_value("upload_type"), Some("resumable"));
    assert_eq!(create.form_value("caption"), Some("A test caption"));
    assert_eq!(create.form_value("access_token"), Some("test-token"));

    // 2. Upload the bytes to the URL Meta returned, with the documented headers.
    let upload = &transport.requests[1];
    assert_eq!(upload.method, xpst_provider_instagram::Method::PostBytes);
    assert_eq!(
        upload.url,
        "https://rupload.facebook.com/ig-api-upload/v26.0/container-1"
    );
    assert_eq!(upload.header("Authorization"), Some("OAuth test-token"));
    assert_eq!(upload.header("offset"), Some("0"));
    assert_eq!(
        upload.header("file_size"),
        Some(bytes.len().to_string().as_str())
    );
    assert_eq!(upload.body, bytes);

    // 3. Ask for the container's status.
    let poll = &transport.requests[2];
    assert_eq!(poll.method, xpst_provider_instagram::Method::Get);
    assert_eq!(
        poll.query
            .iter()
            .find(|(key, _)| key == "fields")
            .map(|(_, value)| value.as_str()),
        Some("status_code")
    );

    // 4. Publish the container.
    let publish = &transport.requests[3];
    assert!(publish.url.ends_with("/17841400000000000/media_publish"));
    assert_eq!(publish.form_value("creation_id"), Some("container-1"));

    assert!(
        clock.sleeps.is_empty(),
        "a FINISHED container needs no wait"
    );
    std::fs::remove_file(path).ok();
}

// ── carousel bounds ─────────────────────────────────────────────────────────

fn carousel(items: usize) -> InstagramPost {
    InstagramPost::Carousel {
        items: (0..items)
            .map(|index| {
                CarouselItem::new(
                    MediaSource::PublicUrl(format!("https://cdn.example.com/item-{index}.jpg")),
                    MediaMeasurements::default(),
                )
            })
            .collect(),
    }
}

#[test]
fn a_carousel_of_ten_is_accepted_and_published_as_one_post() {
    let mut script = Vec::new();
    for index in 0..10 {
        script.push(container_created(&format!("child-{index}")));
    }
    script.push(container_created("carousel-1"));
    script.push(status("FINISHED"));
    script.push(published("media-2"));

    let mut transport = FakeTransport::new(script);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let receipt = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: carousel(10),
            posts_in_last_24h: 0,
        })
        .expect("ten children are inside Instagram's limit");

    assert_eq!(receipt.media_id.as_deref(), Some("media-2"));
    assert_eq!(
        transport.request_count(),
        13,
        "10 child containers + carousel container + its status check + publish"
    );

    let carousel_container = &transport.requests[10];
    assert_eq!(
        carousel_container.form_value("media_type"),
        Some("CAROUSEL")
    );
    assert_eq!(
        carousel_container.form_value("children"),
        Some("child-0,child-1,child-2,child-3,child-4,child-5,child-6,child-7,child-8,child-9")
    );

    // A carousel child is flagged as such, carries no caption of its own, and an
    // image child is not polled: only video has processing to wait for.
    for child in &transport.requests[0..10] {
        assert_eq!(child.form_value("is_carousel_item"), Some("true"));
        assert_eq!(child.form_value("caption"), None);
        assert!(
            child.form_value("image_url").is_some(),
            "an image child must carry image_url"
        );
    }
    assert_eq!(
        transport.requests[0].form_value("image_url"),
        Some("https://cdn.example.com/item-0.jpg")
    );
}

#[test]
fn a_carousel_of_eleven_is_refused_before_any_request() {
    let mut transport = FakeTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: carousel(11),
            posts_in_last_24h: 0,
        })
        .expect_err("eleven children must be refused");

    match error {
        PublishError::Refused(refusal) => {
            assert_eq!(refusal.code, "instagram.carousel.too_many");
            assert!(refusal.message.contains("10"), "{}", refusal.message);
            assert!(
                refusal.message.contains("will not drop items"),
                "the refusal must say nothing was silently dropped"
            );
        }
        other => panic!("expected a local refusal, got {other:?}"),
    }
    assert_eq!(transport.request_count(), 0);
}

#[test]
fn a_carousel_child_that_is_a_local_image_is_refused_like_a_single_one() {
    let mut transport = FakeTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Carousel {
                items: vec![
                    CarouselItem::new(
                        MediaSource::LocalFile(PathBuf::from("/tmp/first.jpg")),
                        MediaMeasurements::default(),
                    ),
                    CarouselItem::new(
                        MediaSource::PublicUrl("https://cdn.example.com/second.jpg".to_string()),
                        MediaMeasurements::default(),
                    ),
                ],
            },
            posts_in_last_24h: 0,
        })
        .expect_err("a local image child cannot be published");

    assert!(matches!(
        error,
        PublishError::Refused(refusal) if refusal.code == "instagram.image.needs_public_url"
    ));
    assert_eq!(transport.request_count(), 0);
}

// ── the image path ──────────────────────────────────────────────────────────

#[test]
fn an_image_without_a_public_url_is_refused_with_the_alternative() {
    let path = std::env::temp_dir().join(format!("xpst-ig-image-{}.jpg", std::process::id()));
    std::fs::write(&path, b"not really a jpeg").expect("write temp image");

    let mut transport = FakeTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let request = PublishRequest {
        caption: caption(),
        post: InstagramPost::Image {
            source: MediaSource::LocalFile(path.clone()),
            measurements: MediaMeasurements::default(),
        },
        posts_in_last_24h: 0,
    };

    // The preflight answer and the publish answer are the same refusal: a caller
    // that asks first never reaches the transport.
    let preflight = publisher
        .preflight(&request)
        .expect_err("preflight refuses");
    assert_eq!(preflight.code, "instagram.image.needs_public_url");

    let error = publisher.publish(request).expect_err("publish refuses");
    match error {
        PublishError::Refused(refusal) => {
            assert_eq!(refusal.code, "instagram.image.needs_public_url");
            assert!(
                refusal.message.contains("public URL"),
                "the refusal must name the requirement: {}",
                refusal.message
            );
            assert!(
                refusal.message.contains("Instagram app"),
                "the refusal must name a way forward: {}",
                refusal.message
            );
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
    assert_eq!(transport.request_count(), 0, "no request may be attempted");
    std::fs::remove_file(path).ok();
}

#[test]
fn an_image_with_a_public_url_publishes_without_a_media_type() {
    let mut transport = FakeTransport::new(vec![
        container_created("image-container"),
        status("FINISHED"),
        published("media-3"),
    ]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let receipt = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Image {
                source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_string()),
                measurements: MediaMeasurements {
                    bytes: Some(1024),
                    width: Some(1080),
                    height: Some(1350),
                    duration_seconds: None,
                },
            },
            posts_in_last_24h: 0,
        })
        .expect("a public image URL is publishable");

    assert_eq!(receipt.media_id.as_deref(), Some("media-3"));
    let create = &transport.requests[0];
    assert_eq!(
        create.form_value("image_url"),
        Some("https://cdn.example.com/photo.jpg")
    );
    assert_eq!(create.form_value("media_type"), None);
}

#[test]
fn an_image_outside_the_aspect_ratio_is_refused_locally() {
    let mut transport = FakeTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Image {
                source: MediaSource::PublicUrl("https://cdn.example.com/tall.jpg".to_string()),
                measurements: MediaMeasurements {
                    bytes: Some(1024),
                    width: Some(1080),
                    height: Some(1920),
                    duration_seconds: None,
                },
            },
            posts_in_last_24h: 0,
        })
        .expect_err("a 9:16 still is outside Instagram's feed range");

    assert!(matches!(
        error,
        PublishError::Refused(refusal) if refusal.code == "instagram.image.aspect"
    ));
    assert_eq!(transport.request_count(), 0);
}

// ── dishonest-looking answers ───────────────────────────────────────────────

#[test]
fn an_html_body_on_200_is_a_login_wall_not_a_publish() {
    let mut transport = FakeTransport::new(vec![html_login_page()]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Image {
                source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_string()),
                measurements: MediaMeasurements::default(),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("an HTML page is not a container");

    match error {
        PublishError::Response { stage, outcome } => {
            assert_eq!(stage, Stage::CreateContainer);
            assert!(matches!(outcome, ResponseOutcome::LoginWall { .. }));
            assert_eq!(error_code(&outcome), "login_wall");
            assert!(outcome.user_message().contains("login page"));
        }
        other => panic!("expected a classified provider response, got {other:?}"),
    }
    assert_eq!(transport.request_count(), 1);
}

fn error_code(outcome: &ResponseOutcome) -> &'static str {
    outcome.label()
}

#[test]
fn a_429_with_retry_after_keeps_the_declared_window() {
    let response = HttpResponse::new(
        429,
        vec![("Retry-After".to_string(), "900".to_string())],
        r#"{"error":{"message":"Application request limit reached","type":"OAuthException","code":4}}"#,
    );
    let mut transport = FakeTransport::new(vec![response]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Image {
                source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_string()),
                measurements: MediaMeasurements::default(),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("a throttle is not a publish");

    match error {
        PublishError::Response { outcome, .. } => {
            assert!(matches!(
                outcome,
                ResponseOutcome::RateLimited {
                    retry_after_seconds: Some(900),
                    ..
                }
            ));
            assert!(outcome.user_message().contains("900"));
        }
        other => panic!("expected a rate limit, got {other:?}"),
    }
    assert_eq!(transport.request_count(), 1);
}

#[test]
fn a_container_that_reports_error_is_a_processing_failure() {
    let mut transport = FakeTransport::new(vec![
        container_created_with_uri("container-9", "https://rupload.example/9"),
        upload_ok(),
        status("ERROR"),
    ]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let bytes = b"bytes".to_vec();
    let (path, source) = video_file("processing-error", &bytes);
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Reel {
                source,
                measurements: MediaMeasurements::from_bytes(bytes.len() as u64),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("ERROR is not a publish");

    assert!(matches!(error, PublishError::ContainerFailed { .. }));
    std::fs::remove_file(path).ok();
}

#[test]
fn a_container_that_is_already_published_is_not_published_twice() {
    let mut transport = FakeTransport::new(vec![
        container_created_with_uri("container-4", "https://rupload.example/4"),
        upload_ok(),
        status("PUBLISHED"),
    ]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let bytes = b"bytes".to_vec();
    let (path, source) = video_file("already-published", &bytes);
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Reel {
                source,
                measurements: MediaMeasurements::from_bytes(bytes.len() as u64),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("PUBLISHED means publishing again would duplicate the post");

    assert!(matches!(error, PublishError::AlreadyPublished { .. }));
    assert_eq!(
        transport.request_count(),
        3,
        "no media_publish was attempted"
    );
    std::fs::remove_file(path).ok();
}

// ── bounded polling ────────────────────────────────────────────────────────

fn poll_policy(interval_ms: u64, max_attempts: u32, max_total_ms: u64) -> Policy {
    Policy {
        poll: PollPolicy {
            interval_ms,
            max_attempts,
            max_total_ms,
        },
        ..Policy::default()
    }
}

#[test]
fn status_polling_stops_at_its_attempt_budget() {
    let mut script = vec![
        container_created_with_uri("container-2", "https://rupload.example/2"),
        upload_ok(),
    ];
    for _ in 0..5 {
        script.push(status("IN_PROGRESS"));
    }

    let mut transport = FakeTransport::new(script);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let bytes = b"bytes".to_vec();
    let (path, source) = video_file("attempt-budget", &bytes);
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials)
        .with_policy(poll_policy(60_000, 5, 300_000));

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Reel {
                source,
                measurements: MediaMeasurements::from_bytes(bytes.len() as u64),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("an unfinished container must time out, not loop");

    match error {
        PublishError::StatusPollTimeout {
            attempts,
            elapsed_ms,
        } => {
            assert_eq!(attempts, 5, "the attempt budget is five checks");
            assert_eq!(elapsed_ms, 240_000, "four waits between five checks");
        }
        other => panic!("expected a distinct timeout outcome, got {other:?}"),
    }
    assert_eq!(clock.sleeps, vec![60_000, 60_000, 60_000, 60_000]);
    assert_eq!(
        transport.request_count(),
        7,
        "create + upload + five checks"
    );
    std::fs::remove_file(path).ok();
}

#[test]
fn the_wall_clock_cap_can_stop_polling_before_the_attempt_budget() {
    let mut script = vec![
        container_created_with_uri("container-3", "https://rupload.example/3"),
        upload_ok(),
    ];
    for _ in 0..4 {
        script.push(status("IN_PROGRESS"));
    }

    let mut transport = FakeTransport::new(script);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let bytes = b"bytes".to_vec();
    let (path, source) = video_file("wall-clock-cap", &bytes);
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials)
        .with_policy(poll_policy(60_000, 10, 120_000));

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Reel {
                source,
                measurements: MediaMeasurements::from_bytes(bytes.len() as u64),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("the two-minute cap must stop the loop");

    match error {
        PublishError::StatusPollTimeout {
            attempts,
            elapsed_ms,
        } => {
            assert_eq!(attempts, 3);
            assert_eq!(elapsed_ms, 120_000);
            assert!(elapsed_ms <= 120_000);
        }
        other => panic!("expected a timeout, got {other:?}"),
    }
    assert_eq!(
        transport.request_count(),
        5,
        "create + upload + three checks"
    );
    std::fs::remove_file(path).ok();
}

// ── limits that are decided locally ────────────────────────────────────────

#[test]
fn the_daily_cap_refuses_at_fifty() {
    let mut transport = FakeTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: caption(),
            post: InstagramPost::Image {
                source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_string()),
                measurements: MediaMeasurements::default(),
            },
            posts_in_last_24h: 50,
        })
        .expect_err("the fifty-post gate must refuse");

    match error {
        PublishError::Refused(refusal) => {
            assert_eq!(refusal.code, "instagram.daily_cap.reached");
            assert!(refusal.message.contains("50"));
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
    assert_eq!(transport.request_count(), 0);
}

#[test]
fn a_caption_over_the_limit_is_refused_rather_than_truncated() {
    let mut transport = FakeTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let error = publisher
        .publish(PublishRequest {
            caption: "x".repeat(2201),
            post: InstagramPost::Image {
                source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_string()),
                measurements: MediaMeasurements::default(),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("2201 characters is over Instagram's caption limit");

    assert!(matches!(
        error,
        PublishError::Refused(refusal) if refusal.code == "instagram.caption.too_long"
    ));
    assert_eq!(transport.request_count(), 0);
}

#[test]
fn thirty_one_hashtags_are_refused() {
    let mut transport = FakeTransport::new(vec![]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let tags = (0..31)
        .map(|index| format!("#tag{index}"))
        .collect::<Vec<_>>()
        .join(" ");
    let error = publisher
        .publish(PublishRequest {
            caption: tags,
            post: InstagramPost::Image {
                source: MediaSource::PublicUrl("https://cdn.example.com/photo.jpg".to_string()),
                measurements: MediaMeasurements::default(),
            },
            posts_in_last_24h: 0,
        })
        .expect_err("31 hashtags are over the limit");

    assert!(matches!(
        error,
        PublishError::Refused(refusal) if refusal.code == "instagram.hashtag.too_many"
    ));
}

#[test]
fn the_capability_declaration_states_the_refusal_before_any_publish() {
    let declaration: CapabilityDeclaration = xpst_provider_instagram::declaration();
    assert_eq!(declaration.max_carousel_children, 10);
    assert_eq!(declaration.max_posts_per_day, 50);
    assert_eq!(declaration.max_caption_chars, 2200);
    assert!(
        declaration.note.contains("professional account"),
        "the declaration must say the access gate out loud"
    );
}

// ── Meta's own counter ─────────────────────────────────────────────────────

#[test]
fn publishing_usage_reads_the_platform_quota() {
    let mut transport = FakeTransport::new(vec![json(
        200,
        r#"{"data":[{"quota_usage":49,"config":{"quota_total":50,"quota_duration":86400}}]}"#,
    )]);
    let mut clock = FakeClock::default();
    let credentials = credentials();
    let mut publisher = Publisher::new(&mut transport, &mut clock, &credentials);

    let usage = publisher
        .publishing_usage()
        .expect("the quota read must parse");
    assert_eq!(usage.quota_usage, 49);
    assert_eq!(usage.quota_total, 50);
    assert_eq!(usage.quota_duration_seconds, 86_400);

    let request = &transport.requests[0];
    assert!(request.url.ends_with("/content_publishing_limit"));
}
