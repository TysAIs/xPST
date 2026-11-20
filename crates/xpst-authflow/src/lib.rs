//! Credential acquisition: the flows that obtain a credential in the first
//! place, and the honest storage decision that follows.
//!
//! ## The three shapes, behind one trait
//!
//! * [`LoopbackRedirectFlow`] — RFC 8252 loopback. **YouTube only.** Google's
//!   installed-app client redirects to `http://127.0.0.1:<port>`; the listener
//!   in [`LoopbackListener`] captures it. X and Meta reject this shape for the
//!   apps this project can register (see the matrix in [`platform`]).
//! * [`HeadlessPasteFlow`] — print the authorize URL, then read back the URL the
//!   browser landed on. **X**, as the official `xurl` client does. **Meta**
//!   can use it only with an HTTPS redirect URI the caller hosts; loopback is
//!   not registrable with Meta.
//! * [`AppPasswordFlow`] — no browser at all. **Bluesky** app password and
//!   **Mastodon** personal access token. Neither needs a developer app.
//!
//! Each platform's support for each shape is data
//! ([`AuthPlatform::flow_support`]), including the reason for a `No`, so no UI
//! has to guess.
//!
//! ## Honest storage
//!
//! [`storage::CredentialStore`] probes the OS keychain first. Where no Secret
//! Service exists (Linux headless is the real case), the default policy writes
//! Argon2id + XChaCha20-Poly1305 encrypted files with an actionable alternative:
//! [`storage::StoragePolicy::KeychainOnly`] refuses outright instead. No runtime
//! path unwraps.
//!
//! ## What is real and what is scaffolded
//!
//! Real: PKCE S256 (checked against RFC 7636 Appendix B), CSRF state
//! generation/verification, authorize-URL assembly, redirect URI/state
//! verification, redirect parsing, loopback HTTP capture, static-secret
//! capture, encrypted storage, the ban-risk registry and its stored
//! acknowledgements.
//!
//! Scaffolded: the HTTP POST to the provider's token endpoint and token
//! refresh. [`AuthorizationCode::token_request_params`] prepares every field
//! that request needs; making it is left to the provider crates because it
//! requires network and a real developer app.
//!
//! No UI and no CLI live here. The caller sequence is:
//!
//! ```text
//! listener = LoopbackListener::bind(0, "/callback", "127.0.0.1")
//! flow     = LoopbackRedirectFlow::new(config with listener.redirect_uri())
//! Begin::Redirect(request) = flow.begin()
//! open(request.url)
//! redirect = listener.accept_redirect(300s)
//! FlowOutcome::AuthorizationCode(code) = flow.complete(redirect)
//! store.put("youtube", credential_from_token_exchange(code))
//! ```

#![forbid(unsafe_code)]

mod credential;
mod encoding;
mod error;
mod flow;
mod loopback;
mod oauth;
mod pkce;
mod platform;
pub mod risk;
mod secret;
mod secure_file;
pub mod storage;

pub use credential::Credential;
pub use error::AuthFlowError;
pub use flow::{
    default_authorize_endpoint, default_flow, AppPasswordFlow, AuthorizationCode, AuthorizeRequest,
    Begin, CallbackInput, CredentialFlow, FlowConfig, FlowOutcome, HeadlessPasteFlow,
    LoopbackRedirectFlow, SecretPrompt, GOOGLE_AUTHORIZE_ENDPOINT, META_AUTHORIZE_ENDPOINT,
    THREADS_AUTHORIZE_ENDPOINT, X_AUTHORIZE_ENDPOINT,
};
pub use loopback::LoopbackListener;
pub use pkce::{s256_challenge, Pkce, StateToken, MAX_VERIFIER_LEN, MIN_VERIFIER_LEN};
pub use platform::{AuthMode, AuthPlatform, FlowKind, FlowSupport, Support};
pub use secret::SecretString;
