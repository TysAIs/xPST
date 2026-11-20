//! The three flow shapes behind one trait.
//!
//! `CredentialFlow` is intentionally small: `begin` says what the human must do
//! (open a URL, or paste a secret), and `complete` consumes what came back and
//! applies the security checks. Everything a UI needs to render before consent
//! is data ([`Begin`]), so no surface has to re-derive permissions from prose.
//!
//! What is real here: PKCE + state generation, authorize-URL assembly, redirect
//! URI/state verification, redirect parsing, loopback capture, static-secret
//! capture, and preparation of the token-endpoint form.
//!
//! What is deliberately not here: the HTTP call to the provider's token
//! endpoint and the refresh loop. Both need network and a real developer app,
//! which this phase excludes. [`AuthorizationCode::token_request_params`]
//! returns every field that call needs, so the missing step is one POST, not a
//! second implementation of OAuth.

use crate::credential::Credential;
use crate::error::AuthFlowError;
use crate::oauth::{build_authorize_url, is_loopback_uri, parse_redirect};
use crate::pkce::{Pkce, StateToken};
use crate::platform::{AuthMode, AuthPlatform, FlowKind, Support};
use crate::secret::SecretString;

/// Google's installed-app authorize endpoint.
pub const GOOGLE_AUTHORIZE_ENDPOINT: &str = "https://accounts.google.com/o/oauth2/v2/auth";
/// X's OAuth 2.0 authorize endpoint.
pub const X_AUTHORIZE_ENDPOINT: &str = "https://x.com/i/oauth2/authorize";
/// Meta's Facebook-Login dialog, used by Instagram/Facebook Graph flows.
pub const META_AUTHORIZE_ENDPOINT: &str = "https://www.facebook.com/dialog/oauth";
/// Threads' own authorize endpoint.
pub const THREADS_AUTHORIZE_ENDPOINT: &str = "https://threads.net/oauth/authorize";

/// Everything needed to build one authorize request.
#[derive(Debug, Clone)]
pub struct FlowConfig {
    /// Which platform this authorization is for.
    pub platform: AuthPlatform,
    /// The app's client id. Fictional values are fine until a real app exists.
    pub client_id: String,
    /// Loopback URI (loopback flow) or registered HTTPS URI (paste flow).
    pub redirect_uri: String,
    /// The auth mode this flow is allowed to record. Validated against the
    /// platform's official mode by [`FlowConfig::validate_for`].
    pub auth_mode: AuthMode,
    /// Requested scopes.
    pub scopes: Vec<String>,
    /// Provider authorize endpoint. Defaults per platform; override for tests
    /// or a pinned API version.
    pub authorize_endpoint: String,
    /// Provider-specific parameters (`access_type=offline`, `prompt=consent`).
    pub extra_params: Vec<(String, String)>,
}

impl FlowConfig {
    /// Build a config with the platform's default authorize endpoint.
    #[must_use]
    pub fn new(
        platform: AuthPlatform,
        client_id: impl Into<String>,
        redirect_uri: impl Into<String>,
        auth_mode: AuthMode,
    ) -> Self {
        Self {
            platform,
            client_id: client_id.into(),
            redirect_uri: redirect_uri.into(),
            auth_mode,
            scopes: Vec::new(),
            authorize_endpoint: default_authorize_endpoint(platform)
                .unwrap_or("")
                .to_string(),
            extra_params: Vec::new(),
        }
    }

    /// Replace the requested scopes.
    #[must_use]
    pub fn with_scopes<I, S>(mut self, scopes: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.scopes = scopes.into_iter().map(Into::into).collect();
        self
    }

    /// Replace the authorize endpoint.
    #[must_use]
    pub fn with_authorize_endpoint(mut self, endpoint: impl Into<String>) -> Self {
        self.authorize_endpoint = endpoint.into();
        self
    }

    /// Add one provider-specific parameter.
    #[must_use]
    pub fn with_param(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.extra_params.push((key.into(), value.into()));
        self
    }

