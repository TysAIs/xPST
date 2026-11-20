//! The publishing path, driven entirely through a fake transport.
//!
//! Nothing here touches the network: every request is captured as data and every
//! response is canned, so the assertions are about the protocol this crate
//! promises (request order, signed headers, priced refusals) rather than about
//! X's availability.
//!
//! The canned responses are modelled on what the live API actually answers,
//! which is the point of [`append_no_content`] returning `204` with a
//! zero-length body: a fake that answers `200 {"media_id":…}` to `APPEND`
//! cannot fail the way the real endpoint would, and hid exactly that bug.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

use serde_json::Value;
use xpst_provider_x::client::Clock;
use xpst_provider_x::{
    Body, CostCap, Credentials, DuplicatePolicy, Endpoints, Media, Method, PublicationLedger,
    PublishRequest, PublishedEntry, Request, Response, Transport, TransportError, Wait, XError,
    XPublisher, MAX_PROCESSING_POLLS, MEDIA_CHUNK_BYTES,
};

struct FakeTransport {
    requests: Vec<Request>,
    responses: VecDeque<Response>,
    /// Served when the script is exhausted, so a bounded poll loop can be
    /// exercised without listing the same response twenty times.
    fallback: Option<Response>,
}

impl FakeTransport {
    fn new(responses: Vec<Response>) -> Self {
        Self {
            requests: Vec::new(),
            responses: responses.into(),
            fallback: None,
        }
    }

    fn repeating(mut self, response: Response) -> Self {
        self.fallback = Some(response);
        self
    }

    fn request_count(&self) -> usize {
        self.requests.len()
    }

    fn urls(&self) -> Vec<&str> {
        self.requests
            .iter()
            .map(|request| request.url.as_str())
            .collect()
    }
}

impl Transport for FakeTransport {
    fn send(&mut self, request: &Request) -> Result<Response, TransportError> {
        self.requests.push(request.clone());
        let response = match self.responses.pop_front().or_else(|| self.fallback.clone()) {
            Some(response) => response,
            None => {
                return Err(TransportError::Io(
                    "fake transport ran out of canned responses".into(),
                ))
            }
        };

        // Every canned response is checked against the endpoint's documented
        // shape. This is the test the X 204 class of bug needed: a fake that
        // answers a bodyless endpoint with a JSON object (or a JSON endpoint
        // with nothing) fails here, as a contradiction between the test and the
        // provider's own documentation.
        let shape = xpst_provider_x::shapes::shape_for(request).unwrap_or_else(|| {
            panic!(
                "{} {} has no documented response shape; add it to \
                 xpst_provider_x::shapes::ENDPOINT_SHAPES before faking a reply",
                request.method, request.url
            )
        });
        shape
            .accepts(response.status, &response.body)
            .unwrap_or_else(|violation| panic!("{violation}\n  request: {request:?}"));
        Ok(response)
    }
}

struct FixedClock(i64);

impl Clock for FixedClock {
    fn now_unix(&self) -> i64 {
        self.0
    }
}

/// Records the pauses the client asked for without spending them, so a test can
/// show that media processing was waited on instead of ignored.
#[derive(Clone, Default)]
struct RecordedWaits(Rc<RefCell<Vec<u64>>>);

impl RecordedWaits {
    fn as_wait(&self) -> Box<dyn Wait> {
        Box::new(self.clone())
    }

    fn pauses(&self) -> Vec<u64> {
        self.0.borrow().clone()
    }
}

impl Wait for RecordedWaits {
    fn pause(&mut self, seconds: u64) {
        self.0.borrow_mut().push(seconds);
    }
}

fn credentials() -> Credentials {
    Credentials::new("ck-test", "cs-test", "at-test", "as-test")
}

fn publisher(responses: Vec<Response>) -> XPublisher<FakeTransport> {
    publisher_with(FakeTransport::new(responses))
}

fn publisher_with(transport: FakeTransport) -> XPublisher<FakeTransport> {
    XPublisher::new(transport, credentials()).with_endpoints(Endpoints::new(
        "https://api.test/2",
        "https://upload.test/1.1",
    ))
}

