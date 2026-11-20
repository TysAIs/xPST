//! The live HTTP transport: the crate's [`Transport`] seam over `reqwest`.
//!
//! Everything in this module is mapping. The crate's [`Request`] already
//! carries the signed `Authorization` header, the exact URL and the body
//! [`Body`] — this turns that into bytes and turns the answer back into a
//! [`Response`]. The exchange itself is [`BlockingHttpAdapter`]'s job, so the
//! tests run the whole publishing path through this code with a recording
//! adapter and no socket.
//!
//! Four decisions belong to the transport rather than to the publisher:
//!
//! * **The multipart boundary.** [`Multipart`] states the intent
//!   (`multipart/form-data`, one binary part) and the transport is the only
//!   layer that can serialise it, so it is also the layer that appends
//!   `boundary=` to the `Content-Type`. A publisher that guessed a boundary
//!   would have to guess it identically in two places.
//! * **Form encoding.** X signs the form parameters as they are *sent*, so the
//!   encoding must be the exact RFC 3986 form encoding, not something a client
//!   library chose. [`xpst_provider::http::form_encode`] owns it.
//! * **No redirects and no retries.** A followed redirect re-sends a POST that
//!   may already have created media, and `reqwest` retries protocol NACKs by
//!   default. Both are disabled in the shared client policy, which is why this
//!   transport constructs its client through it rather than directly.
//! * **Nothing secret in a failure.** The `Authorization` header on these
//!   requests is an OAuth 1.0a signature over the request. It never reaches a
//!   [`TransportError`], and the URL is redacted before it does.
//!
//! [`Transport`]: crate::transport::Transport
//! [`TransportError`]: crate::transport::TransportError

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use xpst_provider::http::{
    form_encode, BlockingHttpAdapter, ClientConfig, HttpFailure, HttpFailureKind, HttpMethod,
    PreparedRequest, ReqwestBlockingAdapter,
};

use crate::transport::{Body, Method, Multipart, Request, Response, Transport, TransportError};

/// The media upload and tweet-creation path over a real HTTP client.
pub struct HttpTransport<A> {
    adapter: A,
}

impl<A> HttpTransport<A> {
    /// Wrap an adapter.
    pub fn new(adapter: A) -> Self {
        Self { adapter }
    }

    /// Borrow the adapter, so a test can read what was sent.
    pub fn adapter(&self) -> &A {
        &self.adapter
    }
}

impl HttpTransport<ReqwestBlockingAdapter> {
    /// The production transport.
    ///
    /// Blocking, because the publishing trait is blocking. It must be called
    /// from a blocking context — never from inside an async executor.
    pub fn live() -> Result<Self, TransportError> {
        Self::live_with(&ClientConfig::for_destination())
    }

    /// The production transport with an adjusted policy.
    ///
    /// The redirect policy is the one knob not to change casually: X reads
    /// `Location` after `INIT`, and a media `POST` that is followed after a
    /// redirect may arrive twice.
    pub fn live_with(config: &ClientConfig) -> Result<Self, TransportError> {
        let adapter = ReqwestBlockingAdapter::new(config).map_err(transport_error)?;
        Ok(Self::new(adapter))
    }
}

impl<A: BlockingHttpAdapter> Transport for HttpTransport<A> {
    fn send(&mut self, request: &Request) -> Result<Response, TransportError> {
        // One call is one request. There is no loop here on purpose: retrying a
        // non-idempotent POST is the pipeline's decision, taken with the outcome
        // of a read-back in hand.
        let prepared = prepare(request);
        let response = self.adapter.execute(prepared).map_err(transport_error)?;
        Ok(Response {
            status: response.status,
            headers: response.headers,
            body: response.body,
        })
    }
}

