//! The HTTP seam every publishing call goes through.
//!
//! This crate owns Instagram's publishing *logic* — what to ask for, in what
//! order, and what an answer means. It deliberately owns no HTTP client: the
//! engine supplies one implementation of [`Transport`], and the tests supply a
//! fake that records requests and replays scripted responses. That is what
//! keeps `cargo test` free of network access and keeps the request shapes
//! reviewable as data instead of hidden inside a client library.
//!
//! Requests carry their parameters as structured pairs, not as a pre-built
//! string. An implementation is responsible for percent-encoding them: the
//! logic layer must never guess whether a caption containing `&` was escaped.

use std::fmt;

/// How a request body is carried.
///
/// Meta's Graph API takes publishing parameters as form fields, and its
/// resumable upload endpoint takes the video as raw bytes, so those are the two
/// shapes this crate needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    /// No body; parameters travel in [`HttpRequest::query`].
    Get,
    /// Parameters travel in [`HttpRequest::form`] as
    /// `application/x-www-form-urlencoded`.
    PostForm,
    /// Parameters travel in [`HttpRequest::headers`], body in
    /// [`HttpRequest::body`].
    PostBytes,
}

/// One outgoing request, as data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpRequest {
    pub method: Method,
    pub url: String,
    /// Query-string parameters. Implementations must percent-encode both name
    /// and value.
    pub query: Vec<(String, String)>,
    /// Form-body parameters, same encoding rule.
    pub form: Vec<(String, String)>,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpRequest {
    /// A `GET` whose parameters are encoded into the query string.
    pub fn get(url: impl Into<String>, query: Vec<(String, String)>) -> Self {
        Self {
            method: Method::Get,
            url: url.into(),
            query,
            form: Vec::new(),
            headers: Vec::new(),
            body: Vec::new(),
        }
    }

    /// A form-encoded `POST`, the shape every Graph publishing call uses.
    pub fn post_form(url: impl Into<String>, form: Vec<(String, String)>) -> Self {
        Self {
            method: Method::PostForm,
            url: url.into(),
            query: Vec::new(),
            form,
            headers: Vec::new(),
            body: Vec::new(),
        }
    }

    /// A binary `POST`, used only by the resumable upload endpoint.
    pub fn post_bytes(
        url: impl Into<String>,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    ) -> Self {
        Self {
            method: Method::PostBytes,
            url: url.into(),
            query: Vec::new(),
            form: Vec::new(),
            headers,
            body,
        }
    }

    /// Case-insensitive header lookup, for callers that need to read what they
    /// built (tests mostly).
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// Case-insensitive form lookup.
    #[must_use]
    pub fn form_value(&self, name: &str) -> Option<&str> {
        self.form
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }
}

/// A provider answer, body already decoded as text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: String,
}

impl HttpResponse {
    /// Build a response. Tests use this; a real transport builds it from the
    /// wire.
    pub fn new(status: u16, headers: Vec<(String, String)>, body: impl Into<String>) -> Self {
        Self {
            status,
            headers,
            body: body.into(),
        }
    }

    /// Case-insensitive header lookup (`Retry-After` is the one that matters).
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// Whether the status is in the 2xx range.
    #[must_use]
    pub fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
    }
}

/// A transport that could not complete the request at all.
///
/// Distinct from a provider error: nothing was classified because nothing came
/// back. It is retryable in principle, but the retry policy belongs to the
/// caller, not to this crate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransportError {
    pub message: String,
}

impl TransportError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for TransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for TransportError {}

/// The one thing this crate needs from the outside world.
///
/// `&mut self` because a real client may hold a connection pool, and because a
/// fake transport records the requests it saw.
pub trait Transport {
    fn send(&mut self, request: &HttpRequest) -> Result<HttpResponse, TransportError>;
}