fn created(id: &str) -> Response {
    // POST /2/tweets answers 201 Created.
    Response::new(201, format!(r#"{{"data":{{"id":"{id}","text":"x"}}}}"#))
}

fn media_init() -> Response {
    // INIT answers 202 Accepted with the media id; it is not a 204.
    Response::new(
        202,
        r#"{"media_id":123,"media_id_string":"123","expires_after_secs":86400}"#,
    )
}

/// What `command=APPEND` really returns: `204 No Content` and no body bytes.
fn append_no_content() -> Response {
    Response::new(204, "")
}

fn finalize_ready() -> Response {
    Response::new(
        200,
        r#"{"media_id":123,"media_id_string":"123","processing_info":{"state":"succeeded"}}"#,
    )
}

fn finalize_pending() -> Response {
    Response::new(
        200,
        r#"{"media_id":123,"media_id_string":"123","processing_info":{"state":"pending","check_after_secs":5,"progress_percent":0}}"#,
    )
}

/// FINALIZE with no `processing_info` at all — not the same as "ready".
fn finalize_without_processing_info() -> Response {
    Response::new(200, r#"{"media_id":123,"media_id_string":"123"}"#)
}

/// X's documented answer to `APPEND`: 204, no body.
fn media_ack() -> Response {
    Response::new(204, "")
}

/// FINALIZE does answer with the media id, so this one keeps a body.
fn media_finalized() -> Response {
    Response::new(200, r#"{"media_id":123,"media_id_string":"123"}"#)
}

/// A transport that returns canned responses without the shape validation —
/// used only by tests that deliberately send an undocumented reply.
struct RawTransport {
    responses: std::collections::VecDeque<Response>,
    answered: usize,
}

impl RawTransport {
    fn new(responses: Vec<Response>) -> Self {
        Self {
            responses: std::collections::VecDeque::from(responses),
            answered: 0,
        }
    }

    fn request_count(&self) -> usize {
        // Only used to assert how many requests the handler made before
        // giving up, so counting what was answered is enough.
        self.answered
    }
}

impl Transport for RawTransport {
    fn send(&mut self, _request: &Request) -> Result<Response, TransportError> {
        let response = self
            .responses
            .pop_front()
            .ok_or_else(|| TransportError::Io("raw transport ran out of responses".into()))?;
        self.answered += 1;
        Ok(response)
    }
}

fn media_status(state: &str) -> Response {
    Response::new(
        200,
        format!(
            r#"{{"media_id":123,"media_id_string":"123","processing_info":{{"state":"{state}","check_after_secs":2,"progress_percent":50}}}}"#
        ),
    )
}

fn media_status_without_processing_info() -> Response {
    Response::new(200, r#"{"media_id":123,"media_id_string":"123"}"#)
}

#[test]
fn text_post_is_signed_and_sent_verbatim() {
    let mut x = publisher(vec![created("111")]);
    let request = PublishRequest::text("acct-a", "shipping the rust rebuild");

    let published = x.publish(&request).expect("text post should publish");

    assert_eq!(published.post_id, "111");
    assert_eq!(published.url, "https://x.com/i/status/111");
    assert_eq!(published.cost.micros, 15_000);

    let transport = x.into_transport();
    assert_eq!(transport.request_count(), 1);
    let sent = &transport.requests[0];
    assert_eq!(sent.url, "https://api.test/2/tweets");
    let Body::Json(bytes) = &sent.body else {
        panic!("text post must be a JSON body");
    };
    let payload: Value = serde_json::from_slice(bytes).expect("post body is JSON");
    assert_eq!(payload["text"], "shipping the rust rebuild");
    assert!(payload.get("media").is_none());

    let authorization = sent.header_value("Authorization").expect("signed");
    assert!(authorization.starts_with("OAuth "));
    assert!(authorization.contains("\"oauth_signature_method\"=\"HMAC-SHA1\""));
    assert!(authorization.contains("\"oauth_signature\"=\""));
}

#[test]
fn media_post_uploads_chunks_in_order_then_posts() {
    let total = MEDIA_CHUNK_BYTES * 2 + 10;
    let media = Media::video_bytes(vec![7_u8; total]);
    let mut x = publisher(vec![
        media_init(),
        append_no_content(),
        append_no_content(),
        append_no_content(),
        finalize_ready(),
        created("222"),
    ]);
    let request = PublishRequest::text("acct-a", "clip")
        .with_media(media)
        .with_asset_key("sha256:clip-1");

    let published = x.publish(&request).expect("media post should publish");
    assert_eq!(published.post_id, "222");
    assert_eq!(published.asset_key.as_deref(), Some("sha256:clip-1"));

    let transport = x.into_transport();
    let requests = &transport.requests;
    assert_eq!(requests.len(), 6, "INIT, 3 APPEND, FINALIZE, POST");

    let Body::Form(init) = &requests[0].body else {
        panic!("INIT must be a form body");
    };
    assert_eq!(field(init, "command"), Some("INIT"));
    let expected_total = total.to_string();
    assert_eq!(field(init, "total_bytes"), Some(expected_total.as_str()));
    assert_eq!(field(init, "media_type"), Some("video/mp4"));

    let expected_segments = [MEDIA_CHUNK_BYTES, MEDIA_CHUNK_BYTES, 10];
    for (index, expected_len) in expected_segments.iter().enumerate() {
        let Body::Multipart(part) = &requests[1 + index].body else {
            panic!("APPEND must be multipart");
        };
        assert_eq!(field(&part.fields, "command"), Some("APPEND"));
        assert_eq!(field(&part.fields, "media_id"), Some("123"));
        let expected_index = index.to_string();
        assert_eq!(
            field(&part.fields, "segment_index"),
            Some(expected_index.as_str())
        );
        assert_eq!(part.bytes.len(), *expected_len, "segment {index} size");
        assert_eq!(part.file_field, "media");
    }

    let Body::Form(finalize) = &requests[4].body else {
        panic!("FINALIZE must be a form body");
    };
    assert_eq!(field(finalize, "command"), Some("FINALIZE"));
    assert_eq!(field(finalize, "media_id"), Some("123"));

    let Body::Json(bytes) = &requests[5].body else {
        panic!("post creation must be JSON");
    };
    let payload: Value = serde_json::from_slice(bytes).expect("post body is JSON");
    assert_eq!(payload["media"]["media_ids"][0], "123");
    assert_eq!(payload["text"], "clip");

    for request in requests {
        assert!(
            request
                .header_value("Authorization")
                .is_some_and(|value| value.starts_with("OAuth ")),
            "every request must be OAuth 1.0a signed"
        );
    }
}

/// The regression test for the audit's C4 finding.
///
/// `APPEND` answers `204 No Content` with an empty body. A client that insists
/// on parsing JSON from every 2xx fails on the first segment against the live
/// API, so no media post can ever be published — which is exactly what the
/// previous fake (`200` + a JSON body) could not reveal.
#[test]
fn an_append_returning_204_with_no_body_is_a_success() {
    let mut x = publisher(vec![
        media_init(),
        append_no_content(),
        finalize_ready(),
        created("333"),
    ]);
    let request = PublishRequest::text("acct-a", "one segment")
        .with_media(Media::video_bytes(vec![3_u8; 16]))
        .with_asset_key("sha256:clip-204");

    let published = x
        .publish(&request)
        .expect("a 204 from APPEND must not be an error");

    assert_eq!(published.post_id, "333");
    let transport = x.into_transport();
    assert_eq!(
        transport.urls(),
        vec![
            "https://upload.test/1.1/media/upload.json",
            "https://upload.test/1.1/media/upload.json",
            "https://upload.test/1.1/media/upload.json",
            "https://api.test/2/tweets",
        ],
        "INIT, one APPEND, FINALIZE, POST"
    );
    let Body::Multipart(part) = &transport.requests[1].body else {
        panic!("APPEND must be multipart");
    };
    assert_eq!(field(&part.fields, "command"), Some("APPEND"));
    assert_eq!(transport.requests[1].method, Method::Post);
}

/// The mirror image of the 204 case: a command endpoint that does answer with
/// JSON must still be read, and a 2xx `APPEND` that is *not* 204 with no body is
/// still judged on its status.
#[test]
fn an_append_that_fails_is_still_an_error() {
    let mut x = publisher(vec![
        media_init(),
        Response::new(400, r#"{"errors":[{"code":324,"message":"bad segment"}]}"#),
    ]);
    let request = PublishRequest::text("acct-a", "one segment")
        .with_media(Media::video_bytes(vec![3_u8; 16]))
        .with_asset_key("sha256:clip-400");

    let error = x
        .publish(&request)
        .expect_err("a 400 from APPEND must not be swallowed");

    assert_eq!(error.code(), "X_API_ERROR");
    assert!(matches!(error, XError::Api { status: 400, .. }));
    assert_eq!(
        x.into_transport().request_count(),
        2,
        "no FINALIZE after a failed APPEND"
    );
}

#[test]
fn a_finalize_returning_json_is_read_before_posting() {
    let mut x = publisher(vec![
        media_init(),
        append_no_content(),
        finalize_ready(),
        created("444"),
    ]);
    let request = PublishRequest::text("acct-a", "finalize json")
        .with_media(Media::video_bytes(vec![4_u8; 8]))
        .with_asset_key("sha256:clip-json");

    let published = x.publish(&request).expect("succeeded media may post");
    assert_eq!(published.post_id, "444");

    let transport = x.into_transport();
    assert_eq!(transport.request_count(), 4);
    let Body::Form(finalize) = &transport.requests[2].body else {
        panic!("FINALIZE must be a form body");
    };
    assert_eq!(field(finalize, "command"), Some("FINALIZE"));
    // No STATUS poll: FINALIZE already said `succeeded`.
    assert!(
        transport
            .requests
            .iter()
            .all(|request| !request.url.contains("command=STATUS")),
        "an explicit succeeded state needs no status poll"
    );
}

#[test]
fn a_pending_processing_state_waits_and_polls_status_before_posting() {
    let waits = RecordedWaits::default();
    let mut x = publisher_with(FakeTransport::new(vec![
        media_init(),
        append_no_content(),
        finalize_pending(),
        media_status("in_progress"),
        media_status("succeeded"),
        created("555"),
    ]))
    .with_wait(waits.as_wait());
    let request = PublishRequest::text("acct-a", "still processing")
        .with_media(Media::video_bytes(vec![5_u8; 8]))
        .with_asset_key("sha256:clip-pending");

    let published = x
        .publish(&request)
        .expect("the media became ready inside the bound");

    assert_eq!(published.post_id, "555");
    assert_eq!(
        waits.pauses(),
        vec![5, 2],
        "the client waits what FINALIZE and STATUS ask for, then stops"
    );

    let transport = x.into_transport();
    assert_eq!(transport.request_count(), 6);
    let statuses: Vec<&Request> = transport
        .requests
        .iter()
        .filter(|request| request.url.contains("command=STATUS"))
        .collect();
    assert_eq!(statuses.len(), 2, "pending → in_progress → succeeded");
    for status in statuses {
        assert_eq!(status.method, Method::Get);
        assert!(status
            .url
            .starts_with("https://upload.test/1.1/media/upload.json?command=STATUS&media_id=123"));
        assert!(
            status
                .header_value("Authorization")
                .is_some_and(|value| value.starts_with("OAuth ")),
            "the STATUS query string is part of the signed parameter set"
        );
    }
    assert_eq!(
        transport
            .requests
            .last()
            .map(|request| request.url.as_str()),
        Some("https://api.test/2/tweets"),
        "the post is only sent after the media was confirmed"
    );
}

#[test]
fn processing_that_never_succeeds_is_bounded_and_never_posts() {
    let waits = RecordedWaits::default();
    let mut x = publisher_with(
        FakeTransport::new(vec![media_init(), append_no_content(), finalize_pending()])
            .repeating(media_status("pending")),
    )
    .with_wait(waits.as_wait());
    let request = PublishRequest::text("acct-a", "never ready")
        .with_media(Media::video_bytes(vec![6_u8; 8]))
        .with_asset_key("sha256:clip-stuck");

    let error = x
        .publish(&request)
        .expect_err("unprocessed media must not be posted");

    assert_eq!(error.code(), "X_MEDIA_UNCONFIRMED");
    match &error {
        XError::MediaProcessingUnconfirmed {
            state,
            polls,
            media_id,
        } => {
            assert_eq!(state, "pending");
            assert_eq!(*polls, MAX_PROCESSING_POLLS);
            assert_eq!(media_id, "123");
        }
        other => panic!("wrong error: {other:?}"),
    }
    assert!(!error.retryable());

    assert_eq!(
        waits.pauses().len(),
        MAX_PROCESSING_POLLS,
        "the wait is bounded, not unbounded"
    );
    let transport = x.into_transport();
    assert_eq!(
        transport.request_count(),
        3 + MAX_PROCESSING_POLLS,
        "INIT, APPEND, FINALIZE and the bounded STATUS polls — never a post"
    );
    assert!(
        transport
            .requests
            .iter()
            .all(|request| request.url != "https://api.test/2/tweets"),
        "nothing is published while the media is unconfirmed"
    );
}

#[test]
fn a_missing_processing_info_is_unconfirmed_and_never_posts() {
    let waits = RecordedWaits::default();
    let mut x = publisher_with(
        FakeTransport::new(vec![
            media_init(),
            append_no_content(),
            finalize_without_processing_info(),
        ])
        .repeating(media_status_without_processing_info()),
    )
    .with_wait(waits.as_wait());
    let request = PublishRequest::text("acct-a", "no processing info")
        .with_media(Media::video_bytes(vec![7_u8; 8]))
        .with_asset_key("sha256:clip-absent");

    let error = x
        .publish(&request)
        .expect_err("a missing processing_info is not a ready signal");

    assert_eq!(error.code(), "X_MEDIA_UNCONFIRMED");
    match error {
        XError::MediaProcessingUnconfirmed { state, polls, .. } => {
            assert_eq!(state, "absent", "the absence itself is reported");
            assert_eq!(polls, MAX_PROCESSING_POLLS);
        }
        other => panic!("wrong error: {other:?}"),
    }
    let transport = x.into_transport();
    assert!(
        transport
            .requests
            .iter()
            .all(|request| request.url != "https://api.test/2/tweets"),
        "a missing processing_info must never publish immediately"
    );
}

#[test]
fn a_failed_processing_state_is_reported_without_polling() {
    let waits = RecordedWaits::default();
    let mut x = publisher_with(
        FakeTransport::new(vec![
            media_init(),
            append_no_content(),
            Response::new(
                200,
                r#"{"media_id":123,"media_id_string":"123","processing_info":{"state":"failed","error_code":1,"error_name":"InvalidMedia"}}"#,
            ),
        ])
        .repeating(media_status("succeeded")),
    )
    .with_wait(waits.as_wait());
    let request = PublishRequest::text("acct-a", "bad media")
        .with_media(Media::video_bytes(vec![8_u8; 8]))
        .with_asset_key("sha256:clip-failed");

    let error = x.publish(&request).expect_err("failed media is terminal");

    assert_eq!(error.code(), "X_API_ERROR");
    assert!(
        error.to_string().contains("InvalidMedia"),
        "X's own reason is carried through: {error}"
    );
    assert!(waits.pauses().is_empty(), "failed needs no wait");
    assert_eq!(
        x.into_transport().request_count(),
        3,
        "no STATUS poll after a terminal failure"
    );
}

/// The other half of the same bug class: an empty body from an endpoint whose
/// contract *is* JSON must not be read as success.
#[test]
fn an_empty_body_on_a_json_endpoint_is_not_a_success() {
    // Deliberately contradicts the documented shape (INIT is a JSON endpoint),
    // so it drives a transport that does not enforce the registry.
    let mut x = XPublisher::new(
        RawTransport::new(vec![Response::new(202, "")]),
        credentials(),
    )
    .with_endpoints(Endpoints::new(
        "https://api.test/2",
        "https://upload.test/1.1",
    ));
    let request = PublishRequest::text("acct-a", "clip")
        .with_media(Media::video_bytes(vec![9_u8; 4]))
        .with_asset_key("sha256:clip-empty-init");

    let error = x
        .publish(&request)
        .expect_err("INIT with no body cannot yield a media id");

    assert_eq!(error.code(), "X_API_ERROR");
    assert!(matches!(error, XError::Api { status: 202, .. }));
    assert_eq!(x.into_transport().request_count(), 1);
}

#[test]
fn an_unreadable_body_on_a_json_endpoint_is_not_a_success() {
    let mut x = publisher(vec![Response::new(201, "<html>proxy error</html>")]);

    let error = x
        .publish(&PublishRequest::text("acct-a", "clip"))
        .expect_err("an unparseable body is not a post id");

    assert_eq!(error.code(), "X_API_ERROR");
    assert!(error.to_string().contains("unreadable response body"));
}

#[test]
fn url_post_is_flagged_as_the_costlier_tier_without_touching_it() {
    let x = publisher(vec![]);
    let request = PublishRequest::text("acct-a", "read this https://example.com/rust");

    let report = x.preflight(std::slice::from_ref(&request));

    assert!(
        report.is_fully_publishable(),
        "no cap was set, so nothing may be refused"
    );
    let plan = &report.plans[0];
    assert!(plan.has_url);
    assert_eq!(plan.cost.micros, 200_000);
    assert_eq!(plan.cost.usd(), "$0.200");
    assert_eq!(plan.cost.multiplier_tenths(), 133);
    assert_eq!(
        request.text, "read this https://example.com/rust",
        "preflight must never edit the text"
    );
    assert_eq!(x.into_transport().request_count(), 0);
}

#[test]
fn cost_cap_refuses_the_expensive_post_and_keeps_the_link() {
    let x = publisher(vec![]).with_cost_cap(CostCap::from_micros(50_000));
    let requests = vec![
        PublishRequest::text("acct-a", "plain update"),
        PublishRequest::text("acct-a", "the link is https://example.com/x"),
    ];

    let report = x.preflight(&requests);

    assert!(report.plans[0].decision.is_publish());
    let refusal = report.plans[1].refusal().expect("URL post exceeds the cap");
    assert_eq!(refusal.code(), "X_COST_CAP_EXCEEDED");
    assert!(matches!(
        refusal.to_error(),
        XError::CostCapExceeded {
            planned_micros: 215_000,
            cap_micros: 50_000
        }
    ));
    assert_eq!(requests[1].text, "the link is https://example.com/x");
    assert_eq!(report.total_micros, 15_000, "a refused post is not charged");
}

#[test]
fn the_same_asset_on_a_second_account_is_refused_before_any_request() {
    let ledger = PublicationLedger::from_entries(vec![PublishedEntry::new(
        "sha256:clip-1",
        "acct-a",
        1_700_000_000,
        Some("111".to_string()),
    )]);
    let mut x = publisher(vec![media_init(), append_no_content(), finalize_ready()])
        .with_ledger(ledger)
        .with_clock(Box::new(FixedClock(1_700_000_000 + 86_400)));

    let request = PublishRequest::text("acct-b", "the same clip")
        .with_media(Media::video_bytes(vec![1_u8, 2, 3]))
        .with_asset_key("sha256:clip-1");

    let error = x
        .publish(&request)
        .expect_err("second account must be refused");

    assert_eq!(error.code(), "X_DUPLICATE_ACROSS_ACCOUNTS");
    assert!(!error.retryable());
    match &error {
        XError::DuplicateAcrossAccounts {
            other_account,
            asset_key,
            ..
        } => {
            assert_eq!(other_account, "acct-a");
            assert_eq!(asset_key, "sha256:clip-1");
        }
        other => panic!("wrong error: {other:?}"),
    }
    assert_eq!(
        x.into_transport().request_count(),
        0,
        "the refusal must happen before INIT — no bytes, no money"
    );
}

#[test]
fn the_documented_duplicate_window_has_no_expiry() {
    let ledger = PublicationLedger::from_entries(vec![PublishedEntry::new(
        "sha256:clip-1",
        "acct-a",
        1_600_000_000,
        None,
    )]);
    let ten_years_later = 1_600_000_000 + 10 * 365 * 86_400;
    let mut x = publisher(vec![])
        .with_ledger(ledger)
        .with_clock(Box::new(FixedClock(ten_years_later)));

    let request = PublishRequest::text("acct-b", "same asset")
        .with_media(Media::video_bytes(vec![1_u8]))
        .with_asset_key("sha256:clip-1");

    let error = x
        .publish(&request)
        .expect_err("the documented rule has no time bound");
    assert_eq!(error.code(), "X_DUPLICATE_ACROSS_ACCOUNTS");

    // A caller that deliberately weakens the policy gets the finite behaviour.
    let finite = publisher(vec![])
        .with_duplicate_policy(DuplicatePolicy::within(7 * 86_400))
        .with_ledger(PublicationLedger::from_entries(vec![PublishedEntry::new(
            "sha256:clip-1",
            "acct-a",
            1_600_000_000,
            None,
        )]))
        .with_clock(Box::new(FixedClock(ten_years_later)));
    assert!(finite
        .preflight(std::slice::from_ref(&request))
        .is_fully_publishable());
}

#[test]
fn over_length_text_is_refused_with_the_measured_count() {
    let text = "a".repeat(281);
    let mut x = publisher(vec![]);
    let request = PublishRequest::text("acct-a", &text);

    let error = x
        .publish(&request)
        .expect_err("281 characters is over the limit");

    assert_eq!(error.code(), "X_TEXT_TOO_LONG");
    match error {
        XError::TextTooLong { measured, limit } => {
            assert_eq!(measured, 281);
            assert_eq!(limit, 280);
        }
        other => panic!("wrong error: {other:?}"),
    }
    assert_eq!(x.into_transport().request_count(), 0);
    assert_eq!(
        request.text.chars().count(),
        281,
        "refused, never truncated"
    );
}

/// The X 204 bug class, pinned.
///
/// The media upload's `APPEND` step answers 204 with no body. A fake that
/// replayed `200 {"media_id": …}` there let the whole suite pass while the real
/// handler tried to parse an empty body and failed against live X. These tests
/// assert the contract from both ends: the bodyless success is accepted by the
/// real handler, and a bodyless success on a JSON endpoint is never reported as
/// a publish.
#[test]
fn a_bodyless_append_is_success_and_the_registry_says_so() {
    let media = Media::video_bytes(vec![1_u8; 32]);
    let mut x = publisher(vec![
        media_init(),
        media_ack(),
        media_finalized(),
        media_status("succeeded"),
        created("111"),
    ]);

    let request = PublishRequest::text("acct-a", "clip")
        .with_media(media)
        .with_asset_key("sha256:clip-bodyless");

    let published = x.publish(&request).expect("a 204 APPEND must publish");
    assert_eq!(published.post_id, "111");

    let transport = x.into_transport();
    let append = transport
        .requests
        .iter()
        .find(|request| request.command() == Some("APPEND"))
        .expect("the upload must APPEND");
    let shape = xpst_provider_x::shapes::shape_for(append).expect("APPEND is documented");
    assert_eq!(
        shape.body,
        xpst_provider::BodyShape::Empty,
        "APPEND's documented success carries no body"
    );
    assert!(
        shape.success_statuses.contains(&204),
        "204 is the documented answer: {:?}",
        shape.success_statuses
    );
}

#[test]
fn a_bodyless_success_on_the_tweet_endpoint_is_not_a_publish() {
    // A 204 from create-post would mean X accepted nothing we can identify.
    // Reporting success anyway is the same class of lie as the APPEND bug, so
    // the handler is driven directly, past the shape-validating fake.
    let mut x = XPublisher::new(
        RawTransport::new(vec![Response::new(204, "")]),
        credentials(),
    )
    .with_endpoints(Endpoints::new(
        "https://api.test/2",
        "https://upload.test/1.1",
    ));

    let error = x
        .publish(&PublishRequest::text("acct-a", "hello"))
        .expect_err("no post id means no post");

    assert_eq!(error.code(), "X_API_ERROR");
    assert!(
        error.to_string().contains("contract is JSON"),
        "the error must say what was missing: {error}"
    );
}

#[test]
fn every_request_the_publisher_makes_has_a_documented_response_shape() {
    let media = Media::video_bytes(vec![9_u8; 16]);
    let mut x = publisher(vec![
        media_init(),
        media_ack(),
        media_finalized(),
        media_status("succeeded"),
        created("333"),
    ]);
    x.publish(
        &PublishRequest::text("acct-a", "clip")
            .with_media(media)
            .with_asset_key("sha256:clip-shapes"),
    )
    .expect("published");

    let transport = x.into_transport();
    assert!(!transport.requests.is_empty(), "nothing was sent");

    let mut seen = std::collections::BTreeSet::new();
    for request in &transport.requests {
        let shape = xpst_provider_x::shapes::shape_for(request).unwrap_or_else(|| {
            panic!(
                "{} {} has no documented response shape",
                request.method, request.url
            )
        });
        seen.insert(shape.endpoint);
    }
    assert_eq!(
        seen.len(),
        xpst_provider_x::shapes::ENDPOINT_SHAPES.len(),
        "every documented endpoint must be exercised: seen {seen:?}"
    );
}

#[test]
fn a_429_reports_the_reset_time_and_is_retryable() {
    let response = Response::new(429, r#"{"title":"Too Many Requests"}"#)
        .with_header("x-rate-limit-reset", "1780003600")
        .with_header("retry-after", "900");
    let mut x = publisher(vec![response]);

    let error = x
        .publish(&PublishRequest::text("acct-a", "one more"))
        .expect_err("429 must not be success");

    assert_eq!(error.code(), "X_RATE_LIMITED");
    assert_eq!(error.rate_limit_reset(), Some(1_780_003_600));
    assert!(error.retryable(), "a rate limit is the one automatic retry");
    assert!(matches!(
        error,
        XError::RateLimited {
            retry_after_seconds: Some(900),
            ..
        }
    ));
}

#[test]
fn a_401_is_needs_reauth_not_a_retry() {
    let mut x = publisher(vec![Response::new(401, "Unauthorized")]);

    let error = x
        .publish(&PublishRequest::text("acct-a", "hello"))
        .expect_err("401 must be an auth answer");

    assert_eq!(error.code(), "X_NEEDS_REAUTH");
    assert!(error.needs_reauth());
    assert!(!error.retryable());
}

#[test]
fn a_duplicate_rejection_is_reported_as_a_duplicate_not_success() {
    let body =
        r#"{"detail":"Forbidden","errors":[{"code":187,"message":"Status is a duplicate."}]}"#;
    let mut x = publisher(vec![Response::new(403, body)]);

    let error = x
        .publish(&PublishRequest::text("acct-a", "same text as last time"))
        .expect_err("X refused the duplicate");

    assert_eq!(error.code(), "X_DUPLICATE_POST");
    assert!(!error.retryable());
}

#[test]
fn media_without_an_asset_key_fails_closed() {
    let mut x = publisher(vec![media_init()]);
    let request =
        PublishRequest::text("acct-a", "no key").with_media(Media::video_bytes(vec![9_u8]));

    let error = x.publish(&request).expect_err("media identity is required");

    assert_eq!(error.code(), "X_ASSET_KEY_REQUIRED");
    assert_eq!(x.into_transport().request_count(), 0);
}

fn field<'a>(fields: &'a [(String, String)], name: &str) -> Option<&'a str> {
    fields
        .iter()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value.as_str())
}
