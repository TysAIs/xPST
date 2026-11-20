//! Honest failure reporting for the X path.
//!
//! Every refusal names its reason with a stable `X_*` code and, where the number
//! exists, the number: the measured character count for an over-length post, the
//! epoch second a rate limit lifts, the account that already published an asset.
//! A caller should never have to parse English to decide whether to retry, stop,
//! or ask the user to re-authenticate.
//!
//! `needs_reauth` is its own answer rather than a flavour of "unauthorized":
//! repeated retries against expired credentials are how a soft block becomes a
//! suspension, so the caller must be able to see that the only useful next step
//! is a human completing OAuth again.

use serde_json::Value;

use crate::transport::Response;

/// Why an X publish or upload did not happen.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum XError {
    /// The credentials are missing, expired or revoked: a human must re-run the
    /// OAuth flow. Never retried automatically.
    #[error("X needs re-authorisation: {detail}")]
    NeedsReauth { detail: String },

    /// X rate-limited the account. `reset_at` is the epoch second from
    /// `x-rate-limit-reset`; `retry_after_seconds` is from `Retry-After` when
    /// present. Retrying before the reset is what turns a limit into a lock.
    #[error("X rate limit reached{}: {detail}", reset_suffix(.reset_at, .retry_after_seconds))]
    RateLimited {
        reset_at: Option<i64>,
        retry_after_seconds: Option<u64>,
        detail: String,
    },

    /// X refused the post because the same content already exists. Nothing new
    /// was published; reporting success here would be a lie.
    #[error("X rejected the post as a duplicate: {detail}")]
    DuplicatePost { detail: String },

    /// The text is longer than X's limit. Refused with the measured count —
    /// xPST never truncates a post into something the user did not write.
    #[error("text is {measured} characters, over X's {limit}-character limit")]
    TextTooLong { measured: usize, limit: usize },

    /// An empty post has nothing to publish.
    #[error("a text post cannot be empty")]
    TextEmpty,

    /// The caller set a cost cap and this post would exceed it. The link is
    /// never stripped to fit: the post is refused and the caller decides.
    #[error(
        "post would cost {} against a cap of {}",
        micros_usd(.planned_micros),
        micros_usd(.cap_micros)
    )]
    CostCapExceeded {
        planned_micros: u64,
        cap_micros: u64,
    },

    /// This asset was already published to a different X account within the
    /// documented window. X prohibits duplicative posts across accounts you
    /// operate, and violating it risks both accounts.
    #[error(
        "asset {asset_key:?} was already published to account {other_account:?} at {posted_at} \
         (window {window_seconds:?} s)"
    )]
    DuplicateAcrossAccounts {
        asset_key: String,
        other_account: String,
        posted_at: i64,
        window_seconds: Option<u64>,
    },

    /// A media post needs an asset identity so the cross-account duplicate rule
    /// can be enforced. Fails closed rather than publishing unchecked.
    #[error("a media post needs an asset key for cross-account duplicate detection")]
    AssetKeyRequired,

    /// The media file could not be read or sized.
    #[error("media could not be read: {detail}")]
    MediaIo { detail: String },

    /// X does not accept an empty media segment.
    #[error("media is empty; X requires at least one byte")]
    EmptyMedia,

    /// An X response this crate does not have a more specific reading for.
    /// Still carries the status and, when present, X's own error code.
    #[error("X returned HTTP {status}{}: {detail}", code_suffix(.code))]
    Api {
        status: u16,
        code: Option<i64>,
        detail: String,
    },

    /// Media uploaded but X never stated it was processed, so the post was not
    /// attempted. `processing_info` was `pending`, `in_progress`, unreadable or
    /// absent for every bounded status check.
    ///
    /// Posting here would reference media that may not exist yet, so the upload
    /// is reported as unconfirmed instead. Not retryable in the automatic sense:
    /// a caller that wants another bounded wait should ask for it deliberately.
    #[error(
        "media {media_id} was not confirmed ready after {polls} status checks \
         (last state: {state}); refusing to post media that is still processing"
    )]
    MediaProcessingUnconfirmed {
        media_id: String,
        state: String,
        polls: usize,
    },

    /// The request never reached X, or no response came back.
    #[error("transport failure: {detail}")]
    Transport { detail: String },
}

