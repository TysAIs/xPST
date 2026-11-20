//! The resumable upload protocol, driven against a scripted transport.
//!
//! Every test here is offline: the transport never opens a socket, and the
//! sleeper never spends the delay it is given. What is asserted is the wire
//! conversation (method, URL, `Content-Range`, body), the resume offset read
//! from a 308's `Range`, the delay taken from `Retry-After`, and the class of
//! each failure.

use std::collections::VecDeque;
use std::future::Future;
use std::sync::Mutex;
use std::time::Duration;

use xpst_core::envelope::VideoMode;
use xpst_provider_youtube::capability::{
    MAX_DESCRIPTION_CHARS, MAX_TAGS, MAX_TITLE_CHARS, SHORTS_CHUNK_BYTES,
    UPLOADS_PER_DAY_PER_PROJECT,
};
use xpst_provider_youtube::mode::post_url;
use xpst_provider_youtube::shapes::NO_BODY_RESPONSES;
use xpst_provider_youtube::token::{AccessToken, TokenError, TokenSource};
use xpst_provider_youtube::transport::{
    HttpRequest, HttpResponse, MemoryMedia, Method, Transport, TransportError,
};
use xpst_provider_youtube::upload::{
    classify_response, parse_range_next_offset, MetadataRefusal, PlanError, Privacy,
    ReconcileRequest, ReconcileVerdict, Reconciler, ResponseKind, RetryAfter, RetryPolicy, Sleeper,
    UploadFailure, UploadPlan, Uploader, VideoMetadata, CODE_DESCRIPTION_TOO_LONG,
    CODE_POLICY_REFUSED, CODE_PROTOCOL_ERROR, CODE_QUOTA_EXCEEDED, CODE_RATE_LIMITED,
    CODE_TITLE_TOO_LONG, CODE_TOO_MANY_TAGS, CODE_UPLOAD_UNCONFIRMED, CODE_UPLOAD_UNVERIFIED,
    SHORTS_MARKER,
};

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

#[derive(Debug)]
enum Replay {
    Response(HttpResponse),
    Failure(TransportError),
}

#[derive(Default)]
struct FakeTransport {
    script: Mutex<VecDeque<Replay>>,
    sent: Mutex<Vec<HttpRequest>>,
}

impl FakeTransport {
    fn scripted(steps: Vec<Replay>) -> Self {
        Self {
            script: Mutex::new(steps.into()),
            sent: Mutex::new(Vec::new()),
        }
    }

    fn requests(&self) -> Vec<HttpRequest> {
        self.sent.lock().expect("transport lock").clone()
    }
}

impl Transport for FakeTransport {
    fn send(
        &self,
        request: HttpRequest,
    ) -> impl Future<Output = Result<HttpResponse, TransportError>> + Send {
        self.sent.lock().expect("transport lock").push(request);
        let next = self
            .script
            .lock()
            .expect("script lock")
            .pop_front()
            .unwrap_or_else(|| Replay::Failure(TransportError::fatal("script exhausted")));
        async move {
            match next {
                Replay::Response(response) => Ok(response),
                Replay::Failure(error) => Err(error),
            }
        }
    }
}

#[derive(Default)]
struct RecordingSleeper {
    slept: Mutex<Vec<Duration>>,
}

impl RecordingSleeper {
    fn slept(&self) -> Vec<Duration> {
        self.slept.lock().expect("sleeper lock").clone()
    }
}

impl Sleeper for RecordingSleeper {
    fn sleep(&self, duration: Duration) -> impl Future<Output = ()> + Send {
        self.slept.lock().expect("sleeper lock").push(duration);
        async {}
    }
}

/// A reconcile probe with a fixed answer that records what it was asked.
#[derive(Debug)]
struct ScriptedReconciler {
    verdict: ReconcileVerdict,
    asked: Mutex<Vec<(u64, u64, u16)>>,
}

impl ScriptedReconciler {
    fn new(verdict: ReconcileVerdict) -> Self {
        Self {
            verdict,
            asked: Mutex::new(Vec::new()),
        }
    }

    fn asked(&self) -> Vec<(u64, u64, u16)> {
        self.asked.lock().expect("reconciler lock").clone()
    }
}

impl Reconciler for ScriptedReconciler {
    async fn reconcile(&self, request: &ReconcileRequest<'_>) -> ReconcileVerdict {
        self.asked.lock().expect("reconciler lock").push((
            request.bytes_submitted,
            request.total_bytes,
            request.status,
        ));
        self.verdict.clone()
    }
}

struct FixedToken(Result<AccessToken, TokenError>);

impl TokenSource for FixedToken {
    fn access_token(&self) -> impl Future<Output = Result<AccessToken, TokenError>> + Send {
        let value = self.0.clone();
        async move { value }
    }
}

fn valid_token() -> FixedToken {
    FixedToken(Ok(AccessToken::new("ya29.test", Some(1_800_000_000))))
}

fn block_on<F: Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("tokio runtime")
        .block_on(future)
}

// ---------------------------------------------------------------------------
// Response builders
// ---------------------------------------------------------------------------

const SESSION: &str = "https://upload.example/videos?uploadType=resumable&upload_id=S1";

fn initiated() -> Replay {
    Replay::Response(HttpResponse::new(200).with_header("Location", SESSION))
}

