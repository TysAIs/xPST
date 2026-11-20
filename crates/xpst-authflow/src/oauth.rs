//! Authorization-URL assembly and redirect parsing.
//!
//! No URL crate: the two operations this crate needs are percent-encoding a
//! query parameter and reading `code`/`state`/`error` out of a redirect. Both
//! are small enough to be audited here and tested against RFC 3986 examples.
//! `+` is NOT decoded as a space: authorization codes are opaque, and rewriting
//! one would exchange the wrong code.

use crate::error::AuthFlowError;
use crate::pkce::StateToken;

/// Percent-encode everything outside RFC 3986's unreserved set.
#[must_use]
pub fn percent_encode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            out.push(char::from(*byte));
        } else {
            out.push('%');
            out.push(hex_digit(byte >> 4));
            out.push(hex_digit(byte & 0x0f));
        }
    }
    out
}

fn hex_digit(nibble: u8) -> char {
    char::from(b"0123456789ABCDEF"[usize::from(nibble & 0x0f)])
}

/// Decode `%XX` sequences. Invalid escapes are an error, never a guess.
pub fn percent_decode(value: &str) -> Result<String, AuthFlowError> {
    let bytes = value.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' => {
                if index + 2 >= bytes.len() {
                    return Err(AuthFlowError::InvalidInput(
                        "truncated percent-encoding in redirect".to_string(),
                    ));
                }
                let high = hex_value(bytes[index + 1]).ok_or_else(|| {
                    AuthFlowError::InvalidInput("invalid percent-encoding in redirect".to_string())
                })?;
                let low = hex_value(bytes[index + 2]).ok_or_else(|| {
                    AuthFlowError::InvalidInput("invalid percent-encoding in redirect".to_string())
                })?;
                out.push((high << 4) | low);
                index += 3;
            }
            other => {
                out.push(other);
                index += 1;
            }
        }
    }
    String::from_utf8(out)
        .map_err(|_| AuthFlowError::InvalidInput("redirect query is not valid UTF-8".to_string()))
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// Build an absolute authorize URL from ordered parameters.
///
/// The endpoint must be `http://` or `https://`; parameters are encoded, and an
/// empty value is allowed only because a provider may document one.
pub fn build_authorize_url(
    endpoint: &str,
    params: &[(&str, &str)],
) -> Result<String, AuthFlowError> {
    if !(endpoint.starts_with("https://") || endpoint.starts_with("http://")) {
        return Err(AuthFlowError::InvalidConfig(format!(
            "authorize endpoint {endpoint:?} is not an absolute http(s) URL"
        )));
    }
    let mut url = String::with_capacity(endpoint.len() + 128);
    url.push_str(endpoint);
    url.push(if endpoint.contains('?') { '&' } else { '?' });
    for (index, (key, value)) in params.iter().enumerate() {
        if index > 0 {
            url.push('&');
        }
        url.push_str(&percent_encode(key));
        url.push('=');
        url.push_str(&percent_encode(value));
    }
    Ok(url)
}

/// Query parameters of a URL, decoded. Duplicate keys are preserved so the
/// caller can reject an ambiguous redirect instead of silently choosing one.
pub fn query_pairs(url: &str) -> Result<Vec<(String, String)>, AuthFlowError> {
    let without_fragment = url.split('#').next().unwrap_or(url);
    let query = match without_fragment.split_once('?') {
        Some((_, query)) => query,
        None => return Ok(Vec::new()),
    };
    let mut pairs = Vec::new();
    for part in query.split('&').filter(|part| !part.is_empty()) {
        let (key, value) = part.split_once('=').unwrap_or((part, ""));
        pairs.push((percent_decode(key)?, percent_decode(value)?));
    }
    Ok(pairs)
}