    /// Reject a platform/shape/mode combination before a URL is ever built.
    fn validate_for(&self, kind: FlowKind) -> Result<(), AuthFlowError> {
        let support = self.platform.flow_support().get(kind);
        if let Support::No(reason) = support {
            return Err(AuthFlowError::UnsupportedFlow {
                platform: self.platform.as_str(),
                requested: kind,
                reason,
            });
        }
        match self.platform.required_auth_mode(kind) {
            Some(required) if required == self.auth_mode => {}
            Some(required) => {
                return Err(AuthFlowError::InvalidConfig(format!(
                    "{} requires auth_mode '{}' for the {} flow, not '{}'; an official flow \
                     must not record an unofficial mode",
                    self.platform,
                    required.as_str(),
                    kind.as_str(),
                    self.auth_mode.as_str()
                )));
            }
            None => {
                return Err(AuthFlowError::UnsupportedFlow {
                    platform: self.platform.as_str(),
                    requested: kind,
                    reason: "no credential flow is registered for this platform and shape",
                });
            }
        }
        if !matches!(kind, FlowKind::StaticSecret) {
            if self.client_id.trim().is_empty() {
                return Err(AuthFlowError::InvalidConfig(
                    "client_id is required for a redirect flow".to_string(),
                ));
            }
            if self.authorize_endpoint.trim().is_empty() {
                return Err(AuthFlowError::InvalidConfig(
                    "authorize_endpoint is required for a redirect flow".to_string(),
                ));
            }
            if self.redirect_uri.trim().is_empty() {
                return Err(AuthFlowError::InvalidConfig(
                    "redirect_uri is required for a redirect flow".to_string(),
                ));
            }
        }
        Ok(())
    }
}

/// Which flow shape a platform uses by default. Data, so a caller can render
/// the choice without hard-coding it.
#[must_use]
pub fn default_flow(platform: AuthPlatform) -> FlowKind {
    match platform {
        AuthPlatform::YouTube => FlowKind::LoopbackRedirect,
        AuthPlatform::X
        | AuthPlatform::Instagram
        | AuthPlatform::Threads
        | AuthPlatform::Facebook => FlowKind::HeadlessPaste,
        AuthPlatform::Bluesky | AuthPlatform::Mastodon => FlowKind::StaticSecret,
    }
}

/// The authorize endpoint this crate defaults to, when the platform has one.
#[must_use]
pub fn default_authorize_endpoint(platform: AuthPlatform) -> Option<&'static str> {
    match platform {
        AuthPlatform::YouTube => Some(GOOGLE_AUTHORIZE_ENDPOINT),
        AuthPlatform::X => Some(X_AUTHORIZE_ENDPOINT),
        AuthPlatform::Instagram | AuthPlatform::Facebook => Some(META_AUTHORIZE_ENDPOINT),
        AuthPlatform::Threads => Some(THREADS_AUTHORIZE_ENDPOINT),
        AuthPlatform::Bluesky | AuthPlatform::Mastodon => None,
    }
}

/// What the human must do next.
#[derive(Debug, Clone)]
pub enum Begin {
    /// Open this URL, then let the transport bring the redirect back.
    Redirect(AuthorizeRequest),
    /// No browser: ask for a secret.
    CollectSecret(SecretPrompt),
}

/// The URL to show the human, with the values that produced it.
#[derive(Clone)]
pub struct AuthorizeRequest {
    /// Platform being authorized.
    pub platform: AuthPlatform,
    /// Which shape produced the URL.
    pub flow: FlowKind,
    /// The absolute authorize URL.
    pub url: String,
    /// The redirect URI embedded in the URL.
    pub redirect_uri: String,
    /// The CSRF state embedded in the URL.
    pub state: String,
    /// Plain-language instruction that matches this shape.
    pub instructions: &'static str,
}

impl AuthorizeRequest {
    /// Read back one query parameter of [`Self::url`], decoded.
    #[must_use]
    pub fn param(&self, name: &str) -> Option<String> {
        crate::oauth::query_pairs(&self.url)
            .ok()?
            .into_iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value)
    }
}

