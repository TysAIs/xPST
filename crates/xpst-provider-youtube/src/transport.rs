//! The transport seam: HTTP as a value, and the bytes as a range-addressable
//! source.
//!
//! Nothing in this crate opens a socket. The resumable upload driver is written
//! against [`Transport`] and [`MediaSource`], so the protocol is exercised with
//! a scripted fake in tests and a real `reqwest` client can be dropped in
//! later without touching one line of the state machine.
//!
//! Both traits are **asynchronous on purpose**. The Python adapter had to push
//! its blocking `next_chunk()` loop onto a thread pool (`asyncio.to_thread`)
//! because a blocking upload inside the event loop stalls every other task;
//! with an async seam there is nothing to stall — awaiting the transport yields
//! to the runtime. A `MediaSource` implementation must honour the same rule: it
//! may not do blocking file I/O on the caller's thread.

use std::fmt;
use std::future::Future;
use std::io;

/// HTTP methods the resumable protocol uses. There are only two.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    Post,
    Put,
}

impl Method {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Post => "POST",
            Self::Put => "PUT",
        }
    }
}

/// One HTTP request, as data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpRequest {
    pub method: Method,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpRequest {
    #[must_use]
    pub fn new(method: Method, url: impl Into<String>) -> Self {
        Self {
            method,
            url: url.into(),
            headers: Vec::new(),
            body: Vec::new(),
        }
    }

    /// Append a header (transport-level duplicates are legal in HTTP).
    #[must_use]
    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    #[must_use]
    pub fn with_body(mut self, body: Vec<u8>) -> Self {
        self.body = body;
        self
    }

    /// The first value for a header, case-insensitively.
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&str> {
        header_value(&self.headers, name)
    }
}

impl fmt::Display for HttpRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} {}", self.method.as_str(), self.url)
    }
}

/// One HTTP response, as data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    #[must_use]
    pub fn new(status: u16) -> Self {
        Self {
            status,
            headers: Vec::new(),
            body: Vec::new(),
        }
    }

    #[must_use]
    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    #[must_use]
    pub fn with_body(mut self, body: Vec<u8>) -> Self {
        self.body = body;
        self
    }

    /// The first value for a header, case-insensitively.
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&str> {
        header_value(&self.headers, name)
    }

    /// The body as text, lossily — a Google error body is JSON or HTML.
    #[must_use]
    pub fn body_text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
}

fn header_value<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .map(|(_, value)| value.as_str())
}

/// A transport-level failure: no usable HTTP response at all.
///
/// `retryable` is the transport's own judgement because only it knows whether
/// the failure is a lost connection (retry) or a configuration problem such as
/// an untrusted certificate (do not retry). The driver never retries a
/// non-retryable one.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("transport error: {message}")]
pub struct TransportError {
    pub message: String,
    pub retryable: bool,
}

impl TransportError {
    /// A network failure: the request may or may not have landed.
    #[must_use]
    pub fn network(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: true,
        }
    }

    /// A failure retrying cannot fix (TLS trust, DNS misconfiguration).
    #[must_use]
    pub fn fatal(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: false,
        }
    }
}

/// Somewhere to send an [`HttpRequest`].
///
/// Implementations must not block the executor thread. The returned future is
/// `Send` so a caller can spawn the whole upload without pinning it to the
/// current task.
pub trait Transport {
    /// Send one request and return the response.
    fn send(
        &self,
        request: HttpRequest,
    ) -> impl Future<Output = Result<HttpResponse, TransportError>> + Send;
}

/// The bytes to upload, addressed by range.
///
/// A file-backed implementation belongs to the phase that does live uploads
/// (it has to decide how to offload reads); tests use [`MemoryMedia`]. What
/// matters here is that the driver asks for exactly the range it needs, so
/// resuming after a 308 never re-reads or re-sends earlier bytes.
pub trait MediaSource {
    /// Total length in bytes, which every `Content-Range` is stated against.
    fn total_bytes(&self) -> u64;

    /// Read `len` bytes starting at `offset`.
    ///
    /// Must not block the executor thread.
    fn read_range(
        &self,
        offset: u64,
        len: u64,
    ) -> impl Future<Output = Result<Vec<u8>, io::Error>> + Send;
}

/// An in-memory [`MediaSource`].
///
/// Used by the tests and by any dry-run path; it is deliberately not a file
/// reader, because a file reader that blocks would reintroduce the Python
/// trap this design exists to avoid.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemoryMedia {
    bytes: Vec<u8>,
}

impl MemoryMedia {
    #[must_use]
    pub fn new(bytes: Vec<u8>) -> Self {
        Self { bytes }
    }

    /// `len` zero bytes, for a test that only cares about sizes and offsets.
    #[must_use]
    pub fn zeroed(len: u64) -> Self {
        Self {
            bytes: vec![0; usize::try_from(len).unwrap_or(usize::MAX)],
        }
    }

    #[must_use]
    pub fn len(&self) -> u64 {
        self.bytes.len() as u64
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }
}

impl MediaSource for MemoryMedia {
    fn total_bytes(&self) -> u64 {
        self.len()
    }

    fn read_range(
        &self,
        offset: u64,
        len: u64,
    ) -> impl Future<Output = Result<Vec<u8>, io::Error>> + Send {
        let start = usize::try_from(offset)
            .unwrap_or(usize::MAX)
            .min(self.bytes.len());
        let requested = usize::try_from(len).unwrap_or(usize::MAX);
        let end = start.saturating_add(requested).min(self.bytes.len());
        let slice = self.bytes[start..end].to_vec();
        async move {
            if slice.len() as u64 != len {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    format!(
                        "{len} bytes requested at offset {offset}, {} available",
                        slice.len()
                    ),
                ));
            }
            Ok(slice)
        }
    }
}
