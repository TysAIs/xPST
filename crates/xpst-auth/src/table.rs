//! The auth/readiness truth table.
//!
//! These are the transition rules from `src/xpst/provider_truth.py` and
//! `src/xpst/token_state.py`, expressed once as data-to-data functions. No
//! function here performs I/O, reads a clock, or calls a provider: every fact
//! arrives in [`PlatformInput`], including the "now" that ages a probe.
//!
//! Three separate verdicts live here because the Python engine keeps them
//! separate and the surfaces rely on the separation:
//!
//! * [`role_state`] — can this ROLE be used (ready / unconfigured / …).
//! * [`token_state`] — what should the human be told about the credential
//!   (valid / expired / source-only / needs re-auth).
//! * [`ban_risk`] — is this platform on an auth path its provider forbids.
//!
//! Folding any two of them together is how xPST used to report an expired
//! Instagram session as connected.

use crate::facts::{PlatformInput, ProbeClass, ProbeResult};
use crate::state::{Badge, BanRisk, Platform, ProviderState, Role, Severity, TokenState};

/// How long before expiry a token WITHOUT a refresh path starts warning.
/// (`token_state.DEFAULT_EXPIRING_WINDOW_SECONDS`)
const EXPIRING_WINDOW_SECONDS: f64 = 24.0 * 3600.0;

/// Lead time for a credential only the human can renew.
/// (`token_state.DEFAULT_REAUTH_WINDOW_SECONDS`)
const REAUTH_WINDOW_SECONDS: f64 = 7.0 * 86400.0;

/// A live check older than this may not claim green any more.
/// (`token_state.DEFAULT_MAX_CHECK_AGE_SECONDS`)
const MAX_CHECK_AGE_SECONDS: f64 = 15.0 * 60.0;

/// A ban-risk advisory for one provider, as this config currently stands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BanRiskAdvisory {
    /// The code every payload carries.
    pub code: BanRisk,
    /// The code preflight/route plans carry.
    pub advisory_code: &'static str,
    /// How loudly it reports.
    pub severity: Severity,
    /// The platform is switched on, so a post could actually use the path.
    pub enabled: bool,
    /// The operator recorded that they accepted this path.
    pub acknowledged: bool,
}

/// The whole verdict for one provider.
#[derive(Debug, Clone, PartialEq)]
pub struct Verdict {
    /// Can the provider be used for what it is listed as.
    pub provider_state: ProviderState,
    /// One row per role the provider supports, in catalog order.
    pub role_states: Vec<(Role, ProviderState)>,
    /// The auth mode the provider would actually use.
    pub auth_mode: String,
    /// The credential-truth state, for providers that carry a badge.
    pub token_state: Option<TokenState>,
    /// The badge a human sees.
    pub badge: Option<Badge>,
    /// xPST can renew this credential without the human. `None` for providers
    /// the badge surface does not report on.
    pub auto_refresh: Option<bool>,
    /// The provider is wired up as a download source, not a destination.
    pub source_only: bool,
    /// The posting role is ready according to the table.
    pub can_post: bool,
    /// Ban-risk advisory, when the configured path is not the official one.
    pub ban_risk: Option<BanRiskAdvisory>,
}

impl Verdict {
    /// The state of one role, if the provider has it.
    #[must_use]
    pub fn role(&self, role: Role) -> Option<ProviderState> {
        self.role_states
            .iter()
            .find(|(candidate, _)| *candidate == role)
            .map(|(_, state)| *state)
    }
}

/// Whether the configured auth mode is one of the provider's unofficial paths.
fn ban_risk(input: &PlatformInput) -> Option<BanRiskAdvisory> {
    // One registry (`auth_path_risk.UNOFFICIAL_AUTH_PATHS`) decides which paths
    // are unofficial, so the truth table and the doctor advisory cannot drift.
    let entry = crate::auth_path_risk::unofficial_path(input.platform)?;
    if input.facts.auth_mode.trim().to_ascii_lowercase() != entry.auth_mode {
        return None;
    }
    Some(BanRiskAdvisory {
        code: entry.ban_risk,
        advisory_code: entry.ban_risk.advisory_code(),
        severity: entry.severity,
        enabled: input.facts.enabled,
        // Acknowledged paths are recorded on disk; no acknowledgement file
        // exists for these inputs, so the advisory must read as unacknowledged
        // rather than pretend the operator accepted it.
        acknowledged: false,
    })
}