impl std::fmt::Debug for AuthorizeRequest {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // The URL carries the state; keep it out of logs, but the fields that
        // matter for debugging stay visible.
        formatter
            .debug_struct("AuthorizeRequest")
            .field("platform", &self.platform)
            .field("flow", &self.flow)
            .field("redirect_uri", &self.redirect_uri)
            .field("state", &"<redacted>")
            .finish_non_exhaustive()
    }
}

/// What a static-secret flow must ask the human for.
#[derive(Debug, Clone)]
pub struct SecretPrompt {
    /// Platform being connected.
    pub platform: AuthPlatform,
    /// Mode that will be recorded.
    pub auth_mode: AuthMode,
    /// Dialog title.
    pub title: &'static str,
    /// Label for the non-secret identifier (handle, instance URL).
    pub identifier_label: Option<&'static str>,
    /// Whether the identifier is required.
    pub identifier_required: bool,
    /// Label for the secret field.
    pub secret_label: &'static str,
    /// Where the human creates the secret.
    pub help_url: &'static str,
    /// Why this shape does not involve a browser redirect.
    pub help: &'static str,
}

/// What came back from the human.
#[derive(Debug, Clone)]
pub enum CallbackInput<'a> {
    /// The full URL the browser was redirected to.
    RedirectUrl(&'a str),
    /// An app password / personal access token, with an optional identifier.
    Secret {
        /// Non-secret identifier, when the platform needs one.
        identifier: Option<&'a str>,
        /// The secret itself.
        secret: &'a str,
    },
}

/// A verified authorization code plus what the token endpoint needs.
#[derive(Debug, Clone)]
pub struct AuthorizationCode {
    /// Platform the code belongs to.
    pub platform: AuthPlatform,
    /// The redirect URI that was registered for this exchange.
    pub redirect_uri: String,
    /// The code. Send only to the provider's token endpoint.
    pub code: SecretString,
    /// The PKCE verifier that matches the challenge sent on the URL.
    pub code_verifier: SecretString,
}

impl AuthorizationCode {
    /// The `application/x-www-form-urlencoded` fields for the token request.
    ///
    /// This crate does not perform the POST; it prepares exactly the fields
    /// RFC 6749 §4.1.3 and RFC 7636 §4.5 require.
    #[must_use]
    pub fn token_request_params(
        &self,
        client_id: &str,
        client_secret: Option<&str>,
    ) -> Vec<(String, String)> {
        let mut params = vec![
            ("grant_type".to_string(), "authorization_code".to_string()),
            ("code".to_string(), self.code.expose().to_string()),
            ("redirect_uri".to_string(), self.redirect_uri.clone()),
            (
                "code_verifier".to_string(),
                self.code_verifier.expose().to_string(),
            ),
            ("client_id".to_string(), client_id.to_string()),
        ];
        if let Some(secret) = client_secret {
            params.push(("client_secret".to_string(), secret.to_string()));
        }
        params
    }
}

/// The result of a completed flow.
#[derive(Debug, Clone)]
pub enum FlowOutcome {
    /// Redirect flows stop here: exchange the code over the network.
    AuthorizationCode(AuthorizationCode),
    /// Static flows are complete: the pasted secret is the credential.
    StaticCredential(Credential),
}

