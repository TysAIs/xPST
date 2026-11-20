//! The auth-state vocabulary, as typed data.
//!
//! Every name here is the name the Python engine emits in its JSON payloads
//! (`src/xpst/providers.py`, `src/xpst/token_state.py`). The Rust engine must
//! speak the same vocabulary, because the CLI, MCP surface and dashboards all
//! match on these strings — renaming a state is a breaking contract change, not
//! an internal refactor.

use serde::{Deserialize, Serialize};

/// Canonical state of one provider ROLE (`xpst.provider_truth.ProviderState`).
///
/// This is the "can this role be used right now" verdict. It deliberately does
/// not distinguish "expired" from "rejected": both are [`Self::Degraded`],
/// because the user-facing question is whether the role is usable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderState {
    /// A live probe proved the role works.
    Ready,
    /// Enabled, but the inputs this role needs were never supplied.
    Unconfigured,
    /// Switched off in config. A deliberate pause, not a failure.
    Disabled,
    /// Configured, but the live probe did not pass.
    Degraded,
    /// The provider refuses to serve this account until its own review ends.
    BlockedExternalReview,
}

impl ProviderState {
    /// The wire name, identical to the Python enum value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::Unconfigured => "unconfigured",
            Self::Disabled => "disabled",
            Self::Degraded => "degraded",
            Self::BlockedExternalReview => "blocked_external_review",
        }
    }
}

impl std::fmt::Display for ProviderState {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Honest credential state (`xpst.token_state.TOKEN_STATE_*`).
///
/// The distinction that matters: presence of a credential is never a green
/// state. Only [`Self::Valid`] maps onto a green badge, and it requires a live
/// check that passed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TokenState {
    /// A live check just proved it works.
    Valid,
    /// Usable now, but the human must renew before it dies.
    Expiring,
    /// Access token expired; xPST holds a refresh token and will renew it.
    ExpiredRefreshable,
    /// Access token expired and no refresh path exists: the human must act.
    ExpiredUnrefreshable,
    /// No credentials configured yet.
    Unconfigured,
    /// Usable as a download source only, never as an upload destination.
    SourceOnly,
    /// Switched off in config.
    Disabled,
    /// No live check has run, or the last one is too old to trust. NOT green.
    Unknown,
}

impl TokenState {
    /// The wire name, identical to the Python `TOKEN_STATE_*` constants.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Valid => "valid",
            Self::Expiring => "expiring",
            Self::ExpiredRefreshable => "expired_refreshable",
            Self::ExpiredUnrefreshable => "expired_unrefreshable",
            Self::Unconfigured => "unconfigured",
            Self::SourceOnly => "source_only",
            Self::Disabled => "disabled",
            Self::Unknown => "unknown",
        }
    }

    /// The badge a human sees (`xpst.token_state.STATE_TO_BADGE`).
    #[must_use]
    pub fn badge(self) -> Badge {
        match self {
            Self::Valid => Badge::Connected,
            Self::Expiring | Self::ExpiredRefreshable => Badge::Expiring,
            Self::ExpiredUnrefreshable | Self::Unconfigured => Badge::NeedsReauth,
            Self::SourceOnly => Badge::SourceOnly,
            Self::Disabled => Badge::Disabled,
            Self::Unknown => Badge::Unknown,
        }
    }
}

/// The rendered badge (`xpst.token_state.BADGE_*`). Only [`Self::Connected`]
/// may ever render green.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Badge {
    /// Green — a live check passed.
    Connected,
    /// Amber — act soon.
    Expiring,
    /// Red — the human must re-authenticate.
    NeedsReauth,
    /// Informational — download source only.
    SourceOnly,
    /// Informational — switched off.
    Disabled,
    /// Grey — nothing was verified.
    Unknown,
}

impl Badge {
    /// The wire name, identical to the Python `BADGE_*` constants.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Connected => "connected",
            Self::Expiring => "expiring",
            Self::NeedsReauth => "needs_reauth",
            Self::SourceOnly => "source_only",
            Self::Disabled => "disabled",
            Self::Unknown => "unknown",
        }
    }

    /// Whether this badge may render green. Exactly one may.
    #[must_use]
    pub fn is_green(self) -> bool {
        matches!(self, Self::Connected)
    }
}

/// A capability a provider can play (`xpst.providers.ProviderRole`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    /// xPST can download from it.
    Source,
    /// xPST can post a video to it.
    VideoDestination,
    /// xPST can send messages through it.
    Messaging,
    /// xPST can read engagement metrics from it.
    Analytics,
}

impl Role {
    /// The wire name, identical to the Python enum value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Source => "source",
            Self::VideoDestination => "video_destination",
            Self::Messaging => "messaging",
            Self::Analytics => "analytics",
        }
    }
}

/// One provider the truth table knows about.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Platform {
    YouTube,
    X,
    Instagram,
    Bluesky,
    TikTok,
    Threads,
    Messenger,
    Facebook,
    Rumble,
    Local,
}

impl Platform {
    /// Every provider, in the order the Python catalog reports them.
    pub const ALL: [Self; 10] = [
        Self::YouTube,
        Self::X,
        Self::Instagram,
        Self::Bluesky,
        Self::TikTok,
        Self::Threads,
        Self::Messenger,
        Self::Facebook,
        Self::Rumble,
        Self::Local,
    ];

