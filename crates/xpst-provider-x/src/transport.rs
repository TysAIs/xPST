//! The transport seam: every byte this crate sends leaves through here.
//!
//! The X publishing path is the one place in xPST where a bug spends money or
//! gets an account suspended, so it is written against a trait rather than an
//! HTTP client. Two consequences, both deliberate:
//!
//! * **Tests need no network.** A fake [`Transport`] records the exact request
//!   sequence (INIT → APPEND → FINALIZE → POST /2/tweets, or a single POST) and
//!   replays canned responses. Nothing in `cargo test` dials out.
//! * **No HTTP client enters the dependency graph.** `twapi-v2` (MIT, verified
//!   from its LICENSE) implements OAuth 1.0a signing and chunked v2 media
//!   upload, but its `Authentication::execute` takes a `reqwest::RequestBuilder`
//!   — its transport is welded to `reqwest`. Depending on it would mean the
//!   seam could not exist, so this crate implements the same protocol against
//!   this trait instead: `hmac` + `sha1` + `base64` only.
//!
//! Requests are fully described as data ([`Request`]) so a fake can assert on
//! the method, URL, headers and body without re-implementing the client.

use std::fmt;

/// HTTP method. Only the two X's publishing endpoints use are modelled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    Get,
    Post,
}

impl Method {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Get => "GET",
            Self::Post => "POST",
        }
    }
}

impl fmt::Display for Method {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A multipart body with one binary part — that is the shape of X's
/// `APPEND` command, which carries the media segment plus the command fields.
///
/// The boundary is the transport's business: a transport serialises this,
/// appends its own boundary parameter to the `Content-Type` request header and
/// sends the bytes. The publisher states `multipart/form-data` as the intent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Multipart {
    /// Ordinary text fields, sent before the file part.
    pub fields: Vec<(String, String)>,
    /// Name of the binary part (`media` for X).
    pub file_field: String,
    /// Filename reported to the server.
    pub file_name: String,
    /// Content type of the binary part.
    pub content_type: String,
    /// The segment.
    pub bytes: Vec<u8>,
}

/// The request body, tagged so a transport never has to guess.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Body {
    Empty,
    /// Serialised JSON, sent with `Content-Type: application/json`.
    Json(Vec<u8>),
    /// `application/x-www-form-urlencoded` parameters.
    Form(Vec<(String, String)>),
    Multipart(Multipart),
}

/// One outbound request, complete enough to re-issue elsewhere.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub method: Method,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Body,
}

impl Request {
    #[must_use]
    pub fn new(method: Method, url: impl Into<String>) -> Self {
        Self {
            method,
            url: url.into(),
            headers: Vec::new(),
            body: Body::Empty,
        }
    }

    #[must_use]
    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    #[must_use]
    pub fn body(mut self, body: Body) -> Self {
        self.body = body;
        self
    }

    /// The `command` field of a media-upload request, when the body carries
    /// one: `INIT`, `APPEND` or `FINALIZE`.
    ///
    /// The upload endpoint multiplexes every step onto one URL, so the command
    /// is what identifies which response shape is documented.
    #[must_use]
    pub fn command(&self) -> Option<&str> {
        // `STATUS` is a GET that carries the command in the query string, not
        // in a body. Reading only the body made every STATUS request look like
        // an undocumented endpoint.
        if let Some((_, query)) = self.url.split_once('?') {
            for pair in query.split('&') {
                if let Some((name, value)) = pair.split_once('=') {
                    if name == "command" {
                        return Some(value);
                    }
                }
            }
        }
        let fields = match &self.body {
            Body::Form(fields) => fields.as_slice(),
            Body::Multipart(part) => part.fields.as_slice(),
            Body::Empty | Body::Json(_) => return None,
        };
        fields
            .iter()
            .find(|(name, _)| name == "command")
            .map(|(_, value)| value.as_str())
    }

    /// Case-insensitive header lookup.
    #[must_use]
    pub fn header_value(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

/// One response, reduced to what error mapping actually reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    #[must_use]
    pub fn new(status: u16, body: impl Into<Vec<u8>>) -> Self {
        Self {
            status,
            headers: Vec::new(),
            body: body.into(),
        }
    }

    #[must_use]
    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    /// Case-insensitive header lookup.
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// Body as lossy UTF-8; X error bodies are JSON text.
    #[must_use]
    pub fn body_text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    /// Whether the response carried no body bytes at all.
    ///
    /// X's `APPEND` command answers `204 No Content` with a zero-length body,
    /// and that is success. Whitespace-only counts as empty: some intermediaries
    /// pad a 204 with a newline.
    #[must_use]
    pub fn body_is_empty(&self) -> bool {
        self.body.iter().all(u8::is_ascii_whitespace)
    }

    #[must_use]
    pub const fn is_success(&self) -> bool {
        self.status >= 200 && self.status < 300
    }
}

/// Why the transport itself failed, before any X status existed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportError {
    /// The connection failed, or the OS refused the write.
    Io(String),
    /// No response arrived inside the client's deadline.
    Timeout,
    /// A blocking layer (bot wall, proxy) answered instead of X.
    Blocked(String),
}

impl fmt::Display for TransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(detail) => write!(f, "transport I/O failure: {detail}"),
            Self::Timeout => f.write_str("transport timed out before any response"),
            Self::Blocked(detail) => write!(f, "transport blocked: {detail}"),
        }
    }
}

impl std::error::Error for TransportError {}

/// The seam every request crosses.
///
/// `&mut self` because a real implementation owns a connection pool and a fake
/// records the requests it saw; neither is meaningfully shareable.
pub trait Transport {
    fn send(&mut self, request: &Request) -> Result<Response, TransportError>;
}
