//! Shared HTTP policy for the live transports: one place that decides how a
//! client is configured, how a request is described, and what a failure is
//! allowed to say.
//!
//! # Why this is shared and the mapping is not
//!
//! The four destination crates describe requests in genuinely different types —
//! a resumable `PUT` payload, a multipart command body, Graph form fields, an
//! XRPC call — so a single transport cannot implement all of them without
//! depending on every provider crate upside down. What *is* identical
//! everywhere is the policy around the request: timeouts, redirects, proxy
//! behaviour, TLS verification, the user agent, `Retry-After` parsing and the
//! rule that an error must never quote a credential. That policy lives here,
//! once, and each provider crate keeps only its own request/response mapping.
//!
//! # Two rules this module exists to enforce
//!
//! * **A transport never retries.** [`ClientConfig::retries`] is
//!   [`Retries::Never`], and [`ReqwestAdapter`] applies it explicitly, because
//!   `reqwest` 0.13 retries protocol NACKs by default. Retry ownership belongs
//!   to the pipeline (and to each provider's resumable driver), which knows
//!   whether a request is idempotent. A transport that retries a
//!   `media_publish` POST on its own is a duplicate post.
//! * **A failure never quotes a credential or a body.** [`HttpFailure`] carries a
//!   kind and a detail that has been through [`redact_text`]; [`PreparedRequest`]
//!   implements `Debug` by hand so that `{request:?}` cannot print an
//!   `Authorization` header. There is no logging in this module at all.
//!
//! The live adapters are behind the `live-http` feature so that a crate which
//! only classifies an answer does not link an HTTP stack.

use std::fmt;
use std::future::Future;
use std::time::{Duration, SystemTime};

/// The application name every request is labelled with.
pub const APP_NAME: &str = "xPST";

/// The value written with [`REDACTED`] wherever a secret was removed.
pub const REDACTED: &str = "REDACTED";

/// `xPST/<version>` — the user agent every destination sends.
///
/// Platforms use it to attribute traffic and to contact us about it, and a
/// version is the difference between "xPST is misbehaving" and "xPST 1.3.0 is
/// misbehaving". It is not configurable: a caller that can forge it is a caller
/// that will be blamed for someone else's traffic.
#[must_use]
pub fn user_agent() -> String {
    format!("{APP_NAME}/{}", env!("CARGO_PKG_VERSION"))
}

/// The methods the publishing paths use.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HttpMethod {
    Get,
    Post,
    Put,
    Delete,
    Patch,
}

impl HttpMethod {
    /// The wire spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Get => "GET",
            Self::Post => "POST",
            Self::Put => "PUT",
            Self::Delete => "DELETE",
            Self::Patch => "PATCH",
        }
    }

    /// Whether repeating the identical request is safe without asking the
    /// platform first.
    ///
    /// Used only to decide what a *caller* may do; this crate's transports
    /// never retry anything (see [`ClientConfig::retries`]).
    #[must_use]
    pub const fn is_idempotent(self) -> bool {
        matches!(self, Self::Get | Self::Put | Self::Delete)
    }
}

impl fmt::Display for HttpMethod {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// One request, fully materialised: method, URL, headers and the exact bytes.
///
/// This is what an [`HttpAdapter`] receives, which is what makes the mapping
/// from each provider's request type assertable in a test without a socket.
#[derive(Clone, PartialEq, Eq)]
pub struct PreparedRequest {
    pub method: HttpMethod,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl PreparedRequest {
    #[must_use]
    pub fn new(method: HttpMethod, url: impl Into<String>) -> Self {
        Self {
            method,
            url: url.into(),
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

    /// The request rendered for a human: method and redacted URL, nothing else.
    #[must_use]
    pub fn describe(&self) -> String {
        format!("{} {}", self.method.as_str(), redact_url(&self.url))
    }
}

/// Hand-written so that `{request:?}` cannot leak an `Authorization` header or
/// the request body. A derive here would put a bearer token one `dbg!` away
/// from a log file.
impl fmt::Debug for PreparedRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PreparedRequest")
            .field("method", &self.method)
            .field("url", &redact_url(&self.url))
            .field("headers", &RedactedHeaders(&self.headers))
            .field("body_bytes", &self.body.len())
            .finish()
    }
}

/// One response, as bytes. Header values are decoded lossily: HTTP allows
/// bytes outside ASCII and a rejected response would be worse than a lossy one.
#[derive(Clone, PartialEq, Eq)]
pub struct PreparedResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl PreparedResponse {
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

    /// The body as text, lossily — platform error bodies are JSON or HTML.
    #[must_use]
    pub fn body_text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    /// Whether the status is in the 2xx range.
    #[must_use]
    pub const fn is_success(&self) -> bool {
        self.status >= 200 && self.status < 300
    }
}

/// Same rule as [`PreparedRequest`]: a response body can be a token scope dump
/// or a private message, so it is described by length, never by content.
impl fmt::Debug for PreparedResponse {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PreparedResponse")
            .field("status", &self.status)
            .field("headers", &RedactedHeaders(&self.headers))
            .field("body_bytes", &self.body.len())
            .finish()
    }
}

fn header_value<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .map(|(_, value)| value.as_str())
}