/// Materialise a [`Request`] into the exact bytes that go on the wire.
///
/// Public because it is the one piece of this module worth reading on its own:
/// the publisher's intent in, the wire's request out.
#[must_use]
pub fn prepare(request: &Request) -> PreparedRequest {
    let method = match request.method {
        Method::Get => HttpMethod::Get,
        Method::Post => HttpMethod::Post,
    };
    let mut prepared = PreparedRequest {
        method,
        url: request.url.clone(),
        headers: request.headers.clone(),
        body: Vec::new(),
    };

    match &request.body {
        Body::Empty => {}
        Body::Json(bytes) => {
            prepared.body = bytes.clone();
            prepared = ensure_content_type(prepared, "application/json");
        }
        Body::Form(fields) => {
            prepared.body = form_encode(fields).into_bytes();
            prepared = ensure_content_type(prepared, "application/x-www-form-urlencoded");
        }
        Body::Multipart(part) => {
            let boundary = boundary();
            prepared.body = encode_multipart(part, &boundary);
            prepared = set_multipart_content_type(prepared, &boundary);
        }
    }
    prepared
}

fn ensure_content_type(request: PreparedRequest, content_type: &str) -> PreparedRequest {
    if request.header("Content-Type").is_some() {
        request
    } else {
        request.with_header("Content-Type", content_type)
    }
}

/// Add `boundary=` to the request's `Content-Type`.
///
/// The publisher states `multipart/form-data` and the transport owns the
/// boundary, so this is where the two are joined. A `Content-Type` that already
/// names a boundary is respected rather than overwritten: only the caller that
/// built the bytes can name them consistently.
fn set_multipart_content_type(request: PreparedRequest, boundary: &str) -> PreparedRequest {
    let existing = request
        .header("Content-Type")
        .unwrap_or_default()
        .to_owned();
    let value = if existing.to_ascii_lowercase().contains("boundary=") {
        existing
    } else if existing.is_empty() {
        format!("multipart/form-data; boundary={boundary}")
    } else {
        format!("{existing}; boundary={boundary}")
    };
    let mut headers: Vec<(String, String)> = request
        .headers
        .iter()
        .filter(|(name, _)| !name.eq_ignore_ascii_case("Content-Type"))
        .cloned()
        .collect();
    headers.push(("Content-Type".to_owned(), value));
    PreparedRequest { headers, ..request }
}

/// A boundary unique to this request.
///
/// It has to be absent from the segment bytes, which no fixed string can
/// guarantee for arbitrary media; a nanosecond stamp plus a counter makes an
/// accidental match the same order of improbability as a UUID collision, without
/// adding a dependency.
fn boundary() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let sequence = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    format!("xpst-{nanos:032x}-{sequence:08x}")
}

/// Serialise one [`Multipart`] body (RFC 7578).
///
/// The fields come first, then the binary part, then the closing delimiter —
/// which is the order X's `APPEND` command expects.
fn encode_multipart(part: &Multipart, boundary: &str) -> Vec<u8> {
    let mut body = Vec::with_capacity(part.bytes.len() + 512);
    for (name, value) in &part.fields {
        body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        body.extend_from_slice(
            format!(
                "Content-Disposition: form-data; name=\"{}\"\r\n\r\n",
                quote(name)
            )
            .as_bytes(),
        );
        body.extend_from_slice(value.as_bytes());
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(
        format!(
            "Content-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n",
            quote(&part.file_field),
            quote(&part.file_name)
        )
        .as_bytes(),
    );
    body.extend_from_slice(format!("Content-Type: {}\r\n\r\n", part.content_type).as_bytes());
    body.extend_from_slice(&part.bytes);
    body.extend_from_slice(b"\r\n");
    body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    body
}

/// Quote a header parameter value, dropping the characters that could break out
/// of it. A media filename is attacker-adjacent input in the sense that it comes
/// from the user's filesystem.
fn quote(value: &str) -> String {
    let mut quoted = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '"' | '\\' => {
                quoted.push('\\');
                quoted.push(character);
            }
            '\r' | '\n' => quoted.push(' '),
            other => quoted.push(other),
        }
    }
    quoted
}