/// The auth mode the engine would actually use for `platform`.
///
/// A mode reported by the probe wins, then the configured mode, then the
/// provider's own rule (TikTok derives its route from the credentials it
/// holds), then the catalog default.
pub(crate) fn resolve_auth_mode(
    platform: Platform,
    facts: &crate::facts::CredentialFacts,
    probe: Option<&ProbeResult>,
) -> String {
    if let Some(reported) = probe.and_then(|item| item.auth_mode.as_deref()) {
        if !reported.is_empty() {
            return reported.to_owned();
        }
    }
    if !facts.auth_mode.is_empty() {
        // X validates its mode against the two it implements; anything else
        // falls back to the cookie path rather than inventing a mode.
        if platform == Platform::X && !matches!(facts.auth_mode.as_str(), "cookies" | "api_v2") {
            return "cookies".to_owned();
        }
        return facts.auth_mode.clone();
    }
    if platform == Platform::TikTok {
        if facts.client_key && (facts.access_token || facts.refresh_token) {
            return "content_posting_api".to_owned();
        }
        if matches!(facts.publish_mode.as_str(), "browser" | "browser_only") {
            return "session".to_owned();
        }
        return "source_only".to_owned();
    }
    platform.default_auth_mode().to_owned()
}

/// The probe a role reads: TikTok's two capabilities are separate probes.
pub(crate) fn probe_for(input: &PlatformInput, role: Role) -> Option<&ProbeResult> {
    if input.platform == Platform::TikTok {
        match role {
            Role::Source => return input.source_live.as_ref().or(input.live.as_ref()),
            Role::VideoDestination => {
                return input.destination_live.as_ref().or(input.live.as_ref());
            }
            Role::Analytics | Role::Messaging => {}
        }
    }
    input.live.as_ref()
}

/// Whether the inputs one role needs have been supplied
/// (`provider_truth._configured`).
///
/// Only consulted when a probe says no or never ran: a live success always
/// wins, and stored encrypted credentials count even without a credential
/// path.
pub(crate) fn configured(input: &PlatformInput, role: Role) -> bool {
    // `_configured` accepts the ROLE's own raw result as proof first: a probe
    // that reports `credentials_stored` / `configured` has already answered the
    // question, and a path check must not overrule it.
    if let Some(probe) = probe_for(input, role) {
        if probe.credentials_stored || probe.configured {
            return true;
        }
    }
    if input.facts.credentials_stored {
        return true;
    }
    let facts = &input.facts;
    match input.platform {
        Platform::Local => facts.local_path_exists,
        Platform::TikTok => match role {
            Role::Source => {
                facts.username || facts.cookies_from_browser || facts.cookies_file_exists
            }
            Role::VideoDestination | Role::Analytics => {
                if facts.client_key && (facts.access_token || facts.refresh_token) {
                    return true;
                }
                // Browser-native publish mode: the credential is the exported
                // web cookie jar, not an OAuth token.
                matches!(facts.publish_mode.as_str(), "browser" | "browser_only")
                    && facts.browser_jar_exists
            }
            Role::Messaging => false,
        },
        Platform::X => {
            if facts.auth_mode == "api_v2" {
                facts.bearer_token
                    || (facts.api_key
                        && facts.api_secret
                        && facts.access_token
                        && facts.access_token_secret)
            } else {
                facts.cookies_file_exists
            }
        }
        Platform::Instagram => {
            if facts.auth_mode == "graph_api" {
                // Legacy installs may hold only a session file while the
                // default graph mode is selected: configured data, but a live
                // probe still decides whether it is healthy.
                facts.graph_access_token || facts.session_file_exists
            } else {
                facts.session_file_exists
            }
        }
        Platform::YouTube => facts.token_file_exists,
        Platform::Threads => facts.graph_access_token && facts.threads_user_id,
        Platform::Bluesky => facts.identifier,
        Platform::Messenger => facts.page_access_token,
        Platform::Facebook => facts.page_id && facts.page_access_token,
        // No configuration path is defined for Rumble's API key, so nothing
        // can be "configured" for it yet.
        Platform::Rumble => false,
    }
}

/// The state of one role, or `None` when the provider does not have it.
pub(crate) fn role_state(input: &PlatformInput, role: Role) -> ProviderState {
    let probe = probe_for(input, role);
    if !input.facts.enabled {
        return ProviderState::Disabled;
    }
    if input.platform == Platform::TikTok && role == Role::VideoDestination {
        if input.facts.sandbox && configured(input, role) {
            return ProviderState::BlockedExternalReview;
        }
        let error_text = probe
            .map_or("", ProbeResult::error_text)
            .to_ascii_lowercase();
        if error_text.contains("external review") || error_text.contains("review required") {
            return ProviderState::BlockedExternalReview;
        }
    }
    if probe.is_some_and(ProbeResult::is_live_ok) {
        return ProviderState::Ready;
    }
    if probe
        .map_or("", ProbeResult::error_text)
        .eq_ignore_ascii_case("disabled")
    {
        return ProviderState::Disabled;
    }
    if !probe.is_some_and(|item| item.live_checked) {
        // No verdict yet: configured data is all we can report. This is a
        // config-only "ready", which is why the badge stays non-green.
        return if configured(input, role) {
            ProviderState::Ready
        } else {
            ProviderState::Unconfigured
        };
    }
    if !configured(input, role) {
        return ProviderState::Unconfigured;
    }
    ProviderState::Degraded
}

