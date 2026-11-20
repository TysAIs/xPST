//! The live HTTP transport: the crate's [`Transport`] seam, implemented over
//! `reqwest` with rustls.
//!
//! This is the only module in the crate that can open a socket, and it is
//! generic over [`HttpAdapter`], the small "send these exact bytes" seam the
//! shared `xpst_provider::http` module owns. Production uses
//! [`xpst_provider::http::ReqwestAdapter`]; the tests in
//! `tests/live_transport.rs` use the recording adapter, so the whole resumable
//! protocol runs through *this* code with no network at all.
//!
//! Three properties are inherited from the shared policy and are worth stating
//! where the upload protocol is read:
//!
//! * **Redirects are never followed.** `308 Resume Incomplete` is the resumable
//!   protocol's progress signal, carrying the `Range` the server holds. A client
//!   that follows it — or that follows the `Location` from `INIT` as if it were
//!   a redirect — sends a request nobody wrote, and the driver loses the offset
//!   it was told.
//! * **Nothing is retried here.** One call is one request. The driver owns
//!   retries because it is the only layer that knows whether a chunk is safe to
//!   repeat and whether a finalising request may already have published.
//! * **A failure carries no credential and no body.** `HttpRequest` here has an
//!   `Authorization` header; it is never included in a [`TransportError`].
//!
//! [`Transport`]: crate::transport::Transport
//! [`TransportError`]: crate::transport::TransportError

use std::future::Future;

use xpst_provider::http::{
    ClientConfig, HttpAdapter, HttpFailure, HttpMethod, PreparedRequest, ReqwestAdapter,
};

use crate::transport::{HttpRequest, HttpResponse, Method, Transport, TransportError};

/// The resumable upload protocol over a real HTTP client.
///
/// Construct with [`HttpTransport::live`] for production, or
/// [`HttpTransport::new`] with any [`HttpAdapter`] — which is how the tests keep
/// the network out of the loop without giving up the real mapping code.
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

impl HttpTransport<ReqwestAdapter> {
    /// The production transport: rustls, verified TLS, no proxy, no redirects,
    /// no client-side retries, `xPST/<version>` as the user agent.
    pub fn live() -> Result<Self, TransportError> {
        Self::live_with(&ClientConfig::for_destination())
    }

    /// The production transport with an adjusted policy.
    ///
    /// Only the timeouts are meaningfully adjustable for this endpoint; a
    /// redirect policy other than the default would break resumption, which is
    /// why the caller has to ask for it deliberately.
    pub fn live_with(config: &ClientConfig) -> Result<Self, TransportError> {
        let adapter = ReqwestAdapter::new(config).map_err(transport_error)?;
        Ok(Self::new(adapter))
    }
}

impl<A: HttpAdapter + Sync> Transport for HttpTransport<A> {
    fn send(
        &self,
        request: HttpRequest,
    ) -> impl Future<Output = Result<HttpResponse, TransportError>> + Send {
        // The mapping is total: this seam's request type is already the shape
        // the adapter sends, because the resumable protocol only ever needs a
        // method, a URL, headers and a body.
        let prepared = PreparedRequest {
            method: match request.method {
                Method::Post => HttpMethod::Post,
                Method::Put => HttpMethod::Put,
            },
            url: request.url,
            headers: request.headers,
            body: request.body,
        };
        async move {
            let response = self
                .adapter
                .execute(prepared)
                .await
                .map_err(transport_error)?;
            Ok(HttpResponse {
                status: response.status,
                headers: response.headers,
                body: response.body,
            })
        }
    }
}