/// The code a verified redirect carries.
pub struct VerifiedRedirect {
    /// The authorization code, already state-checked.
    pub code: String,
}
/// Verify a redirect URL against the flow's state and redirect URI, then
/// return its code.
///
/// Order matters: the target is checked, then the state, then the provider's
/// error, and only then the code. A redirect with the wrong state never yields
/// a code even if it also carries one.
pub fn parse_redirect(
    url: &str,
    redirect_uri: &str,
    state: &StateToken,
) -> Result<VerifiedRedirect, AuthFlowError> {
    let target = normalize_target(url)?;
    let expected = normalize_target(redirect_uri)?;
    if target != expected {
        return Err(AuthFlowError::RedirectUriMismatch {
            expected,
            got: target,
        });
    }

    let mut code: Option<String> = None;
    let mut returned_state: Option<String> = None;
    let mut error: Option<String> = None;
    let mut error_description: Option<String> = None;

    for (key, value) in query_pairs(url)? {
        match key.as_str() {
            "code" => {
                if code.replace(value).is_some() {
                    return Err(AuthFlowError::InvalidInput(
                        "redirect carried more than one authorization code".to_string(),
                    ));
                }
            }
            "state" => {
                if returned_state.replace(value).is_some() {
                    return Err(AuthFlowError::InvalidInput(
                        "redirect carried more than one state value".to_string(),
                    ));
                }
            }
            "error" => error = Some(value),
            "error_description" => error_description = Some(value),
            _ => {}
        }
    }

    let returned_state = returned_state.ok_or(AuthFlowError::StateMismatch)?;
    state.verify(&returned_state)?;

    if let Some(code) = error {
        return Err(AuthFlowError::ProviderError {
            code,
            description: error_description.unwrap_or_default(),
        });
    }

    let code = code.ok_or(AuthFlowError::MissingCode)?;
    if code.is_empty() {
        return Err(AuthFlowError::MissingCode);
    }
    Ok(VerifiedRedirect { code })
}

/// `scheme://authority/path`, with scheme and authority lower-cased.
///
/// The path stays case-sensitive: `/callback` and `/Callback` are different
/// registrations. A query or fragment in the registered URI is rejected by
/// [`normalize_target`] because this crate registers query-free redirect URIs.
pub fn normalize_target(url: &str) -> Result<String, AuthFlowError> {
    let without_fragment = url.split('#').next().unwrap_or(url);
    let cut = without_fragment.find('?').unwrap_or(without_fragment.len());
    let target = &without_fragment[..cut];
    let (scheme, rest) = target.split_once("://").ok_or_else(|| {
        AuthFlowError::InvalidConfig(format!("{url:?} is not an absolute redirect URL"))
    })?;
    if scheme.is_empty() || rest.is_empty() {
        return Err(AuthFlowError::InvalidConfig(format!(
            "{url:?} is not an absolute redirect URL"
        )));
    }
    let (authority, path) = match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, "/"),
    };
    Ok(format!(
        "{}://{}{}",
        scheme.to_ascii_lowercase(),
        authority.to_ascii_lowercase(),
        path
    ))
}

/// True when the URI's host is a literal loopback name, per RFC 8252 §7.3.
#[must_use]
pub fn is_loopback_uri(uri: &str) -> bool {
    let Some((_, rest)) = uri.split_once("://") else {
        return false;
    };
    let host_port = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = if let Some(stripped) = host_port.strip_prefix('[') {
        stripped.split(']').next().unwrap_or("")
    } else {
        host_port.split(':').next().unwrap_or("")
    };
    matches!(host, "127.0.0.1" | "localhost" | "::1")
}

#[cfg(test)]
mod tests {
    use super::{percent_decode, percent_encode};

    #[test]
    fn rfc3986_unreserved_stays_literal() {
        assert_eq!(percent_encode("aZ09-._~"), "aZ09-._~");
    }

    #[test]
    fn reserved_and_utf8_are_encoded() {
        assert_eq!(percent_encode("a b&c=d/é"), "a%20b%26c%3Dd%2F%C3%A9");
        assert_eq!(percent_decode("a%20b%26c").ok().as_deref(), Some("a b&c"));
    }

    #[test]
    fn plus_is_not_a_space() {
        assert_eq!(percent_decode("a+b").ok().as_deref(), Some("a+b"));
    }

    #[test]
    fn truncated_escape_is_an_error() {
        assert!(percent_decode("abc%2").is_err());
        assert!(percent_decode("abc%ZZ").is_err());
    }
}