/// Header names whose values are credentials. Their values are replaced, not
/// truncated: half a bearer token is still half a bearer token.
const SENSITIVE_HEADERS: [&str; 7] = [
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-amz-security-token",
];

/// Query parameter names that carry credentials or signatures.
///
/// `access_token` is not hypothetical: Meta's Graph API takes the token as a
/// query parameter on `GET`, so every Instagram error path has one in its URL.
/// `upload_id` is here for the same reason in a different disguise — a Google
/// resumable-upload session URI is a capability that lets whoever holds it write
/// into that session.
const SENSITIVE_PARAMS: [&str; 18] = [
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "api_key",
    "apikey",
    "key",
    "client_secret",
    "secret",
    "password",
    "sig",
    "signature",
    "x-goog-signature",
    "oauth_signature",
    "oauth_token",
    "oauth_verifier",
    "code",
    "upload_id",
];

fn is_sensitive_header(name: &str) -> bool {
    SENSITIVE_HEADERS
        .iter()
        .any(|candidate| candidate.eq_ignore_ascii_case(name))
}

fn is_sensitive_param(name: &str) -> bool {
    SENSITIVE_PARAMS
        .iter()
        .any(|candidate| candidate.eq_ignore_ascii_case(name))
}

/// A header list rendered for a human, with credential values removed.
struct RedactedHeaders<'a>(&'a [(String, String)]);

impl fmt::Debug for RedactedHeaders<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut list = formatter.debug_list();
        for (name, value) in self.0 {
            if is_sensitive_header(name) {
                list.entry(&format_args!("{name}: <redacted>"));
            } else if value.len() > 120 {
                list.entry(&format_args!("{name}: {}…", &value[..120]));
            } else {
                list.entry(&format_args!("{name}: {value}"));
            }
        }
        list.finish()
    }
}

/// Remove credentials from a URL: userinfo and sensitive query parameters.
///
/// Everything else is preserved, because the URL is the most useful fact an
/// error can carry and a blanket `<url removed>` makes a 404 undebuggable.
#[must_use]
pub fn redact_url(url: &str) -> String {
    let (without_fragment, fragment) = match url.split_once('#') {
        Some((head, fragment)) => (head, Some(fragment)),
        None => (url, None),
    };
    let (head, query) = match without_fragment.split_once('?') {
        Some((head, query)) => (head, Some(query)),
        None => (without_fragment, None),
    };

    let mut redacted = redact_userinfo(head);
    if let Some(query) = query {
        redacted.push('?');
        redacted.push_str(&redact_query(query));
    }
    if let Some(fragment) = fragment {
        redacted.push('#');
        redacted.push_str(fragment);
    }
    redacted
}

fn redact_userinfo(head: &str) -> String {
    let Some(scheme_end) = head.find("://") else {
        return head.to_owned();
    };
    let authority_start = scheme_end + "://".len();
    let rest = &head[authority_start..];
    let authority_end = rest.find('/').unwrap_or(rest.len());
    let authority = &rest[..authority_end];
    let Some(at) = authority.rfind('@') else {
        return head.to_owned();
    };
    let mut out = String::with_capacity(head.len());
    out.push_str(&head[..authority_start]);
    out.push_str(REDACTED);
    out.push_str(&authority[at..]);
    out.push_str(&rest[authority_end..]);
    out
}

fn redact_query(query: &str) -> String {
    query
        .split('&')
        .map(|pair| match pair.split_once('=') {
            Some((name, _)) if is_sensitive_param(name) => format!("{name}={REDACTED}"),
            _ => pair.to_owned(),
        })
        .collect::<Vec<_>>()
        .join("&")
}

/// Redact every URL inside a free-text message.
///
/// `reqwest` renders the request URL inside its `Display`, so any detail built
/// from a client error has to go through this — otherwise a Graph API failure
/// message quietly contains the user's access token.
#[must_use]
pub fn redact_text(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    loop {
        let start = match (rest.find("http://"), rest.find("https://")) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (Some(a), None) | (None, Some(a)) => Some(a),
            (None, None) => None,
        };
        let Some(start) = start else {
            out.push_str(rest);
            return out;
        };
        out.push_str(&rest[..start]);
        let after = &rest[start..];
        let end = after
            .find(|character: char| !is_url_character(character))
            .unwrap_or(after.len());
        out.push_str(&redact_url(&after[..end]));
        rest = &after[end..];
    }
}

/// Characters that continue a URL. Deliberately excludes parentheses, quotes
/// and whitespace, which is how `… for url (https://…)` gets split.
const fn is_url_character(character: char) -> bool {
    character.is_ascii_alphanumeric()
        || matches!(
            character,
            '-' | '.'
                | '_'
                | '~'
                | ':'
                | '/'
                | '?'
                | '#'
                | '['
                | ']'
                | '@'
                | '!'
                | '$'
                | '&'
                | '*'
                | '+'
                | ','
                | ';'
                | '='
                | '%'
        )
}