impl XError {
    /// The stable reason code. Tests and callers branch on this, never on the
    /// message text.
    #[must_use]
    pub const fn code(&self) -> &'static str {
        match self {
            Self::NeedsReauth { .. } => "X_NEEDS_REAUTH",
            Self::RateLimited { .. } => "X_RATE_LIMITED",
            Self::DuplicatePost { .. } => "X_DUPLICATE_POST",
            Self::TextTooLong { .. } => "X_TEXT_TOO_LONG",
            Self::TextEmpty => "X_TEXT_EMPTY",
            Self::CostCapExceeded { .. } => "X_COST_CAP_EXCEEDED",
            Self::DuplicateAcrossAccounts { .. } => crate::duplicate::CROSS_ACCOUNT_CODE,
            Self::AssetKeyRequired => "X_ASSET_KEY_REQUIRED",
            Self::MediaIo { .. } => "X_MEDIA_IO",
            Self::EmptyMedia => "X_EMPTY_MEDIA",
            Self::MediaProcessingUnconfirmed { .. } => "X_MEDIA_UNCONFIRMED",
            Self::Api { .. } => "X_API_ERROR",
            Self::Transport { .. } => "X_TRANSPORT_ERROR",
        }
    }

    /// Whether sending the same request again could succeed without a human or
    /// a policy change. Rate limits are the only automatic retry: auth needs a
    /// person, a duplicate never becomes unique, and a cost cap is a decision.
    #[must_use]
    pub const fn retryable(&self) -> bool {
        matches!(self, Self::RateLimited { .. } | Self::Transport { .. })
    }

    /// Whether a human must complete authentication before any retry.
    #[must_use]
    pub const fn needs_reauth(&self) -> bool {
        matches!(self, Self::NeedsReauth { .. })
    }

    /// The epoch second the rate limit lifts, when X told us.
    #[must_use]
    pub const fn rate_limit_reset(&self) -> Option<i64> {
        match self {
            Self::RateLimited { reset_at, .. } => *reset_at,
            _ => None,
        }
    }

    /// Whether this error is a policy refusal made before any network call —
    /// no money was spent and nothing was sent.
    #[must_use]
    pub const fn is_preflight_refusal(&self) -> bool {
        matches!(
            self,
            Self::TextTooLong { .. }
                | Self::TextEmpty
                | Self::CostCapExceeded { .. }
                | Self::DuplicateAcrossAccounts { .. }
                | Self::AssetKeyRequired
                | Self::EmptyMedia
        )
    }
}

fn reset_suffix(reset_at: &Option<i64>, retry_after: &Option<u64>) -> String {
    match (*reset_at, *retry_after) {
        (Some(at), _) => format!(" (resets at {at})"),
        (None, Some(seconds)) => format!(" (retry after {seconds}s)"),
        (None, None) => String::new(),
    }
}

fn code_suffix(code: &Option<i64>) -> String {
    code.map_or_else(String::new, |code| format!(" (X code {code})"))
}

/// Translate a non-2xx response into the most specific honest error.
///
/// 401 is auth; 429 is a rate limit *with its reset time*; 403 is read for X's
/// own error code, because "status is a duplicate" (187) and "invalid or expired
/// token" (89) arrive as 403 and mean completely different things.
#[must_use]
pub fn from_response(response: &Response) -> XError {
    let detail = truncate(&response.body_text(), 500);
    match response.status {
        401 => XError::NeedsReauth { detail },
        429 => XError::RateLimited {
            reset_at: response
                .header("x-rate-limit-reset")
                .and_then(|value| value.trim().parse::<i64>().ok()),
            retry_after_seconds: response
                .header("retry-after")
                .and_then(|value| value.trim().parse::<u64>().ok()),
            detail,
        },
        status => {
            let code = first_error_code(&response.body);
            match (status, code) {
                (403, Some(187)) => XError::DuplicatePost { detail },
                (403, Some(89)) => XError::NeedsReauth { detail },
                _ if status == 403 && contains_duplicate_wording(&detail) => {
                    XError::DuplicatePost { detail }
                }
                _ => XError::Api {
                    status,
                    code,
                    detail,
                },
            }
        }
    }
}

fn first_error_code(body: &[u8]) -> Option<i64> {
    let value: Value = serde_json::from_slice(body).ok()?;
    value
        .get("errors")
        .and_then(Value::as_array)
        .and_then(|errors| errors.first())
        .and_then(|error| error.get("code"))
        .and_then(Value::as_i64)
        .or_else(|| value.get("code").and_then(Value::as_i64))
}

fn contains_duplicate_wording(detail: &str) -> bool {
    detail.to_ascii_lowercase().contains("duplicate")
}

fn micros_usd(micros: &u64) -> String {
    crate::cost::format_micros(*micros)
}

fn truncate(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        text.to_string()
    } else {
        text.chars().take(limit).collect::<String>() + "…"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unauthorized_is_a_needs_reauth_answer() {
        let error = from_response(&Response::new(401, "Unauthorized"));
        assert_eq!(error.code(), "X_NEEDS_REAUTH");
        assert!(error.needs_reauth());
        assert!(!error.retryable());
    }

    #[test]
    fn duplicate_status_187_is_not_a_generic_api_error() {
        let body =
            r#"{"detail":"Forbidden","errors":[{"code":187,"message":"Status is a duplicate."}]}"#;
        let error = from_response(&Response::new(403, body));
        assert_eq!(error.code(), "X_DUPLICATE_POST");
        assert!(!error.retryable());
    }

    #[test]
    fn rate_limit_keeps_the_reset_time() {
        let response =
            Response::new(429, "Too Many Requests").with_header("x-rate-limit-reset", "1780000000");
        let error = from_response(&response);
        assert_eq!(error.code(), "X_RATE_LIMITED");
        assert_eq!(error.rate_limit_reset(), Some(1_780_000_000));
        assert!(error.retryable());
    }

    #[test]
    fn unknown_status_is_reported_with_the_number() {
        let error = from_response(&Response::new(500, "internal"));
        assert_eq!(error.code(), "X_API_ERROR");
        assert!(matches!(error, XError::Api { status: 500, .. }));
    }
}