/// One credential-acquisition attempt.
///
/// Implementations are single-use: `begin` once, `complete` once. A retry with
/// corrected input before success is allowed; a second success is not.
pub trait CredentialFlow {
    /// Platform being authorized.
    fn platform(&self) -> AuthPlatform;
    /// Shape of this flow.
    fn kind(&self) -> FlowKind;
    /// What the human must do. Generates a fresh state only on the first call.
    fn begin(&mut self) -> Result<Begin, AuthFlowError>;
    /// Consume the redirect or secret and apply the security checks.
    fn complete(&mut self, input: CallbackInput<'_>) -> Result<FlowOutcome, AuthFlowError>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    Ready,
    Started,
    Completed,
}

fn mark_started(phase: &mut Phase) -> Result<(), AuthFlowError> {
    match phase {
        Phase::Ready => {
            *phase = Phase::Started;
            Ok(())
        }
        Phase::Started => Err(AuthFlowError::AlreadyStarted),
        Phase::Completed => Err(AuthFlowError::AlreadyCompleted),
    }
}

fn require_started(phase: Phase) -> Result<(), AuthFlowError> {
    match phase {
        Phase::Ready => Err(AuthFlowError::NotStarted),
        Phase::Started => Ok(()),
        Phase::Completed => Err(AuthFlowError::AlreadyCompleted),
    }
}

const LOOPBACK_INSTRUCTIONS: &str = "Open this URL in a browser. xPST is listening on the \
loopback address and captures the redirect automatically. If the browser cannot reach the \
listener, copy the full address from the address bar back into xPST.";

const PASTE_INSTRUCTIONS: &str = "Open this URL, approve access, then copy the entire address \
the browser was sent to and paste it back. The page itself may fail to load - the URL is what \
matters.";

/// RFC 8252 loopback flow. Supported for YouTube desktop clients; X and Meta
/// cannot use it (see [`AuthPlatform::flow_support`]).
pub struct LoopbackRedirectFlow {
    config: FlowConfig,
    pkce: Pkce,
    state: StateToken,
    phase: Phase,
}

impl LoopbackRedirectFlow {
    /// Prepare a loopback flow. Generates PKCE and state immediately.
    pub fn new(config: FlowConfig) -> Result<Self, AuthFlowError> {
        config.validate_for(FlowKind::LoopbackRedirect)?;
        if !is_loopback_uri(&config.redirect_uri) {
            return Err(AuthFlowError::InvalidConfig(format!(
                "redirect_uri {:?} is not a loopback URI (127.0.0.1, localhost or [::1])",
                config.redirect_uri
            )));
        }
        Ok(Self {
            config,
            pkce: Pkce::generate()?,
            state: StateToken::generate()?,
            phase: Phase::Ready,
        })
    }

    /// The redirect URI this flow will accept.
    #[must_use]
    pub fn redirect_uri(&self) -> &str {
        &self.config.redirect_uri
    }

    fn build_request(&self) -> Result<AuthorizeRequest, AuthFlowError> {
        build_redirect_request(
            &self.config,
            FlowKind::LoopbackRedirect,
            &self.pkce,
            &self.state,
            LOOPBACK_INSTRUCTIONS,
        )
    }
}

impl CredentialFlow for LoopbackRedirectFlow {
    fn platform(&self) -> AuthPlatform {
        self.config.platform
    }

    fn kind(&self) -> FlowKind {
        FlowKind::LoopbackRedirect
    }

    fn begin(&mut self) -> Result<Begin, AuthFlowError> {
        mark_started(&mut self.phase)?;
        Ok(Begin::Redirect(self.build_request()?))
    }

    fn complete(&mut self, input: CallbackInput<'_>) -> Result<FlowOutcome, AuthFlowError> {
        require_started(self.phase)?;
        let CallbackInput::RedirectUrl(url) = input else {
            return Err(AuthFlowError::WrongInput { flow: self.kind() });
        };
        let verified = parse_redirect(url, &self.config.redirect_uri, &self.state)?;
        self.phase = Phase::Completed;
        Ok(FlowOutcome::AuthorizationCode(AuthorizationCode {
            platform: self.config.platform,
            redirect_uri: self.config.redirect_uri.clone(),
            code: SecretString::new(verified.code),
            code_verifier: self.pkce.verifier().clone(),
        }))
    }
}

/// Headless paste-the-redirect flow, as the official `xurl` client does for X.
///
/// X cannot use loopback (registered callback). Meta can use this shape only
/// with an HTTPS redirect URI the caller hosts and registers; the constructor
/// enforces that.
pub struct HeadlessPasteFlow {
    config: FlowConfig,
    pkce: Pkce,
    state: StateToken,
    phase: Phase,
}

impl HeadlessPasteFlow {
    /// Prepare a paste flow. Generates PKCE and state immediately.
    pub fn new(config: FlowConfig) -> Result<Self, AuthFlowError> {
        config.validate_for(FlowKind::HeadlessPaste)?;
        if !(config.redirect_uri.starts_with("https://")
            || config.redirect_uri.starts_with("http://"))
        {
            return Err(AuthFlowError::InvalidConfig(format!(
                "redirect_uri {:?} is not an absolute http(s) URL",
                config.redirect_uri
            )));
        }
        if matches!(
            config.platform,
            AuthPlatform::Instagram | AuthPlatform::Threads | AuthPlatform::Facebook
        ) && !config.redirect_uri.starts_with("https://")
        {
            return Err(AuthFlowError::InvalidConfig(
                "Meta requires an HTTPS redirect URI registered on the app; loopback and plain \
                 http are rejected"
                    .to_string(),
            ));
        }
        Ok(Self {
            config,
            pkce: Pkce::generate()?,
            state: StateToken::generate()?,
            phase: Phase::Ready,
        })
    }