/// Why a request produced no usable response.
///
/// The kind decides what a caller may do next; the detail is for a human and
/// carries no credential (see [`HttpFailure::new`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HttpFailureKind {
    /// The connection could not be established (DNS, refused, reset).
    Connect,
    /// No complete response arrived inside the client's deadline.
    Timeout,
    /// The TLS handshake failed, almost always an untrusted certificate.
    Tls,
    /// A redirect the client was told not to follow.
    Redirect,
    /// The exchange could not be framed: a bad header value, a broken body.
    Protocol,
    /// Anything else the client reported.
    Other,
}

impl HttpFailureKind {
    /// A short description for an error message.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Connect => "connection failed",
            Self::Timeout => "timed out",
            Self::Tls => "TLS handshake failed",
            Self::Redirect => "redirect not followed",
            Self::Protocol => "protocol error",
            Self::Other => "transport failure",
        }
    }

    /// Whether sending the identical request again could succeed.
    ///
    /// `Timeout` is included even though the request may have landed: this says
    /// "retrying might work", not "retrying is safe". Whether it is safe is the
    /// pipeline's call, and for a non-idempotent POST it never is without a
    /// read-back first.
    #[must_use]
    pub const fn is_retryable(self) -> bool {
        matches!(self, Self::Connect | Self::Timeout)
    }
}

impl fmt::Display for HttpFailureKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// A transport-level failure: no usable HTTP response exists.
///
/// Constructed only through [`HttpFailure::new`], which redacts the detail. It
/// is deliberately impossible to attach a request body or a header list to this
/// type: those are how credentials leak into a report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpFailure {
    pub kind: HttpFailureKind,
    pub detail: String,
}

impl HttpFailure {
    /// Build a failure, redacting every URL in `detail`.
    #[must_use]
    pub fn new(kind: HttpFailureKind, detail: impl Into<String>) -> Self {
        Self {
            kind,
            detail: redact_text(&detail.into()),
        }
    }

    /// Whether sending the identical request again could succeed.
    #[must_use]
    pub const fn is_retryable(&self) -> bool {
        self.kind.is_retryable()
    }
}

impl fmt::Display for HttpFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.kind, self.detail)
    }
}

impl std::error::Error for HttpFailure {}

/// How redirects are handled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Redirects {
    /// Surface every 3xx to the provider.
    ///
    /// This is the default and it is not a conservative habit: Google's
    /// resumable upload answers `308 Resume Incomplete` with the `Range` the
    /// server holds, and X's media upload reads `Location` after `INIT`. A
    /// client that follows those silently turns progress information into a
    /// second request against a URL that was never meant to be fetched.
    NoFollow,
    /// Follow up to `limit` redirects. Only for an endpoint where a redirect is
    /// known to be uninteresting.
    FollowUpTo(u8),
}

/// Whether the client may use a proxy from the environment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Proxy {
    /// Ignore `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`.
    ///
    /// The default. An environment variable that silently routes a bearer token
    /// through an unknown intermediary is exactly the surprise this avoids; an
    /// operator who needs a proxy opts in by name.
    Disabled,
    /// Honour the environment's proxy settings.
    FromEnvironment,
}

/// TLS verification. One variant, because "do not verify" is not an option
/// this product offers: a publishing tool that accepts any certificate hands
/// every token to whoever can intercept traffic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tls {
    Verify,
}

/// Whether the HTTP client retries internally.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Retries {
    /// One request per call, always. Retry ownership stays with the pipeline.
    Never,
}

/// Everything that decides how a live client behaves.
///
/// A value, not a builder, so the policy can be asserted in a test without
/// constructing a client or opening a socket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientConfig {
    /// How long the connection may take to establish.
    pub connect_timeout: Duration,
    /// Total deadline for one request, response body included.
    pub request_timeout: Duration,
    pub redirects: Redirects,
    pub proxy: Proxy,
    pub tls: Tls,
    pub retries: Retries,
    /// The exact `User-Agent` line.
    pub user_agent: String,
}

impl ClientConfig {
    /// A dead host fails fast instead of holding the pipeline.
    pub const DEFAULT_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
    /// One request's ceiling.
    ///
    /// Media arrives in 5 MiB (X) and 8 MiB (YouTube) chunks, so this is not a
    /// cap on an upload — it is the point at which a single chunk or metadata
    /// call is declared lost. Five minutes is roughly 220 KiB/s for the largest
    /// chunk: slow enough for a domestic uplink, short enough that a wedged
    /// connection does not stall a run for an hour.
    pub const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(300);

    /// The policy every destination starts from.
    #[must_use]
    pub fn for_destination() -> Self {
        Self {
            connect_timeout: Self::DEFAULT_CONNECT_TIMEOUT,
            request_timeout: Self::DEFAULT_REQUEST_TIMEOUT,
            redirects: Redirects::NoFollow,
            proxy: Proxy::Disabled,
            tls: Tls::Verify,
            retries: Retries::Never,
            user_agent: user_agent(),
        }
    }

    #[must_use]
    pub fn with_connect_timeout(mut self, timeout: Duration) -> Self {
        self.connect_timeout = timeout;
        self
    }

