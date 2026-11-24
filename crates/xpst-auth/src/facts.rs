//! Config-shaped inputs to the truth table.
//!
//! These are the facts a status read is allowed to depend on: which
//! credentials exist (as booleans, never values), which auth mode is
//! configured, and — when a probe has run — its verdict. Secrets never appear
//! here; neither does anything that needs a network call.
//!
//! The field names are deliberately the same ones the parity fixture records,
//! so a captured row deserialises straight into [`PlatformInput`].

use serde::{Deserialize, Serialize};

use crate::state::Platform;

/// Which credentials are present, by presence flag.
///
/// Every field is an observation (`Path::exists()` / `bool(field)`), never a
/// secret. This mirrors the predicates `provider_truth._configured` and
/// `token_state.token_metadata` read.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CredentialFacts {
    /// Switched on in config. The local source is always enabled.
    pub enabled: bool,
    /// The CONFIGURED auth mode (`accounts.<platform>.auth_mode`), which may
    /// differ from the mode the provider would actually use.
    #[serde(default)]
    pub auth_mode: String,
    /// TikTok publish route preference (`auto` / `browser` / `browser_only`).
    #[serde(default)]
    pub publish_mode: String,
    /// TikTok sandbox app: can post only to the test environment.
    #[serde(default)]
    pub sandbox: bool,
    /// The YouTube token file exists.
    #[serde(default)]
    pub token_file_exists: bool,
    /// The YouTube token document parsed to a non-empty mapping.
    #[serde(default)]
    pub token_document_present: bool,
    /// The X cookie jar exists.
    #[serde(default)]
    pub cookies_file_exists: bool,
    /// The Instagram session file exists.
    #[serde(default)]
    pub session_file_exists: bool,
    /// The local source path exists.
    #[serde(default)]
    pub local_path_exists: bool,
    /// The configured local source path (`accounts.local.path`), verbatim.
    #[serde(default)]
    pub local_path: String,
    /// TikTok's browser-mode cookie jar exists (configured path or the
    /// `credentials/tiktok_cookies.txt` default).
    #[serde(default)]
    pub browser_jar_exists: bool,
    /// TikTok reads cookies out of a browser profile.
    #[serde(default)]
    pub cookies_from_browser: bool,
    /// A username is configured (TikTok source).
    #[serde(default)]
    pub username: bool,
    /// A Bluesky handle is configured.
    #[serde(default)]
    pub identifier: bool,
    /// TikTok Content Posting API app key.
    #[serde(default)]
    pub client_key: bool,
    /// An access token is configured.
    #[serde(default)]
    pub access_token: bool,
    /// A refresh token is configured.
    #[serde(default)]
    pub refresh_token: bool,
    /// X API v2 consumer key.
    #[serde(default)]
    pub api_key: bool,
    /// X API v2 consumer secret.
    #[serde(default)]
    pub api_secret: bool,
    /// X API v2 access token secret.
    #[serde(default)]
    pub access_token_secret: bool,
    /// X API v2 bearer token.
    #[serde(default)]
    pub bearer_token: bool,
    /// Instagram/Meta Graph access token.
    #[serde(default)]
    pub graph_access_token: bool,
    /// Instagram's Graph user id (`graph_ig_user_id`).
    #[serde(default)]
    pub graph_ig_user_id: bool,
    /// Threads user id.
    #[serde(default)]
    pub threads_user_id: bool,
    /// Facebook Page id.
    #[serde(default)]
    pub page_id: bool,
    /// Facebook Page access token.
    #[serde(default)]
    pub page_access_token: bool,
    /// The probe reported encrypted credentials in the store.
    #[serde(default)]
    pub credentials_stored: bool,
    /// `token_state.token_metadata(..).configured` — the credential-presence
    /// answer the badge layer uses.
    #[serde(default)]
    pub token_configured: bool,
    /// A refresh path exists for this credential.
    #[serde(default)]
    pub has_refresh_token: bool,
    /// Access-token expiry, epoch seconds, when the provider reports one.
    #[serde(default)]
    pub expires_at: Option<f64>,
    /// TikTok's publishing provider (`local` / `zernio`).
    #[serde(default)]
    pub provider: String,
    /// The aggregator's connected account id (TikTok `zernio` route).
    #[serde(default)]
    pub aggregator_account_id: String,
}

/// The probe's classification, when it produced one
/// (`xpst.utils.probe_errors.PROBE_*`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProbeClass {
    /// The probe ran and the credential works.
    Ok,
    /// The provider rejected the stored credential.
    InvalidCredentials,
    /// The probe could not reach a verdict: transport error, challenge,
    /// anti-bot redirect, 429/5xx. Nothing about the credential is proven.
    Unverified,
    /// The provider is switched off.
    Disabled,
}

impl ProbeClass {
    /// The wire name.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::InvalidCredentials => "invalid_credentials",
            Self::Unverified => "unverified",
            Self::Disabled => "disabled",
        }
    }
}