    fn build_request(&self) -> Result<AuthorizeRequest, AuthFlowError> {
        build_redirect_request(
            &self.config,
            FlowKind::HeadlessPaste,
            &self.pkce,
            &self.state,
            PASTE_INSTRUCTIONS,
        )
    }
}

impl CredentialFlow for HeadlessPasteFlow {
    fn platform(&self) -> AuthPlatform {
        self.config.platform
    }

    fn kind(&self) -> FlowKind {
        FlowKind::HeadlessPaste
    }

    fn begin(&mut self) -> Result<Begin, AuthFlowError> {
        mark_started(&mut self.phase)?;
        Ok(Begin::Redirect(self.build_request()?))
    }

    fn complete(&mut self, input: CallbackInput<'_>) -> Result<FlowOutcome, AuthFlowError> {
        require_started(self.phase)?;
        let CallbackInput::RedirectUrl(url) = input else {
            return Err(AuthFlowError::WrongInput { flow: self.kind() });
        };
        let verified = parse_redirect(url, &self.config.redirect_uri, &self.state)?;
        self.phase = Phase::Completed;
        Ok(FlowOutcome::AuthorizationCode(AuthorizationCode {
            platform: self.config.platform,
            redirect_uri: self.config.redirect_uri.clone(),
            code: SecretString::new(verified.code),
            code_verifier: self.pkce.verifier().clone(),
        }))
    }
}

/// App-password / personal-token flow: no browser, no redirect, no developer
/// app. Bluesky and Mastodon only; neither platform has an OAuth redirect in
/// this build.
pub struct AppPasswordFlow {
    config: FlowConfig,
    prompt: SecretPrompt,
    phase: Phase,
}

impl AppPasswordFlow {
    /// Prepare a static-secret flow.
    pub fn new(config: FlowConfig) -> Result<Self, AuthFlowError> {
        config.validate_for(FlowKind::StaticSecret)?;
        let prompt =
            secret_prompt(config.platform).ok_or_else(|| AuthFlowError::UnsupportedFlow {
                platform: config.platform.as_str(),
                requested: FlowKind::StaticSecret,
                reason: "no static-secret credential exists for this platform",
            })?;
        Ok(Self {
            config,
            prompt,
            phase: Phase::Ready,
        })
    }
}

impl CredentialFlow for AppPasswordFlow {
    fn platform(&self) -> AuthPlatform {
        self.config.platform
    }

    fn kind(&self) -> FlowKind {
        FlowKind::StaticSecret
    }

    fn begin(&mut self) -> Result<Begin, AuthFlowError> {
        mark_started(&mut self.phase)?;
        Ok(Begin::CollectSecret(self.prompt.clone()))
    }