    #[must_use]
    pub fn with_request_timeout(mut self, timeout: Duration) -> Self {
        self.request_timeout = timeout;
        self
    }

    #[must_use]
    pub fn with_redirects(mut self, redirects: Redirects) -> Self {
        self.redirects = redirects;
        self
    }

    /// Opt in to the environment's proxy. The token then transits whatever that
    /// proxy is, which is a decision an operator makes explicitly.
    #[must_use]
    pub fn with_proxy(mut self, proxy: Proxy) -> Self {
        self.proxy = proxy;
        self
    }
}

/// Somewhere a [`PreparedRequest`] is actually sent.
///
/// Implementations must not retry, must not log the request, and must return
/// [`HttpFailure`] rather than a synthesised status: a request that produced no
/// answer has no status, and inventing one is how a lost publish is reported as
/// a clean failure.
pub trait HttpAdapter {
    /// Send one request and return the response.
    fn execute(
        &self,
        request: PreparedRequest,
    ) -> impl Future<Output = Result<PreparedResponse, HttpFailure>> + Send;
}

/// [`HttpAdapter`] for the seams that are synchronous by design.
///
/// The X and Instagram publishing paths are blocking (`fn send(&mut self, …)`),
/// so their transports cannot await. Rather than spawn a runtime per request,
/// they take this trait and the live adapter behind it is `reqwest`'s blocking
/// client — which owns one runtime on a background thread for the process's
/// lifetime.
///
/// A blocking adapter must not be called from inside an async executor. That is
/// a constraint on the caller, documented in
/// `docs/technical/live-transports.md`, and not something this trait can
/// enforce.
pub trait BlockingHttpAdapter {
    /// Send one request and return the response.
    fn execute(&self, request: PreparedRequest) -> Result<PreparedResponse, HttpFailure>;
}

/// Borrowing an adapter borrows its ability to send.
///
/// This is what lets a transport hand out `&A` to a per-request helper instead
/// of cloning the client, without every call site naming the reference impl.
impl<T: HttpAdapter + Sync + ?Sized> HttpAdapter for &T {
    fn execute(
        &self,
        request: PreparedRequest,
    ) -> impl Future<Output = Result<PreparedResponse, HttpFailure>> + Send {
        T::execute(*self, request)
    }
}

impl<T: BlockingHttpAdapter + ?Sized> BlockingHttpAdapter for &T {
    fn execute(&self, request: PreparedRequest) -> Result<PreparedResponse, HttpFailure> {
        T::execute(*self, request)
    }
}

// ---------------------------------------------------------------------------
// Retry-After
// ---------------------------------------------------------------------------

/// A parsed `Retry-After` value.
///
/// Both RFC 9110 forms are accepted: delta-seconds, which every platform that
/// sends the header actually uses, and an HTTP date, which an intermediary in
/// front of it may substitute.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RetryAfter {
    Seconds(u64),
    At(SystemTime),
}

impl RetryAfter {
    /// Parse a header value. `None` when it is neither form.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        let trimmed = value.trim();
        if let Ok(seconds) = trimmed.parse::<u64>() {
            return Some(Self::Seconds(seconds));
        }
        httpdate::parse_http_date(trimmed).ok().map(Self::At)
    }

    /// How long to wait, relative to `now`.
    #[must_use]
    pub fn delay(self, now: SystemTime) -> Duration {
        match self {
            Self::Seconds(seconds) => Duration::from_secs(seconds),
            Self::At(at) => at.duration_since(now).unwrap_or(Duration::ZERO),
        }
    }

    /// The wait rendered back, for a message a user reads.
    #[must_use]
    pub fn describe(self, now: SystemTime) -> String {
        format!("Retry-After: {} s", self.delay(now).as_secs())
    }
}

/// The `Retry-After` of a response, if it carried a readable one.
///
/// Parsing is offered here, at the seam that hands the response back, but the
/// decision to wait is never made here: the transport returns the response
/// unchanged and whoever owns the retry loop honours or refuses the delay.
#[must_use]
pub fn retry_after(headers: &[(String, String)]) -> Option<RetryAfter> {
    header_value(headers, "Retry-After").and_then(RetryAfter::parse)
}

// ---------------------------------------------------------------------------
// Form and query encoding
// ---------------------------------------------------------------------------

/// Percent-encode one `application/x-www-form-urlencoded` component.
///
/// Space becomes `+` and everything outside the unreserved set is escaped,
/// which is the encoding Meta's Graph API specifies for form bodies.
#[must_use]
pub fn form_encode_component(value: &str) -> String {
    encode(value, true)
}

/// Percent-encode one query-string component.
///
/// Space becomes `%20` rather than `+`: both are legal, but `+` in a path or a
/// signature base is a classic source of mismatched requests.
#[must_use]
pub fn query_encode_component(value: &str) -> String {
    encode(value, false)
}

