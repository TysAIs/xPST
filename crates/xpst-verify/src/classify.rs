//! Turning one attempt into one verdict.
//!
//! This module is the whole honesty surface of the smoke, so it is a pure
//! function with no I/O and no clock: [`Attempt`] is what actually happened,
//! [`Verdict`] is what may be claimed about it, and the mapping between them is
//! total.
//!
//! Two rules drive the mapping:
//!
//! * **A status code is evidence about the request, not about the account.**
//!   `401` means the credential presented was refused, `403` means it was
//!   understood and not allowed. A `429` or a `5xx` says nothing about either —
//!   that is [`Verdict::Network`] ("no verdict"), never "rejected".
//! * **"Expired" is only claimable when a refresh could fix it.** A refused
//!   credential with a refresh token behind it is expired; the same refusal for
//!   a session cookie that no refresh can renew is rejected, because telling
//!   someone to wait for a refresh that cannot happen is the failure mode this
//!   classification exists to prevent.

use crate::platform::Platform;

/// What a single attempt actually produced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Attempt {
    /// No credential is present on the auth path, so nothing was sent.
    NoCredential { detail: String },
    /// A credential exists but not in a shape the read-only identity call
    /// accepts, so nothing was sent.
    Unsupported { detail: String },
    /// A request was sent once and a status line came back.
    Status {
        /// The HTTP status code.
        code: u16,
        /// The reply body, kept only so a label can be read out of a 2xx. It is
        /// never printed.
        body: String,
    },
    /// The request was sent and no reply arrived: connection refused, DNS
    /// failure, TLS failure.
    Transport { detail: String },
    /// The request was sent and the reply did not arrive within the timeout.
    Timeout { millis: u64 },
}

/// What may be claimed about one platform.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    /// The provider accepted the credential and named the account.
    Authenticated {
        /// The account handle or title, when the reply carried one.
        label: Option<String>,
    },
    /// No credential on the auth path.
    NoCredential {
        /// Why, in non-secret terms.
        detail: String,
    },
    /// The credential was refused and a refresh could renew it.
    Expired {
        /// The evidence.
        detail: String,
    },
    /// The provider refused the credential outright (or forbade the call).
    Rejected {
        /// The evidence.
        detail: String,
    },
    /// The attempt reached no verdict: no reply, a timeout, a rate limit, a
    /// server error.
    Network {
        /// The evidence.
        detail: String,
    },
    /// A credential exists but not in a shape this read-only call can use.
    Unsupported {
        /// Why.
        detail: String,
    },
}

impl Verdict {
    /// The stable machine name, matching the report's `verdict` field.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Authenticated { .. } => "authenticated",
            Self::NoCredential { .. } => "no_credential",
            Self::Expired { .. } => "expired",
            Self::Rejected { .. } => "rejected",
            Self::Network { .. } => "network",
            Self::Unsupported { .. } => "unsupported",
        }
    }

    /// Whether the credential is proven usable against the live provider.
    #[must_use]
    pub fn proven_live(&self) -> bool {
        matches!(self, Self::Authenticated { .. })
    }

    /// The human-readable part of the verdict.
    #[must_use]
    pub fn detail(&self) -> &str {
        match self {
            Self::Authenticated { label: Some(l) } => l,
            Self::Authenticated { label: None } => "(reply carried no account label)",
            Self::NoCredential { detail }
            | Self::Expired { detail }
            | Self::Rejected { detail }
            | Self::Network { detail }
            | Self::Unsupported { detail } => detail,
        }
    }
}

/// Everything the classification needs beyond the attempt itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Context {
    /// Which platform answered — selects the label field.
    pub platform: Platform,
    /// A refresh path exists for this credential, so a refusal is recoverable.
    pub refreshable: bool,
}

impl Context {
    /// A context for one platform.
    #[must_use]
    pub fn new(platform: Platform, refreshable: bool) -> Self {
        Self {
            platform,
            refreshable,
        }
    }
}