fn initiated_again() -> Replay {
    Replay::Response(HttpResponse::new(200).with_header(
        "Location",
        "https://upload.example/videos?uploadType=resumable&upload_id=S2",
    ))
}

fn incomplete(range: Option<&str>) -> Replay {
    let mut response = HttpResponse::new(308);
    if let Some(range) = range {
        response = response.with_header("Range", range);
    }
    Replay::Response(response)
}

fn created(id: &str) -> Replay {
    Replay::Response(
        HttpResponse::new(200)
            .with_header("Content-Type", "application/json")
            .with_body(format!(r#"{{"kind":"youtube#video","id":"{id}"}}"#).into_bytes()),
    )
}

fn quota_forbidden() -> Replay {
    Replay::Response(HttpResponse::new(403).with_body(
        br#"{
            "error": {
                "code": 403,
                "message": "The request cannot be completed because you have exceeded your quota.",
                "errors": [{
                    "message": "The request cannot be completed because you have exceeded your quota.",
                    "domain": "youtube.quota",
                    "reason": "quotaExceeded"
                }],
                "status": "PERMISSION_DENIED"
            }
        }"#
        .to_vec(),
    ))
}

fn policy_forbidden() -> Replay {
    Replay::Response(
        HttpResponse::new(403).with_body(
            br#"{
            "error": {
                "code": 403,
                "message": "The uploader is not permitted to upload to this channel.",
                "errors": [{
                    "message": "The uploader is not permitted to upload to this channel.",
                    "domain": "youtube.header",
                    "reason": "forbidden"
                }],
                "status": "PERMISSION_DENIED"
            }
        }"#
            .to_vec(),
        ),
    )
}

fn server_error() -> Replay {
    Replay::Response(HttpResponse::new(503).with_body(b"backend error".to_vec()))
}

fn plan(mode: VideoMode, total_bytes: u64) -> UploadPlan {
    UploadPlan::new(
        mode,
        VideoMetadata::from_caption("A test video", mode).expect("the caption fits"),
        total_bytes,
    )
    .expect("plan builds")
}

fn fast_policy() -> RetryPolicy {
    RetryPolicy {
        max_attempts: 3,
        base_delay: Duration::from_millis(1),
        max_delay: Duration::from_millis(10),
        max_retry_after: Duration::from_secs(300),
    }
}

// ---------------------------------------------------------------------------
// 1. Happy path
// ---------------------------------------------------------------------------

/// The bodyless successes, pinned.
///
/// Google answers an initiation with `Location` and an unfinished chunk with
/// `308 Resume Incomplete` — no body either time. A fake that invented a JSON
/// reply there would pass against a handler that cannot work in production, so
/// the scripted responses in this file are asserted against the documented
/// shapes instead of being taken on trust.
#[test]
fn bodyless_successes_match_the_documented_shapes() {
    assert_eq!(
        NO_BODY_RESPONSES.len(),
        2,
        "both bodyless answers are documented"
    );
    for shape in NO_BODY_RESPONSES {
        assert_eq!(
            shape.body,
            xpst_provider::BodyShape::Empty,
            "{} is documented as bodyless",
            shape.endpoint
        );
    }

    let Replay::Response(initiate) = initiated() else {
        panic!("initiation is a response, not a failure");
    };
    assert!(
        initiate.body.is_empty(),
        "initiation carries the session in Location, never in a body"
    );

    let Replay::Response(resume) = incomplete(None) else {
        panic!("a 308 is a response");
    };
    assert!(
        resume.body.is_empty(),
        "a 308 carries its range in a header"
    );

    assert!(
        matches!(
            classify_response(&resume, 1_000),
            ResponseKind::Incomplete(None)
        ),
        "an empty 308 is the handler's resume signal, not a parse failure"
    );
}

