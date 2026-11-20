//! The platforms this crate acquires credentials for, their auth modes, and —
//! as data, not prose — which flow shape each one can and cannot use.
//!
//! The capability matrix below is the load-bearing part. A UI that asks "can I
//! run the loopback flow for X?" gets [`Support::No`] with the reason attached,
//! and cannot render a button that will fail later.
//!
//! | platform | loopback redirect | headless paste-the-redirect | static secret |
//! |---|---|---|---|
//! | YouTube | yes (documented desktop/installed-app flow) | only by running the loopback yourself | no |
//! | X | no (registered callback; this project completes it headlessly) | yes | no |
//! | Instagram, Threads, Facebook | no (Meta requires HTTPS + a registered URI) | only with a redirect URI you host | no |
//! | Bluesky | no | no | yes — app password |
//! | Mastodon | no | no | yes — personal access token |
//!
//! Deliberately absent: TikTok (its Content Posting API loopback flow lives in
//! the provider crate), Messenger (page access token provisioning is a Meta app
//! review concern, not an end-user flow), Rumble/Local (no credential flow).

use serde::{Deserialize, Serialize};

/// A platform whose credential acquisition this crate models.
///
/// This is the auth-flow subset, not the full provider catalog. It overlaps
/// [`xpst_auth::Platform`] by wire name; Mastodon is new here because the
/// truth-table crate does not model it yet.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthPlatform {
    /// Google / YouTube Data + Upload API.
    YouTube,
    /// X API v2.
    X,
    /// Instagram Graph API.
    Instagram,
    /// Threads API.
    Threads,
    /// Facebook Graph API.
    Facebook,
    /// AT Protocol (Bluesky).
    Bluesky,
    /// Mastodon, via a personal access token on the user's instance.
    Mastodon,
}

impl AuthPlatform {
    /// Every platform this crate models.
    pub const ALL: [Self; 7] = [
        Self::YouTube,
        Self::X,
        Self::Instagram,
        Self::Threads,
        Self::Facebook,
        Self::Bluesky,
        Self::Mastodon,
    ];

    /// The wire name, identical to the provider catalog's name.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::YouTube => "youtube",
            Self::X => "x",
            Self::Instagram => "instagram",
            Self::Threads => "threads",
            Self::Facebook => "facebook",
            Self::Bluesky => "bluesky",
            Self::Mastodon => "mastodon",
        }
    }

    /// Human-facing name, used in prompts and advisories.
    #[must_use]
    pub fn display_name(self) -> &'static str {
        match self {
            Self::YouTube => "YouTube",
            Self::X => "X",
            Self::Instagram => "Instagram",
            Self::Threads => "Threads",
            Self::Facebook => "Facebook",
            Self::Bluesky => "Bluesky",
            Self::Mastodon => "Mastodon",
        }
    }

    /// Parse a wire name. Unknown names are `None`, never a guess.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == name)
    }

    /// Which flow shapes this platform can actually complete.
    #[must_use]
    pub fn flow_support(self) -> FlowSupport {
        match self {
            Self::YouTube => FlowSupport {
                loopback: Support::Yes,
                headless_paste: Support::Contingent(
                    "Google's installed-app client redirects to http://127.0.0.1:<port>; \
                     without a listening socket the browser shows a connection error, and the \
                     operator can still copy the address bar. The supported path is loopback.",
                ),
                static_secret: Support::No(
                    "Google has no app-password or pasteable-token credential for the upload API; \
                     the desktop flow issues a refresh token through the redirect.",
                ),
            },
            Self::X => FlowSupport {
                loopback: Support::No(
                    "X binds each app to a registered callback URL. This project completes X's \
                     OAuth 2.0 PKCE flow headlessly: print the authorize URL, then read the \
                     redirect URL the browser lands on back from the operator.",
                ),
                headless_paste: Support::Yes,
                static_secret: Support::No(
                    "X's official static path is OAuth 1.0a request signing (four keys), not one \
                     pasteable token. That is provider-crate work, not this flow shape.",
                ),
            },
            Self::Instagram | Self::Threads | Self::Facebook => FlowSupport {
                loopback: Support::No(
                    "Meta requires an HTTPS redirect URI registered on the app; \
                     http://127.0.0.1 loopback URIs are rejected.",
                ),
                headless_paste: Support::Contingent(
                    "Supported only when the caller supplies an HTTPS redirect URI it hosts \
                     (Meta verifies it against the app). The operator copies the redirect out of \
                     the browser; no loopback listener is involved.",
                ),
                static_secret: Support::No(
                    "Meta has no app-password shape. Credentials come from the OAuth redirect \
                     after app review.",
                ),
            },
            Self::Bluesky => FlowSupport {
                loopback: Support::No(
                    "AT Protocol login is handle + app password; this build has no OAuth consent \
                     redirect for Bluesky.",
                ),
                headless_paste: Support::No("There is no redirect to paste for an app password."),
                static_secret: Support::Yes,
            },
            Self::Mastodon => FlowSupport {
                loopback: Support::No(
                    "xPST uses a personal access token created in the instance's own settings; \
                     no app registration or redirect is involved.",
                ),
                headless_paste: Support::No(
                    "There is no redirect; the token is copied from the instance's settings.",
                ),
                static_secret: Support::Yes,
            },
        }
    }

    /// The auth mode a flow of this shape must record for this platform.
    ///
    /// Returning a mode (rather than accepting one from the caller) is what
    /// stops an Instagram flow from being constructed as `auth_mode: session`
    /// — the ban-risky private-API path — while looking like an official OAuth
    /// flow.
    #[must_use]
    pub fn required_auth_mode(self, flow: FlowKind) -> Option<AuthMode> {
        match (self, flow) {
            (Self::YouTube, FlowKind::LoopbackRedirect) => Some(AuthMode::OauthLoopback),
            // Contingent only: Google's installed-app redirect is loopback, so
            // a paste still uses that URI; the flow itself is the same OAuth.
            (Self::YouTube, FlowKind::HeadlessPaste) => Some(AuthMode::OauthLoopback),
            (Self::X, FlowKind::HeadlessPaste) => Some(AuthMode::ApiV2),
            (Self::Instagram | Self::Threads | Self::Facebook, FlowKind::HeadlessPaste) => {
                Some(AuthMode::GraphApi)
            }
            (Self::Bluesky, FlowKind::StaticSecret) => Some(AuthMode::AppPassword),
            (Self::Mastodon, FlowKind::StaticSecret) => Some(AuthMode::PersonalAccessToken),
            _ => None,
        }
    }
}