/// Translate the shared failure into this crate's vocabulary.
fn transport_error(failure: HttpFailure) -> TransportError {
    match failure.kind {
        HttpFailureKind::Timeout => TransportError::Timeout,
        // `Redirect` cannot arrive while the client follows no redirects; if it
        // ever does, something in front of X answered instead of X.
        HttpFailureKind::Redirect => TransportError::Blocked(failure.to_string()),
        _ => TransportError::Io(failure.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use xpst_provider::http::fake::{reply, RecordingAdapter};

    fn transport(adapter: RecordingAdapter) -> HttpTransport<RecordingAdapter> {
        HttpTransport::new(adapter)
    }

    #[test]
    fn a_json_post_carries_its_body_and_content_type() {
        let adapter =
            RecordingAdapter::scripted(vec![Ok(reply(201, br#"{"data":{"id":"9"}}"#.to_vec()))]);
        let mut transport = transport(adapter);
        let request = Request::new(Method::Post, "https://api.x.com/2/tweets")
            .header("Authorization", "OAuth oauth_signature=\"sig\"")
            .header("Content-Type", "application/json")
            .body(Body::Json(br#"{"text":"hello"}"#.to_vec()));

        let response = transport.send(&request).expect("the script answered");

        assert_eq!(response.status, 201);
        assert_eq!(response.body_text(), r#"{"data":{"id":"9"}}"#);
        let sent = transport.adapter().requests();
        assert_eq!(sent.len(), 1);
        assert_eq!(sent[0].method, HttpMethod::Post);
        assert_eq!(sent[0].url, "https://api.x.com/2/tweets");
        assert_eq!(
            sent[0].header("authorization"),
            Some("OAuth oauth_signature=\"sig\"")
        );
        assert_eq!(sent[0].header("content-type"), Some("application/json"));
        assert_eq!(sent[0].body, br#"{"text":"hello"}"#);
    }

    #[test]
    fn a_form_post_is_encoded_the_way_it_was_signed() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(200, b"{}".to_vec()))]);
        let mut transport = transport(adapter);
        let request = Request::new(Method::Post, "https://api.x.com/2/media/upload")
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(Body::Form(vec![
                ("command".to_owned(), "FINALIZE".to_owned()),
                ("media_id".to_owned(), "17 1".to_owned()),
            ]));

        transport.send(&request).expect("the script answered");

        let sent = transport.adapter().requests();
        assert_eq!(
            String::from_utf8_lossy(&sent[0].body),
            "command=FINALIZE&media_id=17+1"
        );
        assert_eq!(
            sent[0].header("content-type"),
            Some("application/x-www-form-urlencoded")
        );
    }

    #[test]
    fn a_multipart_append_states_a_boundary_its_body_actually_uses() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(204, Vec::new()))]);
        let mut transport = transport(adapter);
        let request = Request::new(Method::Post, "https://api.x.com/2/media/upload")
            .header("Content-Type", "multipart/form-data")
            .body(Body::Multipart(Multipart {
                fields: vec![
                    ("command".to_owned(), "APPEND".to_owned()),
                    ("media_id".to_owned(), "123".to_owned()),
                    ("segment_index".to_owned(), "0".to_owned()),
                ],
                file_field: "media".to_owned(),
                file_name: "clip \"one\".mp4".to_owned(),
                content_type: "video/mp4".to_owned(),
                bytes: vec![0x00, 0x01, 0x02],
            }));

        let response = transport.send(&request).expect("the script answered");
        // 204 with no body is success for APPEND and must not be reinterpreted.
        assert_eq!(response.status, 204);
        assert!(response.body_is_empty());

        let sent = transport.adapter().requests();
        let content_type = sent[0].header("content-type").expect("a content type");
        let boundary = content_type
            .split("boundary=")
            .nth(1)
            .expect("the transport must state the boundary it used");
        assert!(
            content_type.starts_with("multipart/form-data; boundary="),
            "{content_type}"
        );

        let body = String::from_utf8_lossy(&sent[0].body);
        assert!(body.starts_with(&format!("--{boundary}\r\n")), "{body}");
        assert!(
            body.contains("name=\"command\"\r\n\r\nAPPEND\r\n"),
            "{body}"
        );
        assert!(body.contains("name=\"media_id\"\r\n\r\n123\r\n"), "{body}");
        assert!(
            body.contains("name=\"segment_index\"\r\n\r\n0\r\n"),
            "{body}"
        );
        assert!(
            body.contains("name=\"media\"; filename=\"clip \\\"one\\\".mp4\""),
            "{body}"
        );
        assert!(body.contains("Content-Type: video/mp4\r\n\r\n"), "{body}");
        assert!(body.ends_with(&format!("--{boundary}--\r\n")), "{body}");
        // The binary segment is present byte for byte.
        let bytes_at = sent[0]
            .body
            .windows(3)
            .position(|window| window == [0x00, 0x01, 0x02])
            .expect("the segment bytes must be in the body");
        assert!(bytes_at < sent[0].body.len());
    }

    #[test]
    fn two_appends_do_not_share_a_boundary() {
        let first = prepare(
            &Request::new(Method::Post, "https://api.x.com/2/media/upload").body(Body::Multipart(
                Multipart {
                    fields: Vec::new(),
                    file_field: "media".to_owned(),
                    file_name: "a.mp4".to_owned(),
                    content_type: "video/mp4".to_owned(),
                    bytes: vec![1],
                },
            )),
        );
        let second = prepare(
            &Request::new(Method::Post, "https://api.x.com/2/media/upload").body(Body::Multipart(
                Multipart {
                    fields: Vec::new(),
                    file_field: "media".to_owned(),
                    file_name: "a.mp4".to_owned(),
                    content_type: "video/mp4".to_owned(),
                    bytes: vec![1],
                },
            )),
        );
        assert_ne!(
            first.header("content-type"),
            second.header("content-type"),
            "a reused boundary is a body the server may truncate"
        );
    }

    #[test]
    fn a_get_has_no_body() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(200, b"{}".to_vec()))]);
        let mut transport = transport(adapter);
        let request = Request::new(
            Method::Get,
            "https://api.x.com/2/media/upload?command=STATUS&media_id=1",
        );

        transport.send(&request).expect("the script answered");

        let sent = transport.adapter().requests();
        assert_eq!(sent[0].method, HttpMethod::Get);
        assert!(sent[0].body.is_empty());
        assert_eq!(sent[0].header("content-type"), None);
    }

    #[test]
    fn a_204_and_a_200_are_both_mapped_rather_than_turned_into_errors() {
        let adapter = RecordingAdapter::scripted(vec![
            Ok(reply(204, Vec::new())),
            Ok(reply(200, b"{}".to_vec())),
        ]);
        let mut transport = transport(adapter);

        let first = transport
            .send(&Request::new(Method::Post, "https://e/1"))
            .expect("204 is a response");
        let second = transport
            .send(&Request::new(Method::Post, "https://e/2"))
            .expect("200 is a response");

        assert_eq!((first.status, second.status), (204, 200));
        assert!(first.body_is_empty());
        assert!(!second.body_is_empty());
    }

    #[test]
    fn a_timeout_and_a_blocked_request_are_told_apart() {
        let adapter = RecordingAdapter::scripted(vec![
            Err(HttpFailure::new(
                HttpFailureKind::Timeout,
                "no answer in 300 s",
            )),
            Err(HttpFailure::new(
                HttpFailureKind::Redirect,
                "a proxy answered 302 for https://api.x.com/2/tweets",
            )),
        ]);
        let mut transport = transport(adapter);

        let timeout = transport
            .send(&Request::new(Method::Post, "https://api.x.com/2/tweets"))
            .expect_err("scripted failure");
        let blocked = transport
            .send(&Request::new(Method::Post, "https://api.x.com/2/tweets"))
            .expect_err("scripted failure");

        assert!(matches!(timeout, TransportError::Timeout), "{timeout:?}");
        assert!(matches!(blocked, TransportError::Blocked(_)), "{blocked:?}");
    }

    #[test]
    fn the_oauth_signature_never_appears_in_a_rendered_error() {
        let adapter = RecordingAdapter::scripted(vec![Err(HttpFailure::new(
            HttpFailureKind::Connect,
            "connection refused sending https://api.x.com/2/tweets?access_token=SECRET-TOKEN",
        ))]);
        let mut transport = transport(adapter);
        let request = Request::new(Method::Post, "https://api.x.com/2/tweets").header(
            "Authorization",
            "OAuth oauth_consumer_key=\"k\", oauth_signature=\"SECRET-SIGNATURE\"",
        );

        let error = transport.send(&request).expect_err("scripted failure");
        let rendered = error.to_string();

        assert!(!rendered.contains("SECRET-SIGNATURE"), "{rendered}");
        assert!(!rendered.contains("SECRET-TOKEN"), "{rendered}");
        assert!(rendered.contains("access_token=REDACTED"), "{rendered}");
        assert!(!format!("{error:?}").contains("SECRET-SIGNATURE"));
    }
}