#[test]
fn happy_path_publishes_a_short_in_two_chunks() {
    let total = 300_000u64;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        incomplete(Some("bytes=0-262143")),
        created("dQw4w9WgXcQ"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let outcome = block_on(uploader.upload(&media, &plan)).expect("upload succeeds");

    assert_eq!(outcome.video_id, "dQw4w9WgXcQ");
    assert_eq!(outcome.mode, VideoMode::Shorts);
    assert_eq!(outcome.url, "https://youtube.com/shorts/dQw4w9WgXcQ");

    let requests = transport.requests();
    assert_eq!(requests.len(), 3);

    // Initiation: resumable, metadata, declared length, bearer token.
    let initiate = &requests[0];
    assert_eq!(initiate.method, Method::Post);
    assert!(initiate.url.contains("uploadType=resumable"));
    assert!(initiate.url.contains("part=snippet,status"));
    assert_eq!(initiate.header("Authorization"), Some("Bearer ya29.test"));
    assert_eq!(initiate.header("X-Upload-Content-Length"), Some("300000"));
    assert_eq!(initiate.header("X-Upload-Content-Type"), Some("video/mp4"));
    let body: serde_json::Value = serde_json::from_slice(&initiate.body).expect("metadata is JSON");
    assert_eq!(body["status"]["privacyStatus"], "public");
    assert_eq!(body["status"]["selfDeclaredMadeForKids"], false);
    assert_eq!(body["snippet"]["categoryId"], "28");
    assert!(body["snippet"]["title"]
        .as_str()
        .expect("title")
        .contains("#Shorts"));

    // Chunks: the documented range form, inclusive end, exact tail.
    assert_eq!(requests[1].method, Method::Put);
    assert_eq!(requests[1].url, SESSION);
    assert_eq!(
        requests[1].header("Content-Range"),
        Some("bytes 0-262143/300000")
    );
    assert_eq!(requests[1].body.len() as u64, SHORTS_CHUNK_BYTES);
    assert_eq!(
        requests[2].header("Content-Range"),
        Some("bytes 262144-299999/300000")
    );
    assert_eq!(requests[2].body.len() as u64, 300_000 - SHORTS_CHUNK_BYTES);

    // Nothing was retried, so nothing was slept.
    assert!(sleeper.slept().is_empty());
}

#[test]
fn long_form_uses_the_watch_url_and_never_the_shorts_tag() {
    let total = 10u64;
    let transport = FakeTransport::scripted(vec![initiated(), created("abcdefghijk")]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Long, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let outcome = block_on(uploader.upload(&media, &plan)).expect("upload succeeds");
    assert_eq!(outcome.url, "https://www.youtube.com/watch?v=abcdefghijk");

    let body: serde_json::Value =
        serde_json::from_slice(&transport.requests()[0].body).expect("metadata is JSON");
    let title = body["snippet"]["title"].as_str().expect("title");
    assert!(!title.to_ascii_lowercase().contains("#shorts"));
}

// ---------------------------------------------------------------------------
// 2. Resume after 308 with a partial range
// ---------------------------------------------------------------------------

#[test]
fn resumes_from_the_range_a_308_reports_not_from_the_offset_it_sent() {
    let total = 3 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        // The server took only half of the first chunk.
        incomplete(Some("bytes=0-131071")),
        // Then it catches up to the end of the second request.
        incomplete(Some("bytes=0-393215")),
        incomplete(Some("bytes=0-655359")),
        created("resumed0123"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let outcome = block_on(uploader.upload(&media, &plan)).expect("upload resumes");
    assert_eq!(outcome.video_id, "resumed0123");

    let requests = transport.requests();
    let ranges: Vec<&str> = requests[1..]
        .iter()
        .map(|request| request.header("Content-Range").expect("content-range"))
        .collect();
    assert_eq!(
        ranges,
        vec![
            "bytes 0-262143/786432",
            // The second request starts where the Range header said the
            // server got to, not where the driver's own count said.
            "bytes 131072-393215/786432",
            "bytes 393216-655359/786432",
            "bytes 655360-786431/786432",
        ]
    );
    assert_eq!(requests.len(), 5);
    assert!(sleeper.slept().is_empty(), "resuming is not retrying");
}

#[test]
fn a_308_without_a_range_restarts_from_zero() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        incomplete(None),
        incomplete(Some("bytes=0-262143")),
        created("restart0001"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    block_on(uploader.upload(&media, &plan)).expect("upload resumes");

    let requests = transport.requests();
    assert_eq!(
        requests[1].header("Content-Range"),
        Some("bytes 0-262143/524288")
    );
    assert_eq!(
        requests[2].header("Content-Range"),
        Some("bytes 0-262143/524288"),
        "a 308 with no Range means nothing landed, so chunk zero is sent again"
    );
}

#[test]
fn finalises_with_a_status_request_when_every_byte_is_accepted() {
    let total = SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        // The whole file is in, but the session is still open.
        incomplete(Some("bytes=0-262143")),
        created("finalise001"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let outcome = block_on(uploader.upload(&media, &plan)).expect("upload finalises");
    assert_eq!(outcome.video_id, "finalise001");

    let requests = transport.requests();
    assert_eq!(requests.len(), 3);
    assert_eq!(
        requests[2].header("Content-Range"),
        Some("bytes */262144"),
        "finalisation is the documented `bytes */{{total}}` request"
    );
    assert!(requests[2].body.is_empty());
    assert_eq!(requests[2].header("Content-Length"), Some("0"));
}

// ---------------------------------------------------------------------------
// 3. 403 quota: permanent, not a retry
// ---------------------------------------------------------------------------

#[test]
fn forbidden_quota_is_a_permanent_refusal_and_is_not_retried() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![initiated(), quota_forbidden()]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("quota refuses");

    assert_eq!(failure.code, CODE_QUOTA_EXCEEDED);
    assert!(
        !failure.retryable(),
        "a spent daily bucket is not retryable"
    );
    assert!(!failure.needs_reauth());
    assert_eq!(failure.status, Some(403));
    assert_eq!(failure.requests, 2, "initiation plus one chunk, no retries");
    assert_eq!(transport.requests().len(), 2);
    assert!(
        sleeper.slept().is_empty(),
        "a permanent refusal must not sleep"
    );
    assert!(
        failure
            .message
            .contains(&UPLOADS_PER_DAY_PER_PROJECT.to_string()),
        "the refusal names the documented ceiling: {}",
        failure.message
    );
}

#[test]
fn a_403_without_a_quota_reason_is_a_policy_refusal_not_a_quota_one() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![initiated(), policy_forbidden()]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("403 refuses");

    assert_eq!(failure.code, CODE_POLICY_REFUSED);
    assert!(!failure.retryable());
    assert!(failure.message.contains("forbidden"), "{}", failure.message);
    assert!(failure.message.contains("not permitted to upload"));
}

// ---------------------------------------------------------------------------
// 4. 429 with Retry-After
// ---------------------------------------------------------------------------

#[test]
fn rate_limit_waits_exactly_the_retry_after_it_was_given() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Response(HttpResponse::new(429).with_header("Retry-After", "7")),
        created("throttled01"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("retry succeeds");
    assert_eq!(outcome.video_id, "throttled01");
    assert_eq!(sleeper.slept(), vec![Duration::from_secs(7)]);

    // The chunk is re-sent unchanged: nothing was accepted by the 429.
    let requests = transport.requests();
    assert_eq!(requests.len(), 3);
    assert_eq!(
        requests[2].header("Content-Range"),
        requests[1].header("Content-Range")
    );
}

#[test]
fn a_retry_after_beyond_the_window_is_reported_instead_of_slept() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Response(HttpResponse::new(429).with_header("Retry-After", "3600")),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("parked, not slept");

    assert_eq!(failure.code, CODE_RATE_LIMITED);
    assert!(failure.retryable());
    assert_eq!(failure.retry_after, Some(Duration::from_secs(3600)));
    assert!(
        sleeper.slept().is_empty(),
        "the caller owns a wait this long"
    );
}

#[test]
fn a_403_rate_limit_reason_is_retryable_because_google_says_it_is_short_term() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Response(
            HttpResponse::new(403).with_body(
                br#"{"error":{"code":403,"message":"Rate limit exceeded","errors":[
                 {"reason":"rateLimitExceeded","domain":"youtube.quota"}]}}"#
                    .to_vec(),
            ),
        ),
        created("ratelimit01"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("throttle retried");
    assert_eq!(outcome.video_id, "ratelimit01");
    assert_eq!(sleeper.slept().len(), 1);
}