    fn complete(&mut self, input: CallbackInput<'_>) -> Result<FlowOutcome, AuthFlowError> {
        require_started(self.phase)?;
        let CallbackInput::Secret { identifier, secret } = input else {
            return Err(AuthFlowError::WrongInputSecret);
        };
        if secret.trim().is_empty() {
            return Err(AuthFlowError::InvalidInput(format!(
                "{} is required",
                self.prompt.secret_label
            )));
        }
        let identifier = match identifier {
            Some(value) if !value.trim().is_empty() => Some(value.trim().to_string()),
            _ if self.prompt.identifier_required => {
                return Err(AuthFlowError::InvalidInput(format!(
                    "{} is required",
                    self.prompt.identifier_label.unwrap_or("identifier")
                )));
            }
            _ => None,
        };
        self.phase = Phase::Completed;
        Ok(FlowOutcome::StaticCredential(
            Credential::from_static_secret(
                self.config.platform,
                self.config.auth_mode,
                identifier,
                SecretString::new(secret),
            ),
        ))
    }
}

fn build_redirect_request(
    config: &FlowConfig,
    flow: FlowKind,
    pkce: &Pkce,
    state: &StateToken,
    instructions: &'static str,
) -> Result<AuthorizeRequest, AuthFlowError> {
    let scope = config.scopes.join(" ");
    let mut params: Vec<(&str, &str)> = vec![
        ("response_type", "code"),
        ("client_id", config.client_id.as_str()),
        ("redirect_uri", config.redirect_uri.as_str()),
    ];
    if !scope.is_empty() {
        params.push(("scope", scope.as_str()));
    }
    params.push(("state", state.expose()));
    params.push(("code_challenge", pkce.challenge()));
    params.push(("code_challenge_method", pkce.method()));
    for (key, value) in &config.extra_params {
        params.push((key.as_str(), value.as_str()));
    }
    let url = build_authorize_url(&config.authorize_endpoint, &params)?;
    Ok(AuthorizeRequest {
        platform: config.platform,
        flow,
        url,
        redirect_uri: config.redirect_uri.clone(),
        state: state.expose().to_string(),
        instructions,
    })
}

fn secret_prompt(platform: AuthPlatform) -> Option<SecretPrompt> {
    match platform {
        AuthPlatform::Bluesky => Some(SecretPrompt {
            platform,
            auth_mode: AuthMode::AppPassword,
            title: "Connect Bluesky",
            identifier_label: Some("Bluesky handle"),
            identifier_required: true,
            secret_label: "App password",
            help_url: "https://bsky.app/settings/app-passwords",
            help: "Create an app password in Bluesky's settings. It can be revoked there \
                   without changing your account password.",
        }),
        AuthPlatform::Mastodon => Some(SecretPrompt {
            platform,
            auth_mode: AuthMode::PersonalAccessToken,
            title: "Connect Mastodon",
            identifier_label: Some("Instance URL"),
            identifier_required: true,
            secret_label: "Personal access token",
            help_url: "https://docs.joinmastodon.org/client/token/",
            help: "In your instance: Preferences -> Development -> New application, then copy \
                   its access token.",
        }),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::{AppPasswordFlow, FlowConfig, HeadlessPasteFlow, LoopbackRedirectFlow};
    use crate::error::AuthFlowError;
    use crate::platform::{AuthMode, AuthPlatform};

    fn x_config() -> FlowConfig {
        FlowConfig::new(
            AuthPlatform::X,
            "fic-x-client",
            "https://fic.example.invalid/callback",
            AuthMode::ApiV2,
        )
        .with_scopes(["tweet.read", "tweet.write", "offline.access"])
    }

    #[test]
    fn x_cannot_use_loopback() {
        let config = FlowConfig::new(
            AuthPlatform::X,
            "fic-x-client",
            "http://127.0.0.1:8085/callback",
            AuthMode::ApiV2,
        );
        assert!(matches!(
            LoopbackRedirectFlow::new(config),
            Err(AuthFlowError::UnsupportedFlow { .. })
        ));
    }

    #[test]
    fn instagram_flow_cannot_record_the_unofficial_session_mode() {
        let config = FlowConfig::new(
            AuthPlatform::Instagram,
            "fic-meta-client",
            "https://fic.example.invalid/callback",
            AuthMode::Session,
        );
        assert!(matches!(
            HeadlessPasteFlow::new(config),
            Err(AuthFlowError::InvalidConfig(_))
        ));
    }

    #[test]
    fn bluesky_static_flow_needs_no_redirect() {
        let config = FlowConfig::new(AuthPlatform::Bluesky, "", "", AuthMode::AppPassword);
        assert!(AppPasswordFlow::new(config).is_ok());
    }

    #[test]
    fn x_paste_config_is_valid() {
        assert!(HeadlessPasteFlow::new(x_config()).is_ok());
    }
}