/// The provider-level state: the primary role speaks, unless a better
/// explanation exists (`provider_truth._aggregate_state`).
pub(crate) fn aggregate(platform: Platform, states: &[(Role, ProviderState)]) -> ProviderState {
    let primary = platform.primary_role();
    let primary_state = states
        .iter()
        .find(|(role, _)| *role == primary)
        .map_or(ProviderState::Unconfigured, |(_, state)| *state);
    if primary_state == ProviderState::Ready {
        return primary_state;
    }
    for wanted in [
        ProviderState::Degraded,
        ProviderState::BlockedExternalReview,
        ProviderState::Unconfigured,
    ] {
        if states.iter().any(|(_, state)| *state == wanted) {
            return wanted;
        }
    }
    primary_state
}

/// Which role's facts describe the provider as a whole.
///
/// The posting role is the promise everywhere except a TikTok download source,
/// where the source verdict is the only capability that exists.
pub(crate) fn compat_role(input: &PlatformInput) -> Role {
    let primary = input.platform.primary_role();
    if input.platform == Platform::TikTok && input.source_live.is_some() {
        let mode = resolve_auth_mode(input.platform, &input.facts, input.live.as_ref());
        if mode == "source_only" {
            return Role::Source;
        }
    }
    primary
}

/// Derive the credential truth for the provider
/// (`token_state.derive_token_state`).
fn token_state(
    input: &PlatformInput,
    compat: Option<&ProbeResult>,
    auth_mode: &str,
    now: f64,
    any_live_checked: bool,
) -> TokenState {
    let facts = &input.facts;
    let live_ok = compat.is_some_and(ProbeResult::is_live_ok);
    let live_checked = any_live_checked;
    let error_text = compat.map_or("", ProbeResult::error_text);

    let age = match input.checked_at {
        Some(checked_at) => (now - checked_at).max(0.0),
        // A probe handed to us without a timestamp was taken as part of this
        // read; it cannot be stale.
        None => 0.0,
    };

    if !facts.enabled || error_text.eq_ignore_ascii_case("disabled") {
        return TokenState::Disabled;
    }
    if auth_mode == "source_only" {
        return TokenState::SourceOnly;
    }
    if !live_checked {
        return TokenState::Unknown;
    }
    if age > MAX_CHECK_AGE_SECONDS {
        return TokenState::Unknown;
    }
    if live_ok {
        if facts.has_refresh_token {
            // xPST renews it in the background; the badge must not nag.
            return TokenState::Valid;
        }
        let Some(expires_at) = facts.expires_at else {
            return TokenState::Valid;
        };
        let expires_in = expires_at - now;
        if expires_in <= EXPIRING_WINDOW_SECONDS.max(REAUTH_WINDOW_SECONDS) {
            return TokenState::Expiring;
        }
        return TokenState::Valid;
    }
    if !facts.token_configured {
        return TokenState::Unconfigured;
    }
    if compat.and_then(ProbeResult::probe_class) == Some(ProbeClass::Unverified) {
        // The probe failed without proving the credential dead: not green,
        // and not a demand for the password either.
        return TokenState::Unknown;
    }
    if facts.has_refresh_token && !input.refresh_failed {
        return TokenState::ExpiredRefreshable;
    }
    TokenState::ExpiredUnrefreshable
}

/// The posting verdict for a provider (`provider_truth.posting_truth`).
///
/// `source_only` is true only when the provider HAS a posting role that is not
/// usable while its effective auth mode is `source_only` — it is wired up as a
/// download source, not as an upload destination.
pub(crate) fn posting(
    input: &PlatformInput,
    states: &[(Role, ProviderState)],
    auth_mode: &str,
) -> (bool, bool) {
    let posting_role = [Role::VideoDestination, Role::Messaging]
        .into_iter()
        .find(|candidate| states.iter().any(|(role, _)| role == candidate));
    let Some(role) = posting_role else {
        return (false, false);
    };
    let state = states
        .iter()
        .find(|(candidate, _)| *candidate == role)
        .map_or(ProviderState::Unconfigured, |(_, state)| *state);
    let can_post = state == ProviderState::Ready;
    let role_mode = resolve_auth_mode(input.platform, &input.facts, probe_for(input, role));
    let source_only = !can_post && (auth_mode == "source_only" || role_mode == "source_only");
    (source_only, can_post)
}