/// Encode `name=value` pairs as a form body.
#[must_use]
pub fn form_encode(pairs: &[(String, String)]) -> String {
    pairs
        .iter()
        .map(|(name, value)| {
            format!(
                "{}={}",
                form_encode_component(name),
                form_encode_component(value)
            )
        })
        .collect::<Vec<_>>()
        .join("&")
}

/// Encode `name=value` pairs as a query string.
#[must_use]
pub fn query_encode(pairs: &[(String, String)]) -> String {
    pairs
        .iter()
        .map(|(name, value)| {
            format!(
                "{}={}",
                query_encode_component(name),
                query_encode_component(value)
            )
        })
        .collect::<Vec<_>>()
        .join("&")
}

fn encode(value: &str, space_as_plus: bool) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(char::from(byte));
            }
            b' ' if space_as_plus => out.push('+'),
            _ => {
                out.push('%');
                out.push(char::from(HEX[(byte >> 4) as usize]));
                out.push(char::from(HEX[(byte & 0x0f) as usize]));
            }
        }
    }
    out
}

const HEX: [u8; 16] = *b"0123456789ABCDEF";

// ---------------------------------------------------------------------------
// The live adapters
// ---------------------------------------------------------------------------

/// The two adapters that actually open a socket, behind `live-http`.
#[cfg(feature = "live-http")]
mod live {
    use std::time::Duration;

    use super::{
        ClientConfig, HttpAdapter, HttpFailure, HttpFailureKind, HttpMethod, PreparedRequest,
        PreparedResponse, Proxy, Redirects, Retries, Tls,
    };

    /// Build a client from `config`.
    ///
    /// Every knob is set explicitly, including the ones whose default already
    /// matches, because the default is not this crate's promise — the policy
    /// value is.
    fn builder(config: &ClientConfig) -> reqwest::ClientBuilder {
        let redirects = match config.redirects {
            Redirects::NoFollow => reqwest::redirect::Policy::none(),
            Redirects::FollowUpTo(limit) => reqwest::redirect::Policy::limited(usize::from(limit)),
        };
        let tls = match config.tls {
            // Verification is the only variant. Stated as a call so that the
            // Rustls configuration is undeniably the one in force.
            Tls::Verify => true,
        };
        let mut builder = reqwest::Client::builder()
            .user_agent(config.user_agent.clone())
            .connect_timeout(config.connect_timeout)
            .timeout(config.request_timeout)
            .redirect(redirects)
            // `reqwest` 0.13 retries protocol NACKs on its own by default. That
            // is a second request nobody asked for, and for `media_publish` it
            // is a second post.
            .retry(match config.retries {
                Retries::Never => reqwest::retry::never(),
            })
            .use_rustls_tls()
            .danger_accept_invalid_certs(!tls)
            // A pooled connection that has been idle for minutes is more likely
            // to be dead than fresh; recycling early keeps a stale socket from
            // being charged against the request timeout.
            .pool_idle_timeout(Duration::from_secs(90));
        builder = match config.proxy {
            Proxy::Disabled => builder.no_proxy(),
            Proxy::FromEnvironment => builder,
        };
        builder
    }

    fn map_method(method: HttpMethod) -> reqwest::Method {
        match method {
            HttpMethod::Get => reqwest::Method::GET,
            HttpMethod::Post => reqwest::Method::POST,
            HttpMethod::Put => reqwest::Method::PUT,
            HttpMethod::Delete => reqwest::Method::DELETE,
            HttpMethod::Patch => reqwest::Method::PATCH,
        }
    }

    fn response(
        status: u16,
        headers: &reqwest::header::HeaderMap,
        body: Vec<u8>,
    ) -> PreparedResponse {
        let headers = headers
            .iter()
            .map(|(name, value)| {
                (
                    name.as_str().to_owned(),
                    String::from_utf8_lossy(value.as_bytes()).into_owned(),
                )
            })
            .collect();
        PreparedResponse {
            status,
            headers,
            body,
        }
    }

    /// Classify a client error.
    ///
    /// `reqwest` has no `is_tls()`, so a certificate failure is recognised by
    /// walking the source chain for the words every TLS stack uses. A false
    /// negative degrades to [`HttpFailureKind::Connect`], which the pipeline's
    /// retry budget caps; the alternative — reporting a permanently broken
    /// trust store as retryable forever — is worse.
    fn classify(error: reqwest::Error) -> HttpFailure {
        let kind = if error.is_timeout() {
            HttpFailureKind::Timeout
        } else if error.is_redirect() {
            HttpFailureKind::Redirect
        } else if mentions_tls(&error) {
            HttpFailureKind::Tls
        } else if error.is_connect() {
            HttpFailureKind::Connect
        } else if error.is_body() || error.is_decode() {
            HttpFailureKind::Protocol
        } else {
            HttpFailureKind::Other
        };
        // The URL is dropped from the client error before it is rendered, and
        // the caller's own URL is redacted by `HttpFailure::new`. Belt and
        // braces, because this is the one string that reaches a user.
        HttpFailure::new(kind, error.without_url().to_string())
    }

