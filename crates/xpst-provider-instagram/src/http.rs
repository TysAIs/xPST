//! The live HTTP transport: the crate's [`Transport`] seam over `reqwest`.
//!
//! Instagram's publishing path is blocking, so the exchange is performed by a
//! [`BlockingHttpAdapter`] and this module does only the mapping: query
//! parameters onto the URL, form parameters into an
//! `application/x-www-form-urlencoded` body, raw bytes onto the resumable upload
//! endpoint, and the answer back into an [`HttpResponse`].
//!
//! Three things are decided here rather than by the logic layer, and each one is
//! a place a caller could get wrong if it guessed:
//!
//! * **Percent-encoding.** [`HttpRequest`] carries parameters as pairs
//!   precisely so that this layer — not the publisher — decides how a caption
//!   containing `&` is escaped. A pre-encoded string could be encoded twice.
//! * **Query merging.** [`Publisher::container_status_url`] already returns a
//!   URL with `?fields=status_code`, and the request adds `access_token` to it.
//!   Appending blindly would produce a second `?`, which Meta reads as part of
//!   the first parameter's value.
//! * **No redirects, no retries, no credentials in a message.** Inherited from
//!   [`ClientConfig::for_destination`]. `access_token` is a query parameter on
//!   the Graph API, so every error path has one in its URL and the redaction in
//!   [`xpst_provider::http::redact_text`] is what keeps it out of the report.
//!
//! [`Publisher::container_status_url`]: crate::publish::Publisher
//! [`Transport`]: crate::transport::Transport

use xpst_provider::http::{
    form_encode, query_encode, BlockingHttpAdapter, ClientConfig, HttpMethod, PreparedRequest,
    ReqwestBlockingAdapter,
};

use crate::transport::{HttpRequest, HttpResponse, Method, Transport, TransportError};

/// The Graph API and resumable upload endpoints over a real HTTP client.
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
    pub fn live_with(config: &ClientConfig) -> Result<Self, TransportError> {
        let adapter = ReqwestBlockingAdapter::new(config)
            .map_err(|failure| TransportError::new(failure.to_string()))?;
        Ok(Self::new(adapter))
    }
}

impl<A: BlockingHttpAdapter> Transport for HttpTransport<A> {
    fn send(&mut self, request: &HttpRequest) -> Result<HttpResponse, TransportError> {
        // One call, one request. `media_publish` is not idempotent and has no
        // idempotency key, so a retry is a decision that needs the reconciliation
        // step — never something this layer does on its own.
        let prepared = prepare(request);
        let response = self
            .adapter
            .execute(prepared)
            .map_err(|failure| TransportError::new(failure.to_string()))?;
        let body = response.body_text();
        Ok(HttpResponse {
            status: response.status,
            headers: response.headers,
            body,
        })
    }
}

/// Materialise an [`HttpRequest`] into the exact bytes that go on the wire.
#[must_use]
pub fn prepare(request: &HttpRequest) -> PreparedRequest {
    let (method, body, content_type) = match request.method {
        Method::Get => (HttpMethod::Get, Vec::new(), None),
        Method::PostForm => (
            HttpMethod::Post,
            form_encode(&request.form).into_bytes(),
            Some("application/x-www-form-urlencoded"),
        ),
        Method::PostBytes => (HttpMethod::Post, request.body.clone(), None),
    };
    let mut prepared = PreparedRequest {
        method,
        url: with_query(&request.url, &request.query),
        headers: request.headers.clone(),
        body,
    };
    if let Some(content_type) = content_type {
        if prepared.header("Content-Type").is_none() {
            prepared = prepared.with_header("Content-Type", content_type);
        }
    }
    prepared
}