// ---------------------------------------------------------------------------
// 5. invalid_grant → needs_reauth
// ---------------------------------------------------------------------------

#[test]
fn invalid_grant_becomes_needs_reauth_and_never_touches_the_network() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![initiated(), created("unused0001")]);
    let sleeper = RecordingSleeper::default();
    let token = FixedToken(Err(TokenError::NeedsReauth {
        detail: "the refresh token is no longer valid (invalid_grant)".to_owned(),
    }));
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("reauth required");

    assert!(failure.needs_reauth());
    assert!(!failure.retryable());
    assert_eq!(failure.requests, 0);
    assert!(transport.requests().is_empty());
    assert!(
        failure.message.contains("xpst auth youtube"),
        "{}",
        failure.message
    );
    assert!(failure.message.contains("invalid_grant"));
}

#[test]
fn invalid_grant_maps_to_needs_reauth_at_the_oauth_crate_boundary() {
    use yup_oauth2::error::{AuthError, AuthErrorCode};

    let error = yup_oauth2::Error::AuthError(AuthError {
        error: AuthErrorCode::InvalidGrant,
        error_description: Some("Token has been expired or revoked.".to_owned()),
        error_uri: None,
    });
    let mapped = xpst_provider_youtube::token::classify_oauth_error(&error);
    assert!(mapped.needs_reauth(), "{mapped:?}");

    let misconfigured = yup_oauth2::Error::AuthError(AuthError {
        error: AuthErrorCode::InvalidClient,
        error_description: None,
        error_uri: None,
    });
    let mapped = xpst_provider_youtube::token::classify_oauth_error(&misconfigured);
    assert!(!mapped.needs_reauth());
    assert!(!mapped.retryable(), "a bad client id is not a retry");
}

// ---------------------------------------------------------------------------
// 6. Retry behaviour that is not a refusal
// ---------------------------------------------------------------------------

#[test]
fn a_5xx_is_retried_with_backoff() {
    let total = 1024u64;
    let transport =
        FakeTransport::scripted(vec![initiated(), server_error(), created("eventual01")]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("server error retried");
    assert_eq!(outcome.video_id, "eventual01");
    assert_eq!(sleeper.slept(), vec![Duration::from_millis(1)]);
    assert_eq!(transport.requests().len(), 3);
}

#[test]
fn exhausting_the_retry_budget_is_reported_as_retryable() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        server_error(),
        server_error(),
        server_error(),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(RetryPolicy {
        max_attempts: 2,
        ..fast_policy()
    });

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("gives up");
    assert!(failure.retryable(), "still transient, just out of budget");
    assert_eq!(failure.code, "YOUTUBE_SERVER_ERROR");
    assert_eq!(failure.requests, 3);
}
#[test]
fn a_network_error_is_retried_and_resumed_from_the_reported_range() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Failure(TransportError::network("connection reset")),
        // The lost request had in fact landed in full.
        incomplete(Some("bytes=0-262143")),
        created("netresume01"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("network retried");
    assert_eq!(outcome.video_id, "netresume01");

    let requests = transport.requests();
    assert_eq!(requests.len(), 4);
    assert_eq!(
        requests[2].header("Content-Range"),
        requests[1].header("Content-Range"),
        "the ambiguous chunk is re-sent unchanged"
    );
    assert_eq!(
        requests[3].header("Content-Range"),
        Some("bytes 262144-524287/524288"),
        "and the 308 that answers it moves the offset forward"
    );
}