/// Translate the shared failure into this crate's vocabulary.
///
/// The kind decides retryability, and the detail is already redacted: it holds
/// the URL and the client's own words, never a header and never a body.
fn transport_error(failure: HttpFailure) -> TransportError {
    if failure.is_retryable() {
        TransportError::network(failure.to_string())
    } else {
        TransportError::fatal(failure.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use xpst_provider::http::fake::{reply, RecordingAdapter};
    use xpst_provider::http::HttpFailureKind;

    fn block_on<F: Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .expect("tokio runtime")
            .block_on(future)
    }

    #[test]
    fn a_post_keeps_its_method_url_headers_and_body() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(200, b"{}".to_vec()))]);
        let transport = HttpTransport::new(adapter);
        let request = HttpRequest::new(Method::Post, "https://upload.example/init")
            .with_header("Authorization", "Bearer ya29.token")
            .with_header("X-Upload-Content-Length", "1024")
            .with_body(b"{\"snippet\":{}}".to_vec());

        let response = block_on(transport.send(request)).expect("the script answered");

        assert_eq!(response.status, 200);
        let sent = transport.adapter().requests();
        assert_eq!(sent.len(), 1);
        assert_eq!(sent[0].method, HttpMethod::Post);
        assert_eq!(sent[0].url, "https://upload.example/init");
        assert_eq!(sent[0].header("authorization"), Some("Bearer ya29.token"));
        assert_eq!(sent[0].header("x-upload-content-length"), Some("1024"));
        assert_eq!(sent[0].body, b"{\"snippet\":{}}");
    }

    #[test]
    fn a_put_is_a_put() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(308, Vec::new()))]);
        let transport = HttpTransport::new(adapter);
        let request = HttpRequest::new(Method::Put, "https://upload.example/session")
            .with_header("Content-Range", "bytes 0-1023/1024");

        let response = block_on(transport.send(request)).expect("the script answered");

        // A 308 is a response, not a redirect: it must reach the driver with
        // its headers intact so it can read `Range`.
        assert_eq!(response.status, 308);
        assert_eq!(
            transport.adapter().requests()[0].method,
            HttpMethod::Put,
            "the resumable protocol never sends a chunk as anything but PUT"
        );
    }

    #[test]
    fn response_headers_and_body_map_through_unchanged() {
        let adapter = RecordingAdapter::scripted(vec![Ok(reply(
            200,
            br#"{"kind":"youtube#video","id":"vid-1"}"#.to_vec(),
        )
        .with_header("Content-Type", "application/json"))]);
        let transport = HttpTransport::new(adapter);

        let response = block_on(transport.send(HttpRequest::new(Method::Post, "https://e/x")))
            .expect("the script answered");

        assert_eq!(response.header("content-type"), Some("application/json"));
        assert_eq!(
            response.body_text(),
            "{\"kind\":\"youtube#video\",\"id\":\"vid-1\"}"
        );
    }

    #[test]
    fn a_timeout_is_retryable_and_a_tls_failure_is_not() {
        let timeout = RecordingAdapter::scripted(vec![Err(HttpFailure::new(
            HttpFailureKind::Timeout,
            "no answer inside 300 s",
        ))]);
        let transport = HttpTransport::new(timeout);
        let error = block_on(transport.send(HttpRequest::new(Method::Put, "https://e/x")))
            .expect_err("a failure must not become a response");
        assert!(error.retryable, "{error}");

        let tls = RecordingAdapter::scripted(vec![Err(HttpFailure::new(
            HttpFailureKind::Tls,
            "certificate is not trusted",
        ))]);
        let transport = HttpTransport::new(tls);
        let error = block_on(transport.send(HttpRequest::new(Method::Put, "https://e/x")))
            .expect_err("a failure must not become a response");
        assert!(!error.retryable, "{error}");
    }

    #[test]
    fn the_transport_error_does_not_quote_the_bearer_token() {
        let adapter = RecordingAdapter::scripted(vec![Err(HttpFailure::new(
            HttpFailureKind::Connect,
            "connection refused sending https://upload.example/init?upload_id=S1&access_token=SECRET-TOKEN",
        ))]);
        let transport = HttpTransport::new(adapter);
        let request = HttpRequest::new(Method::Post, "https://upload.example/init")
            .with_header("Authorization", "Bearer SECRET-TOKEN");

        let error = block_on(transport.send(request)).expect_err("scripted failure");
        let rendered = error.to_string();

        assert!(!rendered.contains("SECRET-TOKEN"), "{rendered}");
        assert!(rendered.contains("access_token=REDACTED"), "{rendered}");
        assert!(!format!("{error:?}").contains("SECRET-TOKEN"));
    }
}