/// Append `query` to `url`, keeping any query the URL already carries.
///
/// `container_status_url` returns `…?fields=status_code`; the token is added on
/// top of it. Joining with the wrong separator would make Meta read
/// `status_code?access_token` as one value.
fn with_query(url: &str, query: &[(String, String)]) -> String {
    if query.is_empty() {
        return url.to_owned();
    }
    let encoded = query_encode(query);
    if url.contains('?') {
        if url.ends_with('?') || url.ends_with('&') {
            format!("{url}{encoded}")
        } else {
            format!("{url}&{encoded}")
        }
    } else {
        format!("{url}?{encoded}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use xpst_provider::http::fake::{reply, RecordingAdapter};
    use xpst_provider::http::{HttpFailure, HttpFailureKind};

    fn transport(adapter: RecordingAdapter) -> HttpTransport<RecordingAdapter> {
        HttpTransport::new(adapter)
    }

    #[test]
    fn a_get_merges_its_parameters_into_an_existing_query() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(200, b"{\"id\":\"1\"}".to_vec()))]);
        let mut transport = transport(adapter);
        let request = HttpRequest::get(
            "https://graph.facebook.com/v23.0/17841400/media?fields=status_code".to_owned(),
            vec![("access_token".to_owned(), "TOKEN/1".to_owned())],
        );

        transport.send(&request).expect("the script answered");

        let sent = transport.adapter().requests();
        assert_eq!(sent[0].method, HttpMethod::Get);
        assert_eq!(
            sent[0].url,
            "https://graph.facebook.com/v23.0/17841400/media?fields=status_code&access_token=TOKEN%2F1"
        );
        assert!(sent[0].body.is_empty());
        assert_eq!(sent[0].header("content-type"), None);
    }

    #[test]
    fn a_get_without_parameters_leaves_the_url_alone() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(200, b"{}".to_vec()))]);
        let mut transport = transport(adapter);
        transport
            .send(&HttpRequest::get(
                "https://graph.example/1".to_owned(),
                Vec::new(),
            ))
            .expect("the script answered");
        assert_eq!(
            transport.adapter().requests()[0].url,
            "https://graph.example/1"
        );
    }

    #[test]
    fn a_form_post_encodes_every_parameter_and_states_its_type() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(200, b"{}".to_vec()))]);
        let mut transport = transport(adapter);
        let request = HttpRequest::post_form(
            "https://graph.facebook.com/v23.0/17841400/media".to_owned(),
            vec![
                ("caption".to_owned(), "a & b".to_owned()),
                ("access_token".to_owned(), "TOKEN".to_owned()),
            ],
        );

        transport.send(&request).expect("the script answered");

        let sent = transport.adapter().requests();
        assert_eq!(sent[0].method, HttpMethod::Post);
        assert_eq!(
            String::from_utf8_lossy(&sent[0].body),
            "caption=a+%26+b&access_token=TOKEN"
        );
        assert_eq!(
            sent[0].header("content-type"),
            Some("application/x-www-form-urlencoded")
        );
        // The URL is not the carrier for a form post.
        assert_eq!(
            sent[0].url,
            "https://graph.facebook.com/v23.0/17841400/media"
        );
    }

    #[test]
    fn a_binary_upload_keeps_its_headers_and_its_bytes() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(200, b"{}".to_vec()))]);
        let mut transport = transport(adapter);
        let request = HttpRequest::post_bytes(
            "https://rupload.facebook.com/video-upload/v23.0/1".to_owned(),
            vec![
                ("Authorization".to_owned(), "OAuth SECRET-TOKEN".to_owned()),
                ("offset".to_owned(), "0".to_owned()),
                ("file_size".to_owned(), "3".to_owned()),
            ],
            vec![0xde, 0xad, 0xbe],
        );

        transport.send(&request).expect("the script answered");

        let sent = transport.adapter().requests();
        assert_eq!(sent[0].method, HttpMethod::Post);
        assert_eq!(sent[0].header("authorization"), Some("OAuth SECRET-TOKEN"));
        assert_eq!(sent[0].header("offset"), Some("0"));
        assert_eq!(sent[0].body, vec![0xde, 0xad, 0xbe]);
    }

    #[test]
    fn the_response_body_is_decoded_as_text() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(
            400,
            br#"{"error":{"message":"Invalid parameter"}}"#.to_vec(),
        )
        .with_header("Content-Type", "application/json"))]);
        let mut transport = transport(adapter);

        let response = transport
            .send(&HttpRequest::get(
                "https://graph.example/1".to_owned(),
                Vec::new(),
            ))
            .expect("a 400 is still a response");

        assert_eq!(response.status, 400);
        assert!(!response.is_success());
        assert_eq!(response.header("content-type"), Some("application/json"));
        assert_eq!(
            response.body,
            r#"{"error":{"message":"Invalid parameter"}}"#
        );
    }

    #[test]
    fn an_access_token_in_the_url_never_reaches_the_error() {
        let adapter = RecordingAdapter::scripted(vec![Err(HttpFailure::new(
            HttpFailureKind::Connect,
            "connection refused sending https://graph.facebook.com/v23.0/1/media?access_token=SECRET-TOKEN",
        ))]);
        let mut transport = transport(adapter);
        let request = HttpRequest::get(
            "https://graph.example/1".to_owned(),
            vec![("access_token".to_owned(), "SECRET-TOKEN".to_owned())],
        );

        let error = transport.send(&request).expect_err("scripted failure");
        let rendered = error.to_string();

        assert!(!rendered.contains("SECRET-TOKEN"), "{rendered}");
        assert!(rendered.contains("access_token=REDACTED"), "{rendered}");
    }

    #[test]
    fn an_authorization_header_is_not_rendered_by_an_error_either() {
        let adapter = RecordingAdapter::scripted(vec![Err(HttpFailure::new(
            HttpFailureKind::Timeout,
            "no answer inside 300 s",
        ))]);
        let mut transport = transport(adapter);
        let request = HttpRequest::post_bytes(
            "https://rupload.facebook.com/video-upload/v23.0/1".to_owned(),
            vec![("Authorization".to_owned(), "OAuth SECRET-TOKEN".to_owned())],
            vec![1, 2, 3],
        );

        let error = transport.send(&request).expect_err("scripted failure");
        assert!(!error.to_string().contains("SECRET-TOKEN"), "{error}");
        assert!(!format!("{error:?}").contains("SECRET-TOKEN"));
    }
}