#[test]
fn an_expired_session_is_reopened_and_restarts_from_zero() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Response(HttpResponse::new(404)),
        initiated_again(),
        created("restarted01"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("session restarted");
    assert_eq!(outcome.video_id, "restarted01");

    let requests = transport.requests();
    assert_eq!(requests.len(), 4);
    assert!(
        requests[3].url.contains("upload_id=S2"),
        "the chunk after the restart talks to the new session: {}",
        requests[3].url
    );
    assert_eq!(
        requests[3].header("Content-Range"),
        Some("bytes 0-1023/1024"),
        "a new session starts from byte zero"
    );
}

#[test]
fn a_session_that_never_advances_is_given_up_on() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![initiated(), incomplete(None), incomplete(None)]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(RetryPolicy {
        max_attempts: 2,
        ..fast_policy()
    });

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("no progress");
    assert!(failure.retryable(), "a stuck session is not a refusal");
    assert_eq!(failure.code, CODE_PROTOCOL_ERROR);
    assert!(
        failure.message.contains("no progress"),
        "{}",
        failure.message
    );
    assert_eq!(failure.requests, 3);
}

#[test]
fn a_success_without_a_usable_video_id_is_unverified_not_success() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Response(HttpResponse::new(200).with_body(br#"{"kind":"youtube#video"}"#.to_vec())),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("unverified");
    assert_eq!(failure.code, CODE_UPLOAD_UNVERIFIED);
    assert!(!failure.retryable());
}

#[test]
fn an_initiation_without_a_location_is_a_protocol_error() {
    let total = 1024u64;
    let transport = FakeTransport::scripted(vec![Replay::Response(HttpResponse::new(200))]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("no session");
    assert_eq!(failure.code, CODE_PROTOCOL_ERROR);
    assert!(failure.message.contains("Location"), "{}", failure.message);
}

// ---------------------------------------------------------------------------
// Protocol primitives
// ---------------------------------------------------------------------------

#[test]
fn range_parsing_treats_the_end_as_inclusive_and_clamps_to_the_total() {
    assert_eq!(parse_range_next_offset("bytes=0-0", 100), Some(1));
    assert_eq!(
        parse_range_next_offset("bytes=0-1048575", 10_000_000),
        Some(1_048_576)
    );
    assert_eq!(
        parse_range_next_offset("bytes=524288-1048575", 1_048_576),
        Some(1_048_576)
    );
    assert_eq!(parse_range_next_offset("bytes=0-999", 1000), Some(1000));
    // A range past the declared total never moves the offset backwards.
    assert_eq!(parse_range_next_offset("bytes=0-2000", 1000), Some(1000));
    assert_eq!(parse_range_next_offset("bytes=500-400", 1000), None);
    assert_eq!(parse_range_next_offset("items=0-1", 1000), None);
    assert_eq!(parse_range_next_offset("bytes=0-", 1000), None);
}

#[test]
fn retry_after_accepts_seconds_and_an_http_date() {
    assert_eq!(RetryAfter::parse("15"), Some(RetryAfter::Seconds(15)));
    assert_eq!(RetryAfter::parse("nonsense"), None);

    let now = std::time::SystemTime::now();
    let future = httpdate::fmt_http_date(now + Duration::from_secs(60));
    match RetryAfter::parse(&future) {
        Some(RetryAfter::At(at)) => {
            let delay = RetryAfter::At(at).delay(now);
            assert!((59..=61).contains(&delay.as_secs()), "{delay:?}");
        }
        other => panic!("expected an HTTP date, got {other:?}"),
    }
    let past = httpdate::fmt_http_date(now - Duration::from_secs(60));
    assert_eq!(
        RetryAfter::parse(&past).expect("date parses").delay(now),
        Duration::ZERO
    );
}

#[test]
fn classification_reads_the_body_not_just_the_status() {
    let quota: HttpResponse = match quota_forbidden() {
        Replay::Response(response) => response,
        Replay::Failure(_) => unreachable!(),
    };
    match classify_response(&quota, 1024) {
        ResponseKind::Permanent {
            code,
            status,
            message,
            ..
        } => {
            assert_eq!(code, CODE_QUOTA_EXCEEDED);
            assert_eq!(status, 403);
            assert!(message.contains("100"), "{message}");
        }
        other => panic!("expected a permanent quota refusal, got {other:?}"),
    }

    let policy: HttpResponse = match policy_forbidden() {
        Replay::Response(response) => response,
        Replay::Failure(_) => unreachable!(),
    };
    assert!(matches!(
        classify_response(&policy, 1024),
        ResponseKind::Permanent {
            code: CODE_POLICY_REFUSED,
            ..
        }
    ));

    // A 403 with an unparseable body is still permanent, never retried.
    let opaque = HttpResponse::new(403).with_body(b"<html>Forbidden</html>".to_vec());
    assert!(matches!(
        classify_response(&opaque, 1024),
        ResponseKind::Permanent {
            code: CODE_POLICY_REFUSED,
            ..
        }
    ));

    assert!(matches!(
        classify_response(&HttpResponse::new(401), 1024),
        ResponseKind::NeedsReauth { .. }
    ));

    // A throttle body that mentions "quota" in its prose is still a throttle:
    // the structured reason wins over the keyword.
    let throttled = HttpResponse::new(403).with_body(
        br#"{"error":{"code":403,"message":"The request cannot be completed because you have exceeded your quota.","errors":[{"reason":"rateLimitExceeded","domain":"youtube.quota"}]}}"#
            .to_vec(),
    );
    assert!(
        matches!(
            classify_response(&throttled, 1024),
            ResponseKind::Throttled(_)
        ),
        "reason rateLimitExceeded must win over the word quota: {:?}",
        classify_response(&throttled, 1024)
    );
    assert!(matches!(
        classify_response(&HttpResponse::new(400), 1024),
        ResponseKind::Permanent { status: 400, .. }
    ));
    assert!(matches!(
        classify_response(&HttpResponse::new(504), 1024),
        ResponseKind::Retryable { status: 504, .. }
    ));
}

// ---------------------------------------------------------------------------
// Mode constraints on the wire
// ---------------------------------------------------------------------------

#[test]
fn the_chunk_size_must_be_a_documented_multiple() {
    let plan = plan(VideoMode::Long, 10);
    assert_eq!(plan.chunk_size, 8 * 1024 * 1024);
    assert_eq!(plan.chunk_count(), 1);

    let refused = plan.clone().with_chunk_size(1000).expect_err("unaligned");
    assert!(refused.to_string().contains("multiple"), "{refused}");

    assert!(plan.clone().with_chunk_size(0).is_err());
    assert!(plan.with_chunk_size(512 * 1024).is_ok());
}

#[test]
fn privacy_parsing_is_exact() {
    assert_eq!(Privacy::parse("UNLISTED"), Some(Privacy::Unlisted));
    assert_eq!(Privacy::parse(" private "), Some(Privacy::Private));
    assert_eq!(Privacy::parse("secret"), None);
}

#[test]
fn caption_metadata_matches_the_adapters_shape() {
    let metadata =
        VideoMetadata::from_caption("My video title\nA description #tech #AI", VideoMode::Shorts)
            .expect("caption fits");
    assert_eq!(metadata.title, "My video title #Shorts");
    assert_eq!(metadata.description, "A description #tech #AI");
    assert_eq!(metadata.category_id, "28");
    assert_eq!(metadata.privacy, Privacy::Public);
    assert_eq!(metadata.tags[0], "tech");
    assert_eq!(metadata.tags.len(), 4, "tags: {:?}", metadata.tags);

    let already_tagged =
        VideoMetadata::from_caption("hi #shorts", VideoMode::Shorts).expect("caption fits");
    assert_eq!(already_tagged.title, "hi #shorts", "never doubled");

    let long = VideoMetadata::from_caption("x", VideoMode::Long).expect("caption fits");
    assert_eq!(long.title, "x");
}

#[test]
fn post_urls_are_mode_specific() {
    assert_eq!(
        post_url("abc", VideoMode::Shorts),
        "https://youtube.com/shorts/abc"
    );
    assert_eq!(
        post_url("abc", VideoMode::Long),
        "https://www.youtube.com/watch?v=abc"
    );
}

#[test]
fn a_failure_renders_with_its_code() {
    let failure = UploadFailure {
        class: xpst_provider_youtube::upload::FailureClass::Permanent,
        code: CODE_QUOTA_EXCEEDED,
        message: "spent".to_owned(),
        status: Some(403),
        retry_after: None,
        requests: 2,
    };
    assert_eq!(failure.to_string(), "YOUTUBE_QUOTA_EXCEEDED: spent");
}

// ---------------------------------------------------------------------------
// Over-length metadata: refused with numbers, never truncated
// ---------------------------------------------------------------------------

fn refusal_of(caption: &str, mode: VideoMode) -> MetadataRefusal {
    VideoMetadata::from_caption(caption, mode).expect_err("the caption must be refused")
}

#[test]
fn an_over_length_title_is_refused_with_the_measured_length_and_the_limit() {
    let refusal = refusal_of(&"x".repeat(MAX_TITLE_CHARS + 1), VideoMode::Long);

    assert_eq!(refusal.code(), CODE_TITLE_TOO_LONG);
    assert_eq!(refusal.field(), "snippet.title");
    assert_eq!(
        refusal.measured(),
        101,
        "the measured length, not a summary"
    );
    assert_eq!(refusal.limit(), MAX_TITLE_CHARS);
    assert!(!refusal.retryable());
    assert!(refusal.message().contains("101"), "{}", refusal.message());
    assert!(refusal.message().contains("100"), "{}", refusal.message());
    assert_eq!(
        refusal.to_string().split(':').next(),
        Some(CODE_TITLE_TOO_LONG)
    );
}

#[test]
fn an_over_length_description_is_refused_with_the_measured_length_and_the_limit() {
    let caption = format!("a title\n{}", "d".repeat(MAX_DESCRIPTION_CHARS + 1));
    let refusal = refusal_of(&caption, VideoMode::Long);

    assert_eq!(refusal.code(), CODE_DESCRIPTION_TOO_LONG);
    assert_eq!(refusal.field(), "snippet.description");
    assert_eq!(refusal.measured(), MAX_DESCRIPTION_CHARS + 1);
    assert_eq!(refusal.limit(), MAX_DESCRIPTION_CHARS);
    assert!(refusal.message().contains("5001"), "{}", refusal.message());
    assert!(refusal.message().contains("5000"), "{}", refusal.message());
}

#[test]
fn too_many_tags_are_refused_rather_than_dropped_at_the_limit() {
    let hashtags: Vec<String> = (0..20).map(|index| format!("#tag{index}")).collect();
    let caption = format!("a title\n{}", hashtags.join(" "));

    let refusal = refusal_of(&caption, VideoMode::Long);

    assert_eq!(refusal.code(), CODE_TOO_MANY_TAGS);
    assert_eq!(refusal.field(), "snippet.tags");
    assert_eq!(refusal.measured(), 23, "20 hashtags plus the 3 base tags");
    assert_eq!(refusal.limit(), MAX_TAGS);
    assert!(refusal.message().contains("23"), "{}", refusal.message());
    assert!(refusal.message().contains("15"), "{}", refusal.message());
    assert!(
        refusal.message().contains("remove 8"),
        "the refusal says exactly how many to drop: {}",
        refusal.message()
    );
}

#[test]
fn the_shorts_marker_survives_at_the_maximum_title_length() {
    let longest = "x".repeat(MAX_TITLE_CHARS - SHORTS_MARKER.chars().count());
    let metadata =
        VideoMetadata::from_caption(&longest, VideoMode::Shorts).expect("90 characters fits");

    assert_eq!(
        metadata.title.chars().count(),
        MAX_TITLE_CHARS,
        "the marker takes the title to exactly the platform ceiling"
    );
    assert!(
        metadata.title.ends_with(SHORTS_MARKER),
        "the classification marker is never the part that gets cut: {:?}",
        metadata.title
    );
    assert!(
        metadata.title.starts_with(&longest),
        "and the user's words are untouched"
    );

    // One character more is refused rather than shipped without the marker.
    let refusal = refusal_of(&"x".repeat(MAX_TITLE_CHARS), VideoMode::Shorts);
    assert_eq!(refusal.measured(), MAX_TITLE_CHARS);
    assert_eq!(
        refusal.limit(),
        MAX_TITLE_CHARS - SHORTS_MARKER.chars().count(),
        "the Shorts budget is the platform limit minus the reserved marker"
    );
    assert!(
        refusal.message().contains(SHORTS_MARKER),
        "the refusal says what the reservation is for: {}",
        refusal.message()
    );
}

#[test]
fn over_length_metadata_is_refused_at_the_plan_and_again_before_sending() {
    let over_long = VideoMetadata {
        title: "x".repeat(MAX_TITLE_CHARS + 1),
        description: String::from("a description"),
        tags: Vec::new(),
        category_id: "28".to_owned(),
        privacy: Privacy::Public,
        made_for_kids: false,
    };
    match UploadPlan::new(VideoMode::Long, over_long, 1024) {
        Err(PlanError::Metadata(refusal)) => {
            assert_eq!(refusal.code(), CODE_TITLE_TOO_LONG);
            assert_eq!(refusal.measured(), 101);
        }
        other => panic!("expected a metadata refusal, got {other:?}"),
    }

    // The fields are public, so the driver re-checks instead of trusting a plan
    // that may have been edited after it was built.
    let mut plan = plan(VideoMode::Long, 1024);
    plan.metadata.title = "x".repeat(MAX_TITLE_CHARS + 1);

    let transport = FakeTransport::scripted(vec![initiated(), created("never000001")]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(1024);
    let uploader = Uploader::new(&transport, &token, &sleeper);

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("refused");
    assert_eq!(failure.code, CODE_TITLE_TOO_LONG);
    assert_eq!(failure.requests, 0, "a refusal spends no request");
    assert!(!failure.retryable());
    assert!(
        transport.requests().is_empty(),
        "nothing reached the transport"
    );
}

// ---------------------------------------------------------------------------
// No response means no fact: the finalise outcome is unconfirmed
// ---------------------------------------------------------------------------

#[test]
fn a_lost_response_to_the_finalise_request_is_unconfirmed_and_never_retried() {
    let total = SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        // Every byte is in; the session is still open.
        incomplete(Some("bytes=0-262143")),
        // The finalise request produces no response at all.
        Replay::Failure(TransportError::network("connection reset by peer")),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("unconfirmed");

    assert!(
        failure.unconfirmed(),
        "an attempt with no response is not a permanent failure: {failure:?}"
    );
    assert_eq!(failure.code, CODE_UPLOAD_UNCONFIRMED);
    assert!(
        !failure.retryable(),
        "retrying blind could publish a second video"
    );
    assert!(
        failure.status.is_none(),
        "no response arrived, so quoting a status would be a fabricated fact"
    );
    assert_ne!(
        failure.code, CODE_PROTOCOL_ERROR,
        "the old path reported a 308 it never received"
    );
    assert!(
        !failure.message.contains("308"),
        "the message must not invent an answer: {}",
        failure.message
    );
    assert!(
        failure.message.contains("unconfirmed") && failure.message.contains("read the channel"),
        "{}",
        failure.message
    );
    assert_eq!(
        transport.requests().len(),
        3,
        "initiation, the chunk, the finalise — and no automatic re-send"
    );
    assert!(
        sleeper.slept().is_empty(),
        "an unconfirmed outcome must not sleep-and-retry"
    );
}

#[test]
fn a_chunk_that_gets_no_response_is_still_retryable_because_nothing_was_finalised() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Failure(TransportError::network("connection reset")),
        incomplete(Some("bytes=0-262143")),
        created("retried0001"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("the chunk is re-sent");
    assert_eq!(outcome.video_id, "retried0001");
    assert_eq!(sleeper.slept().len(), 1);
}

// ---------------------------------------------------------------------------
// A lost session is never a blank cheque to re-upload the file
// ---------------------------------------------------------------------------

#[test]
fn a_404_after_an_unanswered_send_is_unconfirmed_rather_than_a_blind_re_upload() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        // This chunk's response never arrived, so it may have carried the last
        // byte and published the video.
        Replay::Failure(TransportError::network("connection reset")),
        // The retry finds the session gone.
        Replay::Response(HttpResponse::new(404)),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let uploader = Uploader::new(&transport, &token, &sleeper).with_policy(fast_policy());

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("unconfirmed");

    assert!(failure.unconfirmed(), "{failure:?}");
    assert_eq!(failure.code, CODE_UPLOAD_UNCONFIRMED);
    assert!(
        failure.message.contains("404"),
        "the status it did receive is named: {}",
        failure.message
    );
    assert!(
        failure.message.contains("262144"),
        "and how much had gone out: {}",
        failure.message
    );

    let requests = transport.requests();
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.method == Method::Post)
            .count(),
        1,
        "no second session was opened, so no byte was re-sent"
    );
    assert_eq!(requests.len(), 3);
    assert!(sleeper.slept().len() <= 1, "no retry loop around the loss");
}

