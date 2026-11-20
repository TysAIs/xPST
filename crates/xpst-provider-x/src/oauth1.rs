//! OAuth 1.0a User Context signing (HMAC-SHA1), RFC 5849 §3.4.
//!
//! X's v2 write endpoints require OAuth 1.0a user context for
//! `POST /2/tweets` and the v1.1 media upload commands. The signing rules that
//! matter here:
//!
//! * the signature base is `METHOD & percent(url without query) &
//!   percent(sorted encoded parameters)` where the parameter set includes the
//!   `oauth_*` fields (except `oauth_signature`), any query parameters, and —
//!   **only for `application/x-www-form-urlencoded` bodies** — the form fields;
//! * both the key and the value of every parameter are percent-encoded with the
//!   RFC 3986 unreserved set, then sorted byte-wise by encoded key then encoded
//!   value;
//! * multipart and JSON bodies are *not* in the parameter set — only the OAuth
//!   fields and the query string are signed.
//!
//! Each request carries fresh `oauth_nonce`/`oauth_timestamp`, so no request can
//! be replayed and two identical posts do not share a signature.
//!
//! The signature arithmetic is pinned against vectors produced independently by
//! Python's `hmac`/`hashlib` (`tests/oauth1_vectors.rs`,
//! `scripts/capture_x_oauth1_vectors.py`) — not against this implementation's
//! own output.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use hmac::{Hmac, Mac};
use sha1::Sha1;

use crate::transport::Method;

type HmacSha1 = Hmac<Sha1>;

static NONCE_COUNTER: AtomicU64 = AtomicU64::new(0);

/// The four secrets X issues. Never logged, never serialised by this crate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Credentials {
    pub consumer_key: String,
    pub consumer_secret: String,
    pub access_token: String,
    pub access_token_secret: String,
}

impl Credentials {
    #[must_use]
    pub fn new(
        consumer_key: impl Into<String>,
        consumer_secret: impl Into<String>,
        access_token: impl Into<String>,
        access_token_secret: impl Into<String>,
    ) -> Self {
        Self {
            consumer_key: consumer_key.into(),
            consumer_secret: consumer_secret.into(),
            access_token: access_token.into(),
            access_token_secret: access_token_secret.into(),
        }
    }

    /// Whether all four secrets are present; a missing one is a `needs_reauth`
    /// condition, not a retryable error.
    #[must_use]
    pub fn is_complete(&self) -> bool {
        !self.consumer_key.trim().is_empty()
            && !self.consumer_secret.trim().is_empty()
            && !self.access_token.trim().is_empty()
            && !self.access_token_secret.trim().is_empty()
    }
}

/// HMAC-SHA1 OAuth 1.0a signer.
#[derive(Debug, Clone)]
pub struct OAuth1Signer {
    credentials: Credentials,
}

impl OAuth1Signer {
    #[must_use]
    pub fn new(credentials: Credentials) -> Self {
        Self { credentials }
    }

    #[must_use]
    pub fn credentials(&self) -> &Credentials {
        &self.credentials
    }

    /// Build the `Authorization: OAuth …` header value.
    ///
    /// `body_params` must be the form fields for an
    /// `application/x-www-form-urlencoded` request and empty for JSON or
    /// multipart bodies. `nonce` and `timestamp` are parameters rather than
    /// internal state so the signature is reproducible in tests and fixtures.
    #[must_use]
    pub fn authorization_header(
        &self,
        method: Method,
        url: &str,
        body_params: &[(String, String)],
        nonce: &str,
        timestamp: i64,
    ) -> String {
        let timestamp = timestamp.to_string();
        let mut oauth_params: Vec<(String, String)> = vec![
            (
                "oauth_consumer_key".into(),
                self.credentials.consumer_key.clone(),
            ),
            ("oauth_nonce".into(), nonce.into()),
            ("oauth_signature_method".into(), "HMAC-SHA1".into()),
            ("oauth_timestamp".into(), timestamp),
            ("oauth_token".into(), self.credentials.access_token.clone()),
            ("oauth_version".into(), "1.0".into()),
        ];
        oauth_params.sort_by(|a, b| a.0.cmp(&b.0));

        let mut params: Vec<(String, String)> = oauth_params
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        params.extend(query_params(url));
        params.extend(body_params.iter().cloned());

        let signature = self.signature(method, url, &params);
        oauth_params.push(("oauth_signature".into(), signature));
        oauth_params.sort_by(|a, b| a.0.cmp(&b.0));

        let fields: Vec<String> = oauth_params
            .iter()
            .map(|(key, value)| {
                format!("\"{}\"=\"{}\"", percent_encode(key), percent_encode(value))
            })
            .collect();
        format!("OAuth {}", fields.join(", "))
    }

    /// The base64 HMAC-SHA1 signature for an already-assembled parameter set.
    ///
    /// Public so the fixture test can assert the exact value; callers should
    /// use [`Self::authorization_header`].
    #[must_use]
    pub fn signature(&self, method: Method, url: &str, params: &[(String, String)]) -> String {
        let base = signature_base(method, url, params);
        let key = format!(
            "{}&{}",
            percent_encode(&self.credentials.consumer_secret),
            percent_encode(&self.credentials.access_token_secret)
        );
        let mut mac =
            HmacSha1::new_from_slice(key.as_bytes()).expect("HMAC accepts a key of any length");
        mac.update(base.as_bytes());
        base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes())
    }
}

/// RFC 3986 percent-encoding with the unreserved set left intact.
///
/// Spaces become `%20` (never `+`): OAuth signs the encoded form, and a `+`
/// would sign a different string than X validates.
#[must_use]
pub fn percent_encode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(char::from(byte));
            }
            _ => {
                out.push('%');
                out.push(hex_digit(byte >> 4));
                out.push(hex_digit(byte & 0x0f));
            }
        }
    }
    out
}

const fn hex_digit(nibble: u8) -> char {
    match nibble {
        0..=9 => (b'0' + nibble) as char,
        _ => (b'A' + (nibble - 10)) as char,
    }
}

fn signature_base(method: Method, url: &str, params: &[(String, String)]) -> String {
    let mut encoded: Vec<(String, String)> = params
        .iter()
        .map(|(key, value)| (percent_encode(key), percent_encode(value)))
        .collect();
    encoded.sort();
    let param_string = encoded
        .iter()
        .map(|(key, value)| format!("{key}={value}"))
        .collect::<Vec<_>>()
        .join("&");
    let base_url = url.split('?').next().unwrap_or(url);
    format!(
        "{}&{}&{}",
        method.as_str(),
        percent_encode(base_url),
        percent_encode(&param_string)
    )
}

fn query_params(url: &str) -> Vec<(String, String)> {
    let Some(query) = url.split_once('?').map(|(_, query)| query) else {
        return Vec::new();
    };
    query
        .split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| match pair.split_once('=') {
            Some((key, value)) => (percent_decode(key), percent_decode(value)),
            None => (percent_decode(pair), String::new()),
        })
        .collect()
}

/// Minimal decoder for query parameters we ourselves produce.
fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            if let (Some(high), Some(low)) = (
                (bytes[index + 1] as char).to_digit(16),
                (bytes[index + 2] as char).to_digit(16),
            ) {
                out.push((high * 16 + low) as u8);
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// A nonce that never repeats within this process.
#[must_use]
pub fn fresh_nonce() -> String {
    let counter = NONCE_COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    format!("{nanos:032x}{counter:016x}")
}

/// Seconds since the Unix epoch, the unit `oauth_timestamp` uses.
#[must_use]
pub fn unix_timestamp() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or_default()
}
