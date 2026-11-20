//! Token lifecycle, behind a trait and through the approved OAuth crate.
//!
//! `yup-oauth2` (MIT OR Apache-2.0, approved in
//! `docs/technical/licence-policy.md`) owns refreshing and caching the access
//! token: its `Authenticator::token` returns the cached token while it is
//! valid and performs the refresh grant itself once it is expired. This module
//! does not reimplement any of that. It wraps the authenticator in a
//! [`TokenSource`] so the upload driver can be exercised without a network,
//! and it translates the crate's error type into three outcomes the caller can
//! act on:
//!
//! * [`TokenError::NeedsReauth`] — the refresh grant was refused with
//!   `invalid_grant` (or the token was revoked/expired server-side). No retry
//!   can fix it; the user must run `xpst auth youtube` again.
//! * [`TokenError::Transient`] — the OAuth endpoint failed in a way that may
//!   work next time (connection error, 5xx, malformed response).
//! * [`TokenError::Fatal`] — the client itself is misconfigured
//!   (`invalid_client`, `invalid_scope`). Retrying only burns quota.
//!
//! The distinction matters because "auth failed" and "quota spent" must not
//! look alike to the planner: one parks the post for a human, the other waits.

use std::fmt;
use std::future::Future;

use yup_oauth2::error::{AuthErrorCode, Error as OAuthError};

/// An access token, with the secret kept out of `Debug`.
///
/// `#[derive(Debug)]` on a struct holding a bearer token is how credentials end
/// up in a log line; this type prints `<redacted>` and nothing else about the
/// value.
#[derive(Clone, PartialEq, Eq)]
pub struct AccessToken {
    value: String,
    /// Seconds since the Unix epoch, when the issuer said.
    expires_at_unix: Option<i64>,
}

impl AccessToken {
    #[must_use]
    pub fn new(value: impl Into<String>, expires_at_unix: Option<i64>) -> Self {
        Self {
            value: value.into(),
            expires_at_unix,
        }
    }

    /// The secret. Named so that every use is a visible decision.
    #[must_use]
    pub fn expose_secret(&self) -> &str {
        &self.value
    }

    /// Expiry as the issuer reported it, in Unix seconds.
    #[must_use]
    pub const fn expires_at_unix(&self) -> Option<i64> {
        self.expires_at_unix
    }

    /// Whether the token has expired at `now_unix`.
    ///
    /// The authenticator already refuses to hand out an expired token; this
    /// exists so a caller can decide whether to mint one eagerly.
    #[must_use]
    pub fn is_expired_at(&self, now_unix: i64) -> bool {
        self.expires_at_unix
            .is_some_and(|expiry| expiry <= now_unix)
    }

    /// The `Authorization` header value.
    #[must_use]
    pub fn authorization_value(&self) -> String {
        format!("Bearer {}", self.value)
    }
}

impl fmt::Debug for AccessToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("AccessToken")
            .field("value", &"<redacted>")
            .field("expires_at_unix", &self.expires_at_unix)
            .finish()
    }
}

/// What went wrong while obtaining a token, in terms the caller can act on.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TokenError {
    /// The grant is gone: `invalid_grant`, a revoked token, or an expired
    /// refresh token. Only a human re-authorisation fixes this.
    #[error("needs re-authentication: {detail}")]
    NeedsReauth { detail: String },
    /// A failure that may not repeat (connection error, 5xx, bad response).
    #[error("token endpoint unavailable: {detail}")]
    Transient { detail: String },
    /// The OAuth client is misconfigured. Retrying cannot help.
    #[error("OAuth client rejected: {detail}")]
    Fatal { detail: String },
}

impl TokenError {
    #[must_use]
    pub fn needs_reauth(&self) -> bool {
        matches!(self, Self::NeedsReauth { .. })
    }

    #[must_use]
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Transient { .. })
    }
}

/// Where the upload driver gets its bearer token.
///
/// Asynchronous for the same reason the transport is: the refresh grant is a
/// network call, and it must not block the executor.
pub trait TokenSource {
    /// A currently valid access token, refreshing if necessary.
    fn access_token(&self) -> impl Future<Output = Result<AccessToken, TokenError>> + Send;
}