#[test]
fn a_404_with_no_unanswered_send_restarts_because_nothing_can_have_published() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        // Every response was received and none of them was a completion, so no
        // request can have published anything.
        incomplete(Some("bytes=0-1023")),
        Replay::Response(HttpResponse::new(410)),
        initiated_again(),
        created("proven00001"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    // A reconciler that would refuse permission: it must not even be asked,
    // because the proof is local.
    let reconciler = ScriptedReconciler::new(ReconcileVerdict::Unknown {
        detail: "should not have been consulted".to_owned(),
    });
    let uploader = Uploader::new(&transport, &token, &sleeper)
        .with_reconciler(&reconciler)
        .with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("proven restart");
    assert_eq!(outcome.video_id, "proven00001");
    assert!(
        reconciler.asked().is_empty(),
        "the local proof is enough: {:?}",
        reconciler.asked()
    );

    let requests = transport.requests();
    assert_eq!(
        requests.last().map(|request| request.url.as_str()),
        Some("https://upload.example/videos?uploadType=resumable&upload_id=S2"),
        "the byte-bearing requests after the restart talk to the new session"
    );
}

#[test]
fn a_404_restarts_only_when_a_read_back_proves_nothing_was_published() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Failure(TransportError::network("connection reset")),
        Replay::Response(HttpResponse::new(404)),
        initiated_again(),
        created("afterprobing"),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let reconciler = ScriptedReconciler::new(ReconcileVerdict::NotPublished);
    let uploader = Uploader::new(&transport, &token, &sleeper)
        .with_reconciler(&reconciler)
        .with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("proven restart");
    assert_eq!(outcome.video_id, "afterprobing");
    assert_eq!(
        reconciler.asked(),
        vec![(SHORTS_CHUNK_BYTES, total, 404)],
        "the probe is asked with the bytes that went out and the status that ended the session"
    );
}