/// Compute every verdict for one provider.
///
/// `now` is the epoch second that ages the recorded probe; it is an explicit
/// argument so the table never reads a clock of its own.
#[must_use]
pub fn evaluate(input: &PlatformInput, now: f64) -> Verdict {
    let role_states: Vec<(Role, ProviderState)> = input
        .platform
        .roles()
        .iter()
        .map(|role| (*role, role_state(input, *role)))
        .collect();
    let provider_state = aggregate(input.platform, &role_states);

    let compat = compat_role(input);
    let compat_probe = probe_for(input, compat);
    let auth_mode = resolve_auth_mode(input.platform, &input.facts, compat_probe);
    let any_live_checked = role_states
        .iter()
        .any(|(role, _)| probe_for(input, *role).is_some_and(|item| item.live_checked));

    let (token_state, badge, auto_refresh) = if input.platform.has_badge() {
        let state = token_state(input, compat_probe, &auth_mode, now, any_live_checked);
        (
            Some(state),
            Some(state.badge()),
            Some(input.facts.has_refresh_token),
        )
    } else {
        (None, None, None)
    };
    let (source_only, can_post) = posting(input, &role_states, &auth_mode);

    Verdict {
        provider_state,
        role_states,
        auth_mode,
        token_state,
        badge,
        auto_refresh,
        source_only,
        can_post,
        ban_risk: ban_risk(input),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::CredentialFacts;

    fn youtube_live_ok() -> PlatformInput {
        PlatformInput {
            platform: Platform::YouTube,
            facts: CredentialFacts {
                enabled: true,
                token_file_exists: true,
                token_document_present: true,
                token_configured: true,
                ..CredentialFacts::default()
            },
            live: Some(ProbeResult {
                authenticated: true,
                session_valid: Some(true),
                live_checked: true,
                ..ProbeResult::default()
            }),
            checked_at: Some(1_000.0),
            ..PlatformInput::no_credentials(Platform::YouTube)
        }
    }

    #[test]
    fn disabled_wins_over_everything() {
        let mut input = youtube_live_ok();
        input.facts.enabled = false;
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.provider_state, ProviderState::Disabled);
        assert_eq!(verdict.token_state, Some(TokenState::Disabled));
        assert_eq!(verdict.badge, Some(Badge::Disabled));
    }

    #[test]
    fn an_empty_profile_is_unconfigured_and_never_green() {
        let input = PlatformInput::no_credentials(Platform::YouTube);
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.provider_state, ProviderState::Unconfigured);
        assert_eq!(verdict.token_state, Some(TokenState::Unknown));
        assert!(!verdict.badge.is_some_and(Badge::is_green));
    }

    #[test]
    fn a_stored_credential_is_configured_but_not_verified() {
        let mut input = PlatformInput::no_credentials(Platform::YouTube);
        input.facts.enabled = true;
        input.facts.token_file_exists = true;
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.provider_state, ProviderState::Ready);
        assert_eq!(verdict.token_state, Some(TokenState::Unknown));
        assert!(!verdict.badge.is_some_and(Badge::is_green));
    }

    #[test]
    fn a_live_pass_is_the_only_green_path() {
        let verdict = evaluate(&youtube_live_ok(), 1_000.0);
        assert_eq!(verdict.provider_state, ProviderState::Ready);
        assert_eq!(verdict.token_state, Some(TokenState::Valid));
        assert!(verdict.badge.is_some_and(Badge::is_green));
    }

    #[test]
    fn a_stale_probe_may_not_claim_green() {
        let verdict = evaluate(&youtube_live_ok(), 1_000.0 + MAX_CHECK_AGE_SECONDS + 1.0);
        assert_eq!(verdict.token_state, Some(TokenState::Unknown));
        assert!(!verdict.badge.is_some_and(Badge::is_green));
    }

    #[test]
    fn expired_without_a_refresh_path_asks_the_human() {
        let mut input = youtube_live_ok();
        input.live = Some(ProbeResult {
            authenticated: false,
            session_valid: Some(false),
            live_checked: true,
            error: Some("invalid_grant".to_owned()),
            ..ProbeResult::default()
        });
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.provider_state, ProviderState::Degraded);
        assert_eq!(verdict.token_state, Some(TokenState::ExpiredUnrefreshable));
        assert_eq!(verdict.badge, Some(Badge::NeedsReauth));
    }

    #[test]
    fn expired_with_a_refresh_token_does_not_ask_the_human() {
        let mut input = youtube_live_ok();
        input.facts.has_refresh_token = true;
        input.live = Some(ProbeResult {
            authenticated: false,
            live_checked: true,
            error: Some("invalid_grant".to_owned()),
            ..ProbeResult::default()
        });
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.token_state, Some(TokenState::ExpiredRefreshable));
        assert_eq!(verdict.badge, Some(Badge::Expiring));
    }

    #[test]
    fn a_failed_refresh_overrides_the_refresh_path() {
        let mut input = youtube_live_ok();
        input.facts.has_refresh_token = true;
        input.refresh_failed = true;
        input.live = Some(ProbeResult {
            authenticated: false,
            live_checked: true,
            error: Some("invalid_grant".to_owned()),
            ..ProbeResult::default()
        });
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.token_state, Some(TokenState::ExpiredUnrefreshable));
    }

    #[test]
    fn an_unverified_probe_is_not_a_reauth_demand() {
        let mut input = PlatformInput::no_credentials(Platform::X);
        input.facts = CredentialFacts {
            enabled: true,
            auth_mode: "cookies".to_owned(),
            cookies_file_exists: true,
            token_configured: true,
            ..CredentialFacts::default()
        };
        input.live = Some(ProbeResult {
            authenticated: false,
            live_checked: true,
            error: Some("http 429".to_owned()),
            details: crate::facts::ProbeDetails {
                probe_class: Some("unverified".to_owned()),
            },
            ..ProbeResult::default()
        });
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.provider_state, ProviderState::Degraded);
        assert_eq!(verdict.token_state, Some(TokenState::Unknown));
        assert_eq!(verdict.badge, Some(Badge::Unknown));
    }

    #[test]
    fn source_only_wins_before_any_probe_verdict() {
        let input = PlatformInput {
            platform: Platform::TikTok,
            facts: CredentialFacts {
                enabled: true,
                username: true,
                token_configured: true,
                ..CredentialFacts::default()
            },
            ..PlatformInput::no_credentials(Platform::TikTok)
        };
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(verdict.token_state, Some(TokenState::SourceOnly));
        assert_eq!(verdict.badge, Some(Badge::SourceOnly));
        assert!(verdict.source_only);
        assert!(!verdict.can_post);
        assert_eq!(verdict.role(Role::Source), Some(ProviderState::Ready));
        // The download side being ready does not make the provider usable as
        // a destination.
        assert_eq!(verdict.provider_state, ProviderState::Unconfigured);
    }

    #[test]
    fn sandbox_blocks_the_destination_role() {
        let input = PlatformInput {
            platform: Platform::TikTok,
            facts: CredentialFacts {
                enabled: true,
                sandbox: true,
                client_key: true,
                access_token: true,
                token_configured: true,
                ..CredentialFacts::default()
            },
            ..PlatformInput::no_credentials(Platform::TikTok)
        };
        let verdict = evaluate(&input, 1_000.0);
        assert_eq!(
            verdict.role(Role::VideoDestination),
            Some(ProviderState::BlockedExternalReview)
        );
        assert_eq!(verdict.provider_state, ProviderState::BlockedExternalReview);
    }

    #[test]
    fn an_unknown_x_auth_mode_falls_back_to_cookies() {
        let mut input = PlatformInput::no_credentials(Platform::X);
        input.facts.auth_mode = "oauth2_magic".to_owned();
        assert_eq!(evaluate(&input, 1_000.0).auth_mode, "cookies");
        input.facts.auth_mode = "api_v2".to_owned();
        assert_eq!(evaluate(&input, 1_000.0).auth_mode, "api_v2");
    }

    #[test]
    fn ban_risk_reports_only_unofficial_paths() {
        let mut input = PlatformInput::no_credentials(Platform::Instagram);
        input.facts.auth_mode = "session".to_owned();
        let advisory = ban_risk(&input).expect("session mode is unofficial");
        assert_eq!(advisory.code, BanRisk::UnofficialPrivateApi);
        assert_eq!(advisory.severity, Severity::High);

        input.facts.auth_mode = "graph_api".to_owned();
        assert!(ban_risk(&input).is_none());

        input.platform = Platform::X;
        input.facts.auth_mode = "cookies".to_owned();
        assert_eq!(
            ban_risk(&input).map(|item| item.code),
            Some(BanRisk::UnofficialWebClient)
        );
        input.facts.auth_mode = "api_v2".to_owned();
        assert!(ban_risk(&input).is_none());
    }
}