    fn mentions_tls(error: &(dyn std::error::Error + 'static)) -> bool {
        let mut source = Some(error);
        while let Some(current) = source {
            let text = current.to_string().to_ascii_lowercase();
            if text.contains("certificate")
                || text.contains("handshake")
                || text.contains("invalid peer")
                || text.contains("tls")
            {
                return true;
            }
            source = current.source();
        }
        false
    }

    /// The async adapter: `reqwest` with rustls, for the async transport seams.
    pub struct ReqwestAdapter {
        client: reqwest::Client,
    }

    impl ReqwestAdapter {
        /// Build an adapter from `config`.
        ///
        /// Fails only when the TLS/HTTP stack cannot be initialised, which is a
        /// process-level problem rather than a request-level one.
        pub fn new(config: &ClientConfig) -> Result<Self, HttpFailure> {
            let client = builder(config).build().map_err(|error| {
                HttpFailure::new(
                    HttpFailureKind::Other,
                    format!("could not build the HTTP client: {error}"),
                )
            })?;
            Ok(Self { client })
        }

        /// Adopt a client the caller configured itself.
        #[must_use]
        pub fn with_client(client: reqwest::Client) -> Self {
            Self { client }
        }

        #[must_use]
        pub fn client(&self) -> &reqwest::Client {
            &self.client
        }
    }

    impl HttpAdapter for ReqwestAdapter {
        fn execute(
            &self,
            request: PreparedRequest,
        ) -> impl std::future::Future<Output = Result<PreparedResponse, HttpFailure>> + Send
        {
            let PreparedRequest {
                method,
                url,
                headers,
                body,
            } = request;
            async move {
                let mut builder = self.client.request(map_method(method), &url);
                for (name, value) in headers {
                    builder = builder.header(name, value);
                }
                let answer = builder.body(body).send().await.map_err(classify)?;
                let status = answer.status().as_u16();
                let headers = answer.headers().clone();
                let bytes = answer.bytes().await.map_err(classify)?;
                Ok(response(status, &headers, bytes.to_vec()))
            }
        }
    }

    /// The blocking adapter, for the seams that are synchronous by design.
    ///
    /// `reqwest`'s blocking client owns a runtime on a background thread, so it
    /// must not be called from inside an async executor: the provider crates
    /// whose `Transport` trait is synchronous are driven from a blocking
    /// context (see `docs/technical/live-transports.md`).
    pub struct ReqwestBlockingAdapter {
        client: reqwest::blocking::Client,
    }

    impl ReqwestBlockingAdapter {
        pub fn new(config: &ClientConfig) -> Result<Self, HttpFailure> {
            let redirects = match config.redirects {
                Redirects::NoFollow => reqwest::redirect::Policy::none(),
                Redirects::FollowUpTo(limit) => {
                    reqwest::redirect::Policy::limited(usize::from(limit))
                }
            };
            let mut builder = reqwest::blocking::Client::builder()
                .user_agent(config.user_agent.clone())
                .connect_timeout(config.connect_timeout)
                .timeout(config.request_timeout)
                .redirect(redirects)
                .retry(match config.retries {
                    Retries::Never => reqwest::retry::never(),
                })
                .use_rustls_tls()
                .danger_accept_invalid_certs(false)
                .pool_idle_timeout(Duration::from_secs(90));
            builder = match config.proxy {
                Proxy::Disabled => builder.no_proxy(),
                Proxy::FromEnvironment => builder,
            };
            let client = builder.build().map_err(|error| {
                HttpFailure::new(
                    HttpFailureKind::Other,
                    format!("could not build the HTTP client: {error}"),
                )
            })?;
            Ok(Self { client })
        }

        #[must_use]
        pub fn with_client(client: reqwest::blocking::Client) -> Self {
            Self { client }
        }

        #[must_use]
        pub fn client(&self) -> &reqwest::blocking::Client {
            &self.client
        }
    }

    impl super::BlockingHttpAdapter for ReqwestBlockingAdapter {
        fn execute(&self, request: PreparedRequest) -> Result<PreparedResponse, HttpFailure> {
            let PreparedRequest {
                method,
                url,
                headers,
                body,
            } = request;
            let mut builder = self.client.request(map_method(method), &url);
            for (name, value) in headers {
                builder = builder.header(name, value);
            }
            let answer = builder.body(body).send().map_err(classify)?;
            let status = answer.status().as_u16();
            let headers = answer.headers().clone();
            let bytes = answer.bytes().map_err(classify)?;
            Ok(response(status, &headers, bytes.to_vec()))
        }
    }
}

#[cfg(feature = "live-http")]
pub use live::{ReqwestAdapter, ReqwestBlockingAdapter};

/// A test double for [`HttpAdapter`], so each provider's tests do not
/// re-implement it.
///
/// It records what it was asked to send and answers from a script. It never
/// opens a socket, which is what keeps `cargo test` offline.
#[cfg(feature = "live-http")]
pub mod fake {
    use std::collections::VecDeque;
    use std::future::Future;
    use std::sync::Mutex;

    use super::{HttpAdapter, HttpFailure, HttpFailureKind, PreparedRequest, PreparedResponse};