/// Translate the approved crate's error into the three outcomes above.
///
/// The `invalid_grant` match is on the typed `AuthErrorCode` first — string
/// matching is a fallback for the cases where the code arrives nested inside a
/// message (a proxy rewrites the body, say), because missing it would send a
/// revoked user into a retry loop against a grant that cannot come back.
#[must_use]
pub fn classify_oauth_error(error: &OAuthError) -> TokenError {
    if let OAuthError::AuthError(auth) = error {
        return match auth.error {
            AuthErrorCode::InvalidGrant => TokenError::NeedsReauth {
                detail: format!("the refresh token is no longer valid ({auth})"),
            },
            AuthErrorCode::ExpiredToken => TokenError::NeedsReauth {
                detail: format!("the token expired server-side ({auth})"),
            },
            AuthErrorCode::AccessDenied => TokenError::NeedsReauth {
                detail: format!("access was denied ({auth})"),
            },
            AuthErrorCode::InvalidClient | AuthErrorCode::UnauthorizedClient => TokenError::Fatal {
                detail: format!("the OAuth client was rejected ({auth})"),
            },
            AuthErrorCode::InvalidScope => TokenError::Fatal {
                detail: format!("a requested scope is not allowed for this client ({auth})"),
            },
            AuthErrorCode::InvalidRequest
            | AuthErrorCode::UnsupportedGrantType
            | AuthErrorCode::Other(_) => TokenError::Transient {
                detail: format!("the token endpoint refused the request ({auth})"),
            },
        };
    }

    let text = error.to_string();
    let lowered = text.to_ascii_lowercase();
    if lowered.contains("invalid_grant") || lowered.contains("token has been expired or revoked") {
        return TokenError::NeedsReauth { detail: text };
    }
    if lowered.contains("invalid_client") || lowered.contains("invalid_scope") {
        return TokenError::Fatal { detail: text };
    }
    TokenError::Transient { detail: text }
}

/// A [`TokenSource`] backed by `yup-oauth2`'s authenticator.
///
/// The authenticator is built by the caller (the auth crate owns the consent
/// flow and the token storage); this adapter only asks it for a token, so the
/// upload path never touches a client secret.
pub struct GoogleTokenSource<C>
where
    C: hyper_util::client::legacy::connect::Connect + Clone + Send + Sync + 'static,
{
    authenticator: yup_oauth2::authenticator::Authenticator<C>,
    scopes: Vec<&'static str>,
}

impl<C> GoogleTokenSource<C>
where
    C: hyper_util::client::legacy::connect::Connect + Clone + Send + Sync + 'static,
{
    #[must_use]
    pub fn new(
        authenticator: yup_oauth2::authenticator::Authenticator<C>,
        scopes: &[&'static str],
    ) -> Self {
        Self {
            authenticator,
            scopes: scopes.to_vec(),
        }
    }

    /// The scopes requests are minted for.
    #[must_use]
    pub fn scopes(&self) -> &[&'static str] {
        &self.scopes
    }
}

impl<C> GoogleTokenSource<C>
where
    C: hyper_util::client::legacy::connect::Connect + Clone + Send + Sync + 'static,
{
    async fn fetch(&self) -> Result<AccessToken, TokenError> {
        // `token()` returns the cached token while it is valid and performs the
        // refresh grant when it is not — the refresh is the crate's job, not
        // ours, which is the whole point of the licence decision.
        let token = self
            .authenticator
            .token(&self.scopes)
            .await
            .map_err(|error| classify_oauth_error(&error))?;
        let Some(value) = token.token() else {
            return Err(TokenError::NeedsReauth {
                detail: "the token endpoint returned no access token".to_owned(),
            });
        };
        Ok(AccessToken::new(
            value,
            token
                .expiration_time()
                .map(|expiry| expiry.unix_timestamp()),
        ))
    }
}

impl<C> TokenSource for GoogleTokenSource<C>
where
    C: hyper_util::client::legacy::connect::Connect + Clone + Send + Sync + 'static,
{
    fn access_token(&self) -> impl Future<Output = Result<AccessToken, TokenError>> + Send {
        self.fetch()
    }
}