/// The non-secret part of one uploader `check_health()` result.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProbeDetails {
    /// The probe classification, when the engine recorded one.
    #[serde(default)]
    pub probe_class: Option<String>,
}

/// One live probe verdict (`xpst.auth_status` entry shape, secrets stripped).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProbeResult {
    /// The credential was accepted by the provider.
    #[serde(default)]
    pub authenticated: bool,
    /// The session is still valid. Absent means "not reported"; the engine
    /// reads that as true, because only `authenticated` gates liveness.
    #[serde(default)]
    pub session_valid: Option<bool>,
    /// A probe ran. `false` means "no verdict", not "invalid".
    #[serde(default)]
    pub live_checked: bool,
    /// The probe's failure text, verbatim.
    #[serde(default)]
    pub error: Option<String>,
    /// Probe classification details.
    #[serde(default)]
    pub details: ProbeDetails,
    /// The probe's full `details` mapping, verbatim.
    ///
    /// `details` above carries only the classification the truth table reads;
    /// this is the whole non-secret mapping the payload renders (TikTok's
    /// `yt_dlp_path`, Instagram's `probe`, the local source's path flags). It is
    /// a `Value` so an unknown key survives instead of being dropped.
    #[serde(default)]
    pub raw_details: serde_json::Value,
    /// The mode this particular check used, when it reports one.
    #[serde(default)]
    pub auth_mode: Option<String>,
    /// Encrypted credentials were found in the store.
    #[serde(default)]
    pub credentials_stored: bool,
    /// The probe reported the role as configured (`raw["configured"]`).
    ///
    /// `provider_truth._configured` accepts this as proof the inputs exist, so
    /// a probe that answers "configured" must not be overruled by a path check.
    #[serde(default)]
    pub configured: bool,
}

impl ProbeResult {
    /// Whether the probe proved the credential usable.
    #[must_use]
    pub fn is_live_ok(&self) -> bool {
        self.authenticated && self.session_valid.unwrap_or(true)
    }

    /// The probe classification, if any.
    #[must_use]
    pub fn probe_class(&self) -> Option<ProbeClass> {
        match self.details.probe_class.as_deref() {
            Some("ok") => Some(ProbeClass::Ok),
            Some("invalid_credentials") => Some(ProbeClass::InvalidCredentials),
            Some("unverified") => Some(ProbeClass::Unverified),
            Some("disabled") => Some(ProbeClass::Disabled),
            _ => None,
        }
    }

    /// The failure text with surrounding whitespace removed.
    #[must_use]
    pub fn error_text(&self) -> &str {
        self.error.as_deref().unwrap_or("").trim()
    }
}

/// Everything the truth table is allowed to know about one provider.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlatformInput {
    /// Which provider this row describes.
    pub platform: Platform,
    /// Credential-presence facts from config.
    pub facts: CredentialFacts,
    /// The provider-level probe verdict, if one ran.
    #[serde(default)]
    pub live: Option<ProbeResult>,
    /// TikTok's download-side verdict, which is independent of posting.
    #[serde(default)]
    pub source_live: Option<ProbeResult>,
    /// TikTok's posting-side verdict.
    #[serde(default)]
    pub destination_live: Option<ProbeResult>,
    /// When the probe that produced `live` ran, epoch seconds.
    #[serde(default)]
    pub checked_at: Option<f64>,
    /// A recorded refresh attempt failed inside its TTL, so a refreshable
    /// token must be reported as needing the human instead.
    #[serde(default)]
    pub refresh_failed: bool,
}

impl PlatformInput {
    /// An ENABLED platform with no credentials configured and no probe: the
    /// shape a fresh install has. Whether the platform is switched on is the
    /// caller's fact to set, so it defaults to on here.
    #[must_use]
    pub fn no_credentials(platform: Platform) -> Self {
        Self {
            platform,
            facts: CredentialFacts {
                enabled: true,
                ..CredentialFacts::default()
            },
            live: None,
            source_live: None,
            destination_live: None,
            checked_at: None,
            refresh_failed: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_session_valid_reads_as_valid() {
        let probe = ProbeResult {
            authenticated: true,
            session_valid: None,
            ..ProbeResult::default()
        };
        assert!(probe.is_live_ok());
    }

    #[test]
    fn explicit_invalid_session_is_not_ok() {
        let probe = ProbeResult {
            authenticated: true,
            session_valid: Some(false),
            ..ProbeResult::default()
        };
        assert!(!probe.is_live_ok());
    }

    #[test]
    fn unknown_probe_class_is_none_not_a_guess() {
        let probe = ProbeResult {
            details: ProbeDetails {
                probe_class: Some("something_new".to_owned()),
            },
            ..ProbeResult::default()
        };
        assert_eq!(probe.probe_class(), None);
    }
}