    /// Records requests and replays scripted responses.
    #[derive(Default)]
    pub struct RecordingAdapter {
        requests: Mutex<Vec<PreparedRequest>>,
        script: Mutex<VecDeque<Result<PreparedResponse, HttpFailure>>>,
    }

    impl RecordingAdapter {
        /// An adapter with nothing scripted: every call fails, which is a
        /// useful default for a test that only inspects the request.
        #[must_use]
        pub fn new() -> Self {
            Self::default()
        }

        /// An adapter that answers from `steps`, in order.
        #[must_use]
        pub fn scripted(steps: Vec<Result<PreparedResponse, HttpFailure>>) -> Self {
            Self {
                requests: Mutex::new(Vec::new()),
                script: Mutex::new(steps.into()),
            }
        }

        /// Every request the transport handed over, in order.
        #[must_use]
        pub fn requests(&self) -> Vec<PreparedRequest> {
            self.requests
                .lock()
                .expect("recording adapter lock")
                .clone()
        }

        /// How many requests were made.
        #[must_use]
        pub fn count(&self) -> usize {
            self.requests.lock().expect("recording adapter lock").len()
        }
    }

    impl HttpAdapter for RecordingAdapter {
        fn execute(
            &self,
            request: PreparedRequest,
        ) -> impl Future<Output = Result<PreparedResponse, HttpFailure>> + Send {
            let next = self.record(request);
            async move { next }
        }
    }

    impl super::BlockingHttpAdapter for RecordingAdapter {
        fn execute(&self, request: PreparedRequest) -> Result<PreparedResponse, HttpFailure> {
            self.record(request)
        }
    }

    impl RecordingAdapter {
        /// Record the request and pop the next scripted answer.
        fn record(&self, request: PreparedRequest) -> Result<PreparedResponse, HttpFailure> {
            self.requests
                .lock()
                .expect("recording adapter lock")
                .push(request);
            self.script
                .lock()
                .expect("recording adapter script lock")
                .pop_front()
                .unwrap_or_else(|| {
                    Err(HttpFailure::new(
                        HttpFailureKind::Other,
                        "the recording adapter had no response scripted",
                    ))
                })
        }
    }