    /// The wire name, identical to `SUPPORTED_PROVIDERS[..].name`.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::YouTube => "youtube",
            Self::X => "x",
            Self::Instagram => "instagram",
            Self::Bluesky => "bluesky",
            Self::TikTok => "tiktok",
            Self::Threads => "threads",
            Self::Messenger => "messenger",
            Self::Facebook => "facebook",
            Self::Rumble => "rumble",
            Self::Local => "local",
        }
    }

    /// Parse a wire name. Unknown providers are an error, never a guess.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == name)
    }

    /// The roles this provider supports, in catalog order.
    #[must_use]
    pub fn roles(self) -> &'static [Role] {
        match self {
            Self::YouTube | Self::X | Self::Instagram | Self::TikTok => {
                &[Role::Source, Role::VideoDestination, Role::Analytics]
            }
            Self::Threads => &[Role::VideoDestination, Role::Analytics],
            Self::Bluesky | Self::Facebook | Self::Rumble => &[Role::VideoDestination],
            Self::Messenger => &[Role::Messaging],
            Self::Local => &[Role::Source],
        }
    }

    /// The auth mode the provider's catalog definition declares, used when
    /// neither the probe nor the config names one (`provider_truth._auth_mode`).
    #[must_use]
    pub fn default_auth_mode(self) -> &'static str {
        match self {
            Self::YouTube | Self::Threads | Self::Messenger | Self::Facebook => "oauth",
            Self::X => "cookies",
            Self::Instagram => "session",
            Self::Bluesky => "app_password",
            Self::TikTok => "source_only",
            Self::Rumble => "api_key",
            Self::Local => "local",
        }
    }

    /// Whether the badge surface reports this provider. The local file source
    /// is not an account, so it carries no badge (`token_state.PLATFORM_ORDER`).
    #[must_use]
    pub fn has_badge(self) -> bool {
        !matches!(self, Self::Local | Self::Rumble)
    }

    /// The role whose verdict speaks for the provider as a whole.
    #[must_use]
    pub(crate) fn primary_role(self) -> Role {
        let roles = self.roles();
        if roles.contains(&Role::VideoDestination) {
            Role::VideoDestination
        } else if roles.contains(&Role::Messaging) {
            Role::Messaging
        } else {
            Role::Source
        }
    }
}

/// A non-official integration path that carries ban risk
/// (`xpst.auth_path_risk.BAN_RISK_*`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BanRisk {
    /// The provider's terms forbid the path outright (Instagram private API).
    UnofficialPrivateApi,
    /// Unofficial, but xPST's documented default (X web client / twikit).
    UnofficialWebClient,
}

impl BanRisk {
    /// The code carried in payloads (`BAN_RISK_*`).
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::UnofficialPrivateApi => "unofficial_private_api",
            Self::UnofficialWebClient => "unofficial_web_client",
        }
    }

    /// The advisory code a preflight/route plan carries (`ADVISORY_CODE_*`).
    #[must_use]
    pub fn advisory_code(self) -> &'static str {
        match self {
            Self::UnofficialPrivateApi => "UNOFFICIAL_PRIVATE_API",
            Self::UnofficialWebClient => "UNOFFICIAL_WEB_CLIENT",
        }
    }
}

/// How loudly a ban-risk advisory reports (`auth_path_risk` severity words).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    /// The provider's terms forbid the path: readiness warns, doctor lists it.
    High,
    /// Unofficial but the documented default: informational only.
    Advisory,
}

impl Severity {
    /// The wire name.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::High => "high",
            Self::Advisory => "advisory",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_connected_is_green() {
        assert!(Badge::Connected.is_green());
        for badge in [
            Badge::Expiring,
            Badge::NeedsReauth,
            Badge::SourceOnly,
            Badge::Disabled,
            Badge::Unknown,
        ] {
            assert!(!badge.is_green(), "{badge:?} must not render green");
        }
    }

    #[test]
    fn badge_mapping_matches_the_python_table() {
        let expected = [
            (TokenState::Valid, Badge::Connected),
            (TokenState::Expiring, Badge::Expiring),
            (TokenState::ExpiredRefreshable, Badge::Expiring),
            (TokenState::ExpiredUnrefreshable, Badge::NeedsReauth),
            (TokenState::Unconfigured, Badge::NeedsReauth),
            (TokenState::SourceOnly, Badge::SourceOnly),
            (TokenState::Disabled, Badge::Disabled),
            (TokenState::Unknown, Badge::Unknown),
        ];
        for (state, badge) in expected {
            assert_eq!(state.badge(), badge);
        }
    }

    #[test]
    fn platform_names_round_trip() {
        for platform in Platform::ALL {
            assert_eq!(Platform::parse(platform.as_str()), Some(platform));
        }
        assert_eq!(Platform::parse("mastodon"), None);
    }

    #[test]
    fn provider_roster_matches_the_canonical_catalog() {
        assert_eq!(Platform::YouTube.roles().len(), 3);
        assert_eq!(Platform::Bluesky.roles(), &[Role::VideoDestination]);
        assert_eq!(Platform::Messenger.roles(), &[Role::Messaging]);
        assert_eq!(Platform::Local.roles(), &[Role::Source]);
        assert!(!Platform::Local.has_badge());
        assert!(Platform::YouTube.has_badge());
    }
}
