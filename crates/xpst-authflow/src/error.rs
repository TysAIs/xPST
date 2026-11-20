//! Flow errors. Every variant is a value a caller can match on; none of them
//! contains an authorization code, a verifier or a token.

use crate::platform::FlowKind;

/// Why a credential-acquisition flow could not produce a credential.
#[derive(Debug, thiserror::Error)]
pub enum AuthFlowError {
    /// The verifier is outside RFC 7636's 43..=128 character window.
    #[error("PKCE verifier is {actual} characters long; RFC 7636 requires 43..=128")]
    PkceVerifierLength { actual: usize },

    /// The verifier contains something outside RFC 7636's unreserved set.
    #[error("PKCE verifier contains {0:?}, which is outside RFC 7636's unreserved character set")]
    PkceVerifierCharset(char),

    /// The redirect's `state` did not match the value this flow generated.
    ///
    /// This is the CSRF check, and it fails closed: the code is never returned
    /// to the caller, let alone exchanged.
    #[error(
        "the redirect did not carry the state value this flow generated; refusing the code (possible CSRF)"
    )]
    StateMismatch,

    /// The redirect carried neither an error nor a code.
    #[error("the redirect carried no authorization code")]
    MissingCode,

    /// The provider itself refused the authorization request.
    #[error("the provider refused authorization: {code}: {description}")]
    ProviderError {
        /// Stable provider error code, e.g. `access_denied`.
        code: String,
        /// Provider-supplied description. Never contains our secrets.
        description: String,
    },

    /// The browser was sent somewhere other than the registered redirect URI.
    #[error("redirect {got} does not match the registered redirect URI {expected}")]
    RedirectUriMismatch {
        /// The redirect URI this flow registered.
        expected: String,
        /// The target the pasted/captured URL actually points at.
        got: String,
    },

    /// This platform cannot use this flow shape at all.
    #[error("{platform} cannot use the {requested:?} flow: {reason}")]
    UnsupportedFlow {
        /// Wire name of the platform.
        platform: &'static str,
        /// The requested shape.
        requested: FlowKind,
        /// Why it cannot be used. Carried as data so the UI cannot invent a
        /// different explanation.
        reason: &'static str,
    },

    /// `begin` was called twice on one flow.
    #[error("the flow has already been started; construct a new flow for a new authorization")]
    AlreadyStarted,

    /// The flow already produced its credential/code.
    #[error("the flow has already been completed")]
    AlreadyCompleted,

    /// `complete` was called before `begin`.
    #[error("the flow has not been started; call begin() first")]
    NotStarted,

    /// A redirect-shaped flow was handed a secret instead of a URL.
    #[error("a {flow:?} flow needs the redirect URL, not a pasted secret")]
    WrongInput { flow: FlowKind },

    /// A static-secret flow was handed a redirect URL instead of a secret.
    #[error("a static-secret flow needs the secret, not a redirect URL")]
    WrongInputSecret,

    /// The configuration cannot produce a valid request.
    #[error("invalid flow configuration: {0}")]
    InvalidConfig(String),

    /// The human supplied something unusable (empty secret, missing ident).
    #[error("{0}")]
    InvalidInput(String),

    /// The OS random source failed. Never silently substituted.
    #[error("operating-system randomness is unavailable: {0}")]
    Random(String),

    /// Binding/reading the loopback listener failed.
    #[error("loopback listener failed: {0}")]
    Listener(String),

    /// Nobody completed the consent page before the deadline.
    #[error("the authorization redirect timed out after {seconds} s; no code was captured")]
    Timeout { seconds: u64 },
}