    /// A plain response, for a script.
    #[must_use]
    pub fn reply(status: u16, body: impl Into<Vec<u8>>) -> PreparedResponse {
        PreparedResponse::new(status).with_body(body.into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_user_agent_names_the_application_and_its_version() {
        let agent = user_agent();
        assert!(agent.starts_with("xPST/"), "{agent}");
        assert_eq!(agent, format!("xPST/{}", env!("CARGO_PKG_VERSION")));
        assert!(agent.len() > "xPST/".len());
    }

    #[test]
    fn the_default_policy_is_the_safe_one() {
        let config = ClientConfig::for_destination();
        assert_eq!(config.redirects, Redirects::NoFollow);
        assert_eq!(config.proxy, Proxy::Disabled);
        assert_eq!(config.tls, Tls::Verify);
        assert_eq!(config.retries, Retries::Never);
        assert_eq!(config.connect_timeout, Duration::from_secs(10));
        assert_eq!(config.request_timeout, Duration::from_secs(300));
        assert!(config.connect_timeout < config.request_timeout);
        assert_eq!(config.user_agent, user_agent());
    }

    #[test]
    fn a_query_parameter_that_is_a_credential_is_redacted() {
        let url = "https://graph.facebook.com/v23.0/123/media?fields=id&access_token=SECRET-VALUE";
        let redacted = redact_url(url);
        assert!(!redacted.contains("SECRET-VALUE"), "{redacted}");
        assert!(redacted.contains("access_token=REDACTED"), "{redacted}");
        // The useful half survives: a redacted 404 must still be debuggable.
        assert!(redacted.contains("fields=id"), "{redacted}");
        assert!(redacted.contains("graph.facebook.com"), "{redacted}");
    }

    #[test]
    fn a_resumable_session_id_is_treated_as_a_capability() {
        let url = "https://upload.example/videos?uploadType=resumable&upload_id=S1";
        let redacted = redact_url(url);
        assert!(redacted.contains("upload_id=REDACTED"), "{redacted}");
        assert!(redacted.contains("uploadType=resumable"), "{redacted}");
    }

    #[test]
    fn userinfo_in_a_url_is_redacted() {
        let redacted = redact_url("https://user:pass@example.com/path?x=1");
        assert!(!redacted.contains("user"), "{redacted}");
        assert!(!redacted.contains("pass"), "{redacted}");
        assert!(redacted.contains("REDACTED@example.com/path"), "{redacted}");
    }

    #[test]
    fn a_url_without_a_query_is_unchanged() {
        let url = "https://upload.youtube.com/upload?uploadType=resumable";
        assert_eq!(redact_url(url), url);
        assert_eq!(
            redact_url("https://bsky.social/xrpc/com.atproto.repo.createRecord"),
            "https://bsky.social/xrpc/com.atproto.repo.createRecord"
        );
    }

    #[test]
    fn redact_text_rewrites_a_url_inside_a_sentence() {
        let message = "error sending request for url (https://api.example/v1?access_token=SECRET)";
        let redacted = redact_text(message);
        assert!(!redacted.contains("SECRET"), "{redacted}");
        assert!(
            redacted.starts_with("error sending request for url ("),
            "{redacted}"
        );
        assert!(redacted.ends_with(')'), "{redacted}");
    }

    #[test]
    fn a_request_never_renders_its_authorization_header_or_body() {
        let request = PreparedRequest::new(HttpMethod::Post, "https://api.x.com/2/tweets")
            .with_header("Authorization", "OAuth oauth_signature=SECRET-SIG")
            .with_header("Content-Type", "application/json")
            .with_body(br#"{"text":"secret body"}"#.to_vec());

        let rendered = format!("{request:?}");
        assert!(!rendered.contains("SECRET-SIG"), "{rendered}");
        assert!(!rendered.contains("secret body"), "{rendered}");
        assert!(rendered.contains("Authorization: <redacted>"), "{rendered}");
        assert!(
            rendered.contains("Content-Type: application/json"),
            "{rendered}"
        );
        assert!(rendered.contains("body_bytes: 22"), "{rendered}");
    }

    #[test]
    fn a_response_never_renders_its_body() {
        let response = PreparedResponse::new(401)
            .with_header("Set-Cookie", "session=SECRET-COOKIE")
            .with_body(b"{\"token\":\"SECRET-TOKEN\"}".to_vec());
        let rendered = format!("{response:?}");
        assert!(!rendered.contains("SECRET-COOKIE"), "{rendered}");
        assert!(!rendered.contains("SECRET-TOKEN"), "{rendered}");
        assert!(rendered.contains("status: 401"), "{rendered}");
    }

    #[test]
    fn a_failure_detail_has_its_urls_redacted_on_construction() {
        let failure = HttpFailure::new(
            HttpFailureKind::Connect,
            "dns error while sending https://graph.facebook.com/v23.0/1/media?access_token=SECRET",
        );
        assert!(!failure.to_string().contains("SECRET"), "{failure}");
        assert!(
            failure.to_string().contains("access_token=REDACTED"),
            "{failure}"
        );
        assert_eq!(failure.kind, HttpFailureKind::Connect);
    }

    #[test]
    fn retry_after_accepts_both_rfc_forms() {
        assert_eq!(RetryAfter::parse("120"), Some(RetryAfter::Seconds(120)));
        assert_eq!(RetryAfter::parse(" 0 "), Some(RetryAfter::Seconds(0)));
        assert!(matches!(
            RetryAfter::parse("Wed, 21 Oct 2015 07:28:00 GMT"),
            Some(RetryAfter::At(_))
        ));
        assert_eq!(RetryAfter::parse("soon"), None);
    }

    #[test]
    fn retry_after_is_read_case_insensitively_from_a_header_list() {
        let headers = vec![("retry-after".to_owned(), "42".to_owned())];
        assert_eq!(retry_after(&headers), Some(RetryAfter::Seconds(42)));
        assert_eq!(retry_after(&[]), None);
    }

    #[test]
    fn an_http_date_in_the_past_means_no_wait() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(2_000_000_000);
        let value = RetryAfter::parse("Wed, 21 Oct 2015 07:28:00 GMT").expect("a date");
        assert_eq!(value.delay(now), Duration::ZERO);
        assert_eq!(value.describe(now), "Retry-After: 0 s");
    }

    #[test]
    fn form_and_query_encoding_differ_only_in_how_a_space_is_written() {
        let pairs = vec![
            ("caption".to_owned(), "hello world & more".to_owned()),
            ("access_token".to_owned(), "abc/def".to_owned()),
        ];
        assert_eq!(
            form_encode(&pairs),
            "caption=hello+world+%26+more&access_token=abc%2Fdef"
        );
        assert_eq!(
            query_encode(&pairs),
            "caption=hello%20world%20%26%20more&access_token=abc%2Fdef"
        );
    }

    #[test]
    fn only_get_put_and_delete_are_idempotent() {
        assert!(HttpMethod::Get.is_idempotent());
        assert!(HttpMethod::Put.is_idempotent());
        assert!(HttpMethod::Delete.is_idempotent());
        assert!(!HttpMethod::Post.is_idempotent());
        assert!(!HttpMethod::Patch.is_idempotent());
    }

    #[test]
    fn describe_names_the_method_and_never_the_query_credentials() {
        let request = PreparedRequest::new(
            HttpMethod::Get,
            "https://graph.example/1?access_token=SECRET",
        );
        assert_eq!(
            request.describe(),
            "GET https://graph.example/1?access_token=REDACTED"
        );
    }

    /// Building a client dials nothing, and it is the only way to prove that
    /// `reqwest` accepts this combination of knobs (no redirects, no retries,
    /// rustls, no proxy) rather than silently rejecting one at request time.
    #[cfg(feature = "live-http")]
    #[test]
    fn the_live_adapters_build_from_the_default_policy() {
        let config = ClientConfig::for_destination();
        assert!(ReqwestAdapter::new(&config).is_ok());
        assert!(ReqwestBlockingAdapter::new(&config).is_ok());
    }
}