/// Classify one attempt. Never panics, never guesses.
#[must_use]
pub fn classify(attempt: &Attempt, ctx: Context) -> Verdict {
    match attempt {
        Attempt::NoCredential { detail } => Verdict::NoCredential {
            detail: detail.clone(),
        },
        Attempt::Unsupported { detail } => Verdict::Unsupported {
            detail: detail.clone(),
        },
        Attempt::Timeout { millis } => Verdict::Network {
            detail: format!("no reply within {millis} ms"),
        },
        Attempt::Transport { detail } => Verdict::Network {
            detail: format!("no reply: {detail}"),
        },
        Attempt::Status { code, body } => classify_status(*code, body, ctx),
    }
}

fn classify_status(code: u16, body: &str, ctx: Context) -> Verdict {
    let base = match reason(body) {
        Some(reason) => format!("HTTP {code}: {reason}"),
        None => format!("HTTP {code}"),
    };
    match code {
        200..=299 => Verdict::Authenticated {
            label: ctx.platform.label_from_body(body),
        },
        401 if ctx.refreshable => Verdict::Expired {
            detail: format!("{base} (credential refused; refresh available but not attempted)"),
        },
        401 => Verdict::Rejected {
            detail: format!("{base} (credential refused; no refresh path for this shape)"),
        },
        403 => Verdict::Rejected {
            detail: format!("{base} (credential understood, call not permitted)"),
        },
        // Nothing about the credential is proven by a rate limit, a server
        // error or an unexpected redirect.
        429 => Verdict::Network {
            detail: format!("{base} (rate limited; credential unproven)"),
        },
        500..=599 => Verdict::Network {
            detail: format!("{base} (server error; credential unproven)"),
        },
        300..=399 => Verdict::Network {
            detail: format!("{base} (unexpected redirect; credential unproven)"),
        },
        400..=499 => Verdict::Rejected { detail: base },
        _ => Verdict::Network {
            detail: format!("{base} (unexpected status; credential unproven)"),
        },
    }
}

/// Pull a short, non-secret reason out of an error body.
///
/// Providers put the useful token in one of a few documented fields; when none
/// is present the verdict carries the status alone rather than echoing a body
/// that could contain identifiers.
fn reason(body: &str) -> Option<String> {
    let json: serde_json::Value = serde_json::from_str(body).ok()?;
    let mut found: Option<&str> = None;
    for key in ["error_description", "error", "message", "detail"] {
        match json.get(key) {
            Some(serde_json::Value::String(text)) => {
                found = Some(text);
            }
            // Google nests it: {"error": {"code": 401, "message": "…"}}
            Some(serde_json::Value::Object(inner)) => {
                if let Some(text) = inner.get("message").and_then(serde_json::Value::as_str) {
                    found = Some(text);
                    break;
                }
                if let Some(text) = inner.get("status").and_then(serde_json::Value::as_str) {
                    found = Some(text);
                    break;
                }
            }
            _ => {}
        }
        if found.is_some() {
            break;
        }
    }
    found.map(sanitize)
}

/// Collapse whitespace, drop control characters, cap the length.
fn sanitize(text: &str) -> String {
    let collapsed = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let cleaned: String = collapsed
        .chars()
        .filter(|c| c.is_ascii_graphic() || *c == ' ')
        .collect();
    const MAX: usize = 80;
    if cleaned.chars().count() > MAX {
        let mut truncated: String = cleaned.chars().take(MAX).collect();
        truncated.push('…');
        truncated
    } else {
        cleaned
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unsupported_and_missing_are_reported_as_themselves() {
        let missing = Attempt::NoCredential {
            detail: "no token".to_owned(),
        };
        assert_eq!(
            classify(&missing, Context::new(Platform::X, false)),
            Verdict::NoCredential {
                detail: "no token".to_owned()
            }
        );
    }

    #[test]
    fn rate_limits_are_not_rejections() {
        let limited = Attempt::Status {
            code: 429,
            body: String::new(),
        };
        assert_eq!(
            classify(&limited, Context::new(Platform::Instagram, false)).kind(),
            "network"
        );
    }

    #[test]
    fn sanitize_caps_and_strips_control_characters() {
        let noisy = format!("line\nbreak\u{7}{}", "x".repeat(200));
        let clean = sanitize(&noisy);
        assert!(clean.contains("line break"));
        assert!(clean.chars().count() <= 81);
    }
}