impl std::fmt::Display for AuthPlatform {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// The three credential-acquisition shapes this crate implements.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FlowKind {
    /// RFC 8252 loopback: bind 127.0.0.1, read the redirect from the browser.
    LoopbackRedirect,
    /// Print the authorize URL, then read back the URL the browser landed on.
    HeadlessPaste,
    /// No browser: an app password or personal access token pasted directly.
    StaticSecret,
}

impl FlowKind {
    /// Wire name used in diagnostics.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::LoopbackRedirect => "loopback_redirect",
            Self::HeadlessPaste => "headless_paste",
            Self::StaticSecret => "static_secret",
        }
    }
}

/// Whether a platform supports a flow shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Support {
    /// Documented and supported.
    Yes,
    /// Possible, but outside the supported path; the payload says why.
    Contingent(&'static str),
    /// Not possible. The payload is the honest reason, shown to the user.
    No(&'static str),
}

impl Support {
    /// Supportive enough to attempt the flow.
    #[must_use]
    pub fn is_allowed(self) -> bool {
        matches!(self, Self::Yes | Self::Contingent(_))
    }

    /// The reason, when there is one.
    #[must_use]
    pub fn reason(self) -> Option<&'static str> {
        match self {
            Self::Yes => None,
            Self::Contingent(reason) | Self::No(reason) => Some(reason),
        }
    }
}

/// One platform's support for each flow shape.
#[derive(Debug, Clone, Copy)]
pub struct FlowSupport {
    /// Loopback redirect (RFC 8252).
    pub loopback: Support,
    /// Headless paste-the-redirect.
    pub headless_paste: Support,
    /// App password / personal access token.
    pub static_secret: Support,
}

impl FlowSupport {
    /// Look up one shape.
    #[must_use]
    pub fn get(&self, kind: FlowKind) -> Support {
        match kind {
            FlowKind::LoopbackRedirect => self.loopback,
            FlowKind::HeadlessPaste => self.headless_paste,
            FlowKind::StaticSecret => self.static_secret,
        }
    }

    /// Whether this flow shape may be attempted at all.
    #[must_use]
    pub fn allows(&self, kind: FlowKind) -> bool {
        self.get(kind).is_allowed()
    }
}

/// The credential kind stored alongside a token, matching the Python engine's
/// `auth_mode` strings exactly (`session`, `cookies`, `api_v2`, `graph_api`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthMode {
    /// YouTube / Google installed-app OAuth.
    OauthLoopback,
    /// X official API v2 credentials.
    ApiV2,
    /// Meta Graph API credentials.
    GraphApi,
    /// Instagram private mobile API via instagrapi. UNOFFICIAL.
    Session,
    /// X web session replayed by twikit. UNOFFICIAL.
    Cookies,
    /// Bluesky app password.
    AppPassword,
    /// Mastodon personal access token.
    PersonalAccessToken,
}

impl AuthMode {
    /// The wire name, identical to the Python config's `auth_mode`.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::OauthLoopback => "oauth_loopback",
            Self::ApiV2 => "api_v2",
            Self::GraphApi => "graph_api",
            Self::Session => "session",
            Self::Cookies => "cookies",
            Self::AppPassword => "app_password",
            Self::PersonalAccessToken => "personal_access_token",
        }
    }

    /// Parse a stored `auth_mode`. Unknown values are `None`, never a guess.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        [
            Self::OauthLoopback,
            Self::ApiV2,
            Self::GraphApi,
            Self::Session,
            Self::Cookies,
            Self::AppPassword,
            Self::PersonalAccessToken,
        ]
        .into_iter()
        .find(|item| item.as_str() == value)
    }
}

impl std::fmt::Display for AuthMode {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}