#[test]
fn a_read_back_that_finds_the_video_reports_it_instead_of_uploading_again() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Failure(TransportError::network("connection reset")),
        Replay::Response(HttpResponse::new(404)),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let reconciler = ScriptedReconciler::new(ReconcileVerdict::Published {
        video_id: "already0001".to_owned(),
    });
    let uploader = Uploader::new(&transport, &token, &sleeper)
        .with_reconciler(&reconciler)
        .with_policy(fast_policy());

    let outcome = block_on(uploader.upload(&media, &plan)).expect("already published");

    assert_eq!(outcome.video_id, "already0001");
    assert_eq!(outcome.url, "https://youtube.com/shorts/already0001");
    assert_eq!(outcome.mode, VideoMode::Shorts);
    assert_eq!(
        transport.requests().len(),
        3,
        "nothing was re-uploaded: the read-back settled it"
    );
}

#[test]
fn a_read_back_with_no_answer_is_reported_as_unconfirmed_with_its_reason() {
    let total = 2 * SHORTS_CHUNK_BYTES;
    let transport = FakeTransport::scripted(vec![
        initiated(),
        Replay::Failure(TransportError::network("connection reset")),
        Replay::Response(HttpResponse::new(410)),
    ]);
    let sleeper = RecordingSleeper::default();
    let token = valid_token();
    let media = MemoryMedia::zeroed(total);
    let plan = plan(VideoMode::Shorts, total);
    let reconciler = ScriptedReconciler::new(ReconcileVerdict::Unknown {
        detail: "videos.list returned 403".to_owned(),
    });
    let uploader = Uploader::new(&transport, &token, &sleeper)
        .with_reconciler(&reconciler)
        .with_policy(fast_policy());

    let failure = block_on(uploader.upload(&media, &plan)).expect_err("unconfirmed");

    assert!(failure.unconfirmed(), "{failure:?}");
    assert_eq!(failure.code, CODE_UPLOAD_UNCONFIRMED);
    assert!(
        failure.message.contains("videos.list returned 403"),
        "the reason the read-back failed is carried: {}",
        failure.message
    );
    assert!(
        failure.message.contains("410") && failure.message.contains("262144"),
        "{}",
        failure.message
    );
    assert_eq!(
        transport.requests().len(),
        3,
        "an unknown outcome never becomes a second upload"
    );
}
