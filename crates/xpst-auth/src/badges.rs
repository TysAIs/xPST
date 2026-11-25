//! Badge truth: what a human is told about a credential (`xpst.token_state`).
//!
//! The distinction this module exists to keep: **presence of a credential is
//! never a green state.** Only [`TokenState::Valid`] maps onto the green badge,
//! and it requires a live check that passed inside the freshness window. Every
//! other state is either amber (act soon), red (the human must act), or grey
//! (nothing was verified).
//!
//! Two windows, deliberately different:
//!
//! * 24 hours for a token xPST refreshes by itself — the background refresh
//!   handles it and the badge must not nag;
//! * 7 days for a credential only the human can renew (an Instagram session, a
//!   Meta long-lived token with no refresh path) — by the time a 24-hour warning
//!   fired, the user's next post would already fail.
//!
//! Nothing here performs I/O or reads a clock: `now` is an argument, and the
//! credential facts arrive as booleans.

use serde_json::{Map, Value};

use crate::canonical::ProviderFacts;
use crate::state::{Badge, Platform, TokenState};

/// How long before expiry a token WITHOUT a refresh path starts warning.
pub const EXPIRING_WINDOW_SECONDS: f64 = 24.0 * 3600.0;
/// Lead time for a credential only the human can renew.
pub const REAUTH_WINDOW_SECONDS: f64 = 7.0 * 86400.0;
/// A live check older than this may not claim green any more.
pub const MAX_CHECK_AGE_SECONDS: f64 = 15.0 * 60.0;
/// How long a recorded failed refresh keeps the badge at `needs_reauth`.
pub const REFRESH_FAILURE_TTL_SECONDS: f64 = 6.0 * 3600.0;

/// Every platform the badge surface reports on, in display order
/// (`token_state.PLATFORM_ORDER`). The local file source is not an account.
pub const PLATFORM_ORDER: [Platform; 8] = [
    Platform::YouTube,
    Platform::X,
    Platform::Instagram,
    Platform::Bluesky,
    Platform::TikTok,
    Platform::Threads,
    Platform::Facebook,
    Platform::Messenger,
];

/// The badge fields copied onto canonical entries (`BADGE_FIELD_NAMES`).
pub const BADGE_FIELD_NAMES: [&str; 13] = [
    "token_state",
    "badge",
    "badge_label",
    "badge_reason",
    "badge_action",
    "checked_at",
    "checked_at_iso",
    "check_age_seconds",
    "expires_at",
    "expires_at_iso",
    "expires_in_seconds",
    "auto_refresh",
    "token_metadata",
];

/// `BADGE_LABELS`.
#[must_use]
pub fn badge_label(badge: Badge) -> &'static str {
    match badge {
        Badge::Connected => "Connected",
        Badge::Expiring => "Expiring soon",
        Badge::NeedsReauth => "Needs re-auth",
        Badge::SourceOnly => "Source only",
        Badge::Disabled => "Disabled",
        Badge::Unknown => "Not verified",
    }
}

/// `_iso()`: epoch seconds → RFC3339 UTC with a `Z` suffix.
///
/// Python renders microseconds only when they are non-zero
/// (`datetime.isoformat()`), which is what `jiff`'s `%.f` does not do — so the
/// fraction is trimmed by hand.
#[must_use]
pub fn iso_timestamp(ts: Option<f64>) -> Option<String> {
    let ts = ts?;
    if !ts.is_finite() {
        return None;
    }
    let mut seconds = ts.floor() as i64;
    let mut micros = ((ts - ts.floor()) * 1_000_000.0).round() as i64;
    if micros >= 1_000_000 {
        seconds += 1;
        micros = 0;
    }
    let stamp = jiff::Timestamp::from_second(seconds).ok()?;
    let base = stamp.strftime("%Y-%m-%dT%H:%M:%S").to_string();
    if micros == 0 {
        Some(format!("{base}Z"))
    } else {
        Some(format!("{base}.{micros:06}Z"))
    }
}

/// `humanize_seconds()`: `3h 12m`, `45s`, `4d`.
#[must_use]
pub fn humanize_seconds(seconds: Option<f64>) -> Option<String> {
    let seconds = seconds?;
    let total = seconds.max(0.0) as i64;
    if total >= 86_400 {
        let days = total / 86_400;
        let hours = (total % 86_400) / 3600;
        return Some(if hours == 0 {
            format!("{days}d")
        } else {
            format!("{days}d {hours}h")
        });
    }
    if total >= 3600 {
        let hours = total / 3600;
        let minutes = (total % 3600) / 60;
        return Some(if minutes == 0 {
            format!("{hours}h")
        } else {
            format!("{hours}h {minutes}m")
        });
    }
    if total >= 60 {
        return Some(format!("{}m", total / 60));
    }
    Some(format!("{total}s"))
}

/// The badge-relevant, non-secret metadata for one platform
/// (`token_state.token_metadata`).
#[derive(Debug, Clone, PartialEq)]
pub struct TokenMetadata {
    pub platform: Platform,
    pub auth_mode: String,
    pub enabled: bool,
    pub configured: bool,
    pub has_refresh_token: bool,
    pub expires_at: Option<f64>,
    pub expiry_source: Option<String>,
    pub notes: Vec<String>,
}

impl TokenMetadata {
    /// `TokenMetadata.to_dict()`.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut map = Map::new();
        map.insert(
            "platform".to_string(),
            Value::String(self.platform.as_str().to_string()),
        );
        map.insert(
            "auth_mode".to_string(),
            Value::String(self.auth_mode.clone()),
        );
        map.insert("enabled".to_string(), Value::Bool(self.enabled));
        map.insert("configured".to_string(), Value::Bool(self.configured));
        map.insert(
            "has_refresh_token".to_string(),
            Value::Bool(self.has_refresh_token),
        );
        map.insert(
            "expires_at".to_string(),
            self.expires_at.map_or(Value::Null, number),
        );
        map.insert(
            "expires_at_iso".to_string(),
            iso_timestamp(self.expires_at).map_or(Value::Null, Value::String),
        );
        map.insert(
            "expiry_source".to_string(),
            self.expiry_source
                .clone()
                .map_or(Value::Null, Value::String),
        );
        map.insert(
            "notes".to_string(),
            Value::Array(self.notes.iter().cloned().map(Value::String).collect()),
        );
        Value::Object(map)
    }
}

fn number(value: f64) -> Value {
    serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number)
}

/// `token_state.token_metadata()`.
#[must_use]
pub fn token_metadata(provider: &ProviderFacts) -> TokenMetadata {
    let facts = &provider.facts;
    let enabled = facts.enabled;
    let base = |auth_mode: &str| TokenMetadata {
        platform: provider.platform,
        auth_mode: auth_mode.to_string(),
        enabled,
        configured: false,
        has_refresh_token: false,
        expires_at: None,
        expiry_source: None,
        notes: Vec::new(),
    };

    match provider.platform {
        Platform::YouTube => TokenMetadata {
            configured: facts.token_document_present || facts.token_file_exists,
            has_refresh_token: facts.has_refresh_token,
            expires_at: facts.expires_at,
            expiry_source: facts.expires_at.map(|_| "token_document".to_string()),
            ..base("oauth")
        },
        Platform::X => {
            let mode = if facts.auth_mode.is_empty() {
                "cookies".to_string()
            } else {
                facts.auth_mode.clone()
            };
            let configured = if mode == "api_v2" {
                facts.bearer_token
            } else {
                facts.cookies_file_exists
            };
            // `token_metadata` appends a note here but never passes it to
            // `TokenMetadata`, so the shipped payload carries `notes: []`. The
            // dead append is reproduced rather than "fixed": the payload is the
            // contract.
            TokenMetadata {
                auth_mode: if matches!(mode.as_str(), "cookies" | "api_v2") {
                    mode
                } else {
                    "cookies".to_string()
                },
                configured,
                ..base("cookies")
            }
        }
        Platform::Instagram => {
            let graph_token = facts.graph_access_token;
            let mode = if facts.auth_mode == "graph_api" && graph_token {
                "graph_api"
            } else {
                "session"
            };
            let configured = if mode == "graph_api" {
                graph_token
            } else {
                facts.session_file_exists
            };
            TokenMetadata {
                auth_mode: mode.to_string(),
                configured,
                ..base("session")
            }
        }
        Platform::TikTok => {
            if facts.provider.trim().to_lowercase() == "zernio" {
                return TokenMetadata {
                    auth_mode: "api_key".to_string(),
                    configured: !facts.aggregator_account_id.is_empty(),
                    notes: vec![
                        "aggregator publish route: a vendor API key authenticates the post \
                         (no TikTok OAuth token on this machine)"
                            .to_string(),
                    ],
                    ..base("api_key")
                };
            }
            let has_posting = facts.client_key && (facts.access_token || facts.refresh_token);
            if has_posting {
                return TokenMetadata {
                    auth_mode: "content_posting_api".to_string(),
                    configured: true,
                    has_refresh_token: facts.refresh_token,
                    ..base("content_posting_api")
                };
            }
            let configured =
                facts.username || facts.cookies_from_browser || facts.cookies_file_exists;
            if matches!(facts.publish_mode.as_str(), "browser" | "browser_only") {
                return TokenMetadata {
                    auth_mode: "session".to_string(),
                    configured,
                    notes: vec![
                        "browser publish mode: the logged-in TikTok Studio web session is the \
                         credential (no OAuth token)"
                            .to_string(),
                    ],
                    ..base("session")
                };
            }
            TokenMetadata {
                auth_mode: "source_only".to_string(),
                configured,
                notes: vec![
                    "source-only mode: downloads work, posting is not configured".to_string(),
                ],
                ..base("source_only")
            }
        }
        Platform::Threads => TokenMetadata {
            configured: facts.graph_access_token && facts.threads_user_id,
            has_refresh_token: facts.graph_access_token,
            ..base("oauth")
        },
        Platform::Facebook => TokenMetadata {
            configured: facts.page_id && facts.page_access_token,
            notes: vec![
                "Page-scoped token: no refresh path — re-run `xpst auth facebook` when it expires"
                    .to_string(),
            ],
            ..base("oauth")
        },
        Platform::Messenger => TokenMetadata {
            configured: facts.page_access_token,
            notes: vec!["static Page token: no refresh path".to_string()],
            ..base("oauth")
        },
        Platform::Bluesky => TokenMetadata {
            configured: facts.identifier,
            notes: vec![
                "app password (AT Protocol): no OAuth refresh; the adapter re-authenticates with \
                 the stored app password"
                    .to_string(),
            ],
            ..base("app_password")
        },
        Platform::Rumble | Platform::Local => base("unknown"),
    }
}

/// The derived badge fields for one platform.
#[derive(Debug, Clone, PartialEq)]
pub struct BadgeTruth {
    pub token_state: TokenState,
    pub badge: Badge,
    pub reason: String,
    pub action: Option<String>,
    pub checked_at: Option<f64>,
    pub check_age_seconds: Option<f64>,
    pub expires_at: Option<f64>,
    pub expires_in_seconds: Option<f64>,
    pub auto_refresh: bool,
}

impl BadgeTruth {
    /// The fields `attach_badge_truth` copies onto a canonical entry.
    #[must_use]
    pub fn fields(&self, metadata: &TokenMetadata) -> Vec<(&'static str, Value)> {
        vec![
            (
                "token_state",
                Value::String(self.token_state.as_str().to_string()),
            ),
            ("badge", Value::String(self.badge.as_str().to_string())),
            (
                "badge_label",
                Value::String(badge_label(self.badge).to_string()),
            ),
            ("badge_reason", Value::String(self.reason.clone())),
            (
                "badge_action",
                self.action.clone().map_or(Value::Null, Value::String),
            ),
            ("checked_at", self.checked_at.map_or(Value::Null, number)),
            (
                "checked_at_iso",
                iso_timestamp(self.checked_at).map_or(Value::Null, Value::String),
            ),
            (
                "check_age_seconds",
                self.check_age_seconds.map_or(Value::Null, number),
            ),
            ("expires_at", self.expires_at.map_or(Value::Null, number)),
            (
                "expires_at_iso",
                iso_timestamp(self.expires_at).map_or(Value::Null, Value::String),
            ),
            (
                "expires_in_seconds",
                self.expires_in_seconds.map_or(Value::Null, number),
            ),
            ("auto_refresh", Value::Bool(self.auto_refresh)),
            ("token_metadata", metadata.to_value()),
        ]
    }
}

/// A persisted refresh attempt (`token_refresh`'s report entry).
///
/// The caller filters it by [`REFRESH_FAILURE_TTL_SECONDS`] before passing it:
/// a failure older than the TTL no longer keeps the badge red.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RefreshRecord {
    /// The attempt failed.
    pub failed: bool,
    /// How many attempts were made.
    pub attempts: u32,
    /// The provider's error text, when it reported one.
    pub error: Option<String>,
}

/// `derive_token_state()`.
///
/// `entry` is the canonical provider entry (or the raw live entry — the fields
/// it reads are the same ones both carry).
#[must_use]
pub fn derive_token_state(
    platform: Platform,
    entry: &Value,
    metadata: &TokenMetadata,
    now: f64,
    refresh: Option<&RefreshRecord>,
) -> BadgeTruth {
    let auth_mode = entry
        .get("auth_mode")
        .and_then(Value::as_str)
        .filter(|mode| !mode.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| {
            if metadata.auth_mode.is_empty() {
                "unknown".to_string()
            } else {
                metadata.auth_mode.clone()
            }
        });
    let enabled = entry
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(metadata.enabled);

    let checked_at = entry.get("checked_at").and_then(Value::as_f64).or_else(|| {
        // A probe handed to us without a timestamp was taken as part of the
        // read consuming it; stamp it so the UI can age it out later.
        if entry
            .get("live_checked")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            Some(now)
        } else {
            None
        }
    });
    let age = checked_at.map(|checked_at| (now - checked_at).max(0.0));

    let has_refresh = metadata.has_refresh_token;
    let expires_at = metadata.expires_at;
    let expires_in = expires_at.map(|expires_at| expires_at - now);
    let error_text = entry
        .get("error")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    let live_ok = entry
        .get("authenticated")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        && entry
            .get("session_valid")
            .is_none_or(|value| value.as_bool().unwrap_or(false));
    let live_checked = entry
        .get("live_checked")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let configured = metadata.configured
        || entry
            .get("credentials_stored")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        || entry
            .get("configured")
            .and_then(Value::as_bool)
            .unwrap_or(false);

    let mut action: Option<String> = None;
    let (state, reason) = if !enabled || error_text.eq_ignore_ascii_case("disabled") {
        (TokenState::Disabled, "disabled in config".to_string())
    } else if auth_mode == "source_only" {
        let mut reason =
            "source only — no posting credentials configured (TikTok dev app pending)".to_string();
        if live_checked && !live_ok && !error_text.is_empty() {
            reason = format!("{reason}; source check failed: {error_text}");
        }
        (TokenState::SourceOnly, reason)
    } else if !live_checked {
        action = Some(format!("xpst auth status --refresh --platform {platform}"));
        (
            TokenState::Unknown,
            "no live check has run — a stored credential is not proof of a working session"
                .to_string(),
        )
    } else if age.is_some_and(|age| age > MAX_CHECK_AGE_SECONDS) {
        action = Some(format!("xpst auth status --refresh --platform {platform}"));
        (
            TokenState::Unknown,
            format!(
                "last live check was {} ago — older than the {} freshness window",
                humanize_seconds(age).unwrap_or_default(),
                humanize_seconds(Some(MAX_CHECK_AGE_SECONDS)).unwrap_or_default()
            ),
        )
    } else if live_ok {
        if has_refresh {
            let reason = match expires_in {
                Some(expires_in) if expires_in <= 0.0 => {
                    "live check passed (the token was refreshed during the check)".to_string()
                }
                Some(expires_in) if expires_in <= EXPIRING_WINDOW_SECONDS => format!(
                    "live check passed; access token refreshes automatically in the background \
                     (current one expires in {})",
                    humanize_seconds(Some(expires_in)).unwrap_or_default()
                ),
                _ => "live check passed; access token refreshes automatically in the background"
                    .to_string(),
            };
            (TokenState::Valid, reason)
        } else if expires_at.is_none() && auth_mode == "session" {
            (
                TokenState::Valid,
                "live check passed; the stored session is the credential (there is no token \
                 expiry to report)"
                    .to_string(),
            )
        } else if expires_at.is_none() {
            (
                TokenState::Valid,
                "live check passed; token expiry not reported by this provider".to_string(),
            )
        } else if expires_in.is_some_and(|expires_in| {
            expires_in <= EXPIRING_WINDOW_SECONDS.max(REAUTH_WINDOW_SECONDS)
        }) {
            action = Some(format!("xpst connect {platform}"));
            (
                TokenState::Expiring,
                format!(
                    "live check passed but the token expires in {} and there is no automatic \
                     refresh",
                    humanize_seconds(expires_in).unwrap_or_default()
                ),
            )
        } else {
            (TokenState::Valid, "live check passed".to_string())
        }
    } else {
        let failure = if error_text.is_empty() {
            "live check failed".to_string()
        } else {
            error_text.clone()
        };
        if !configured {
            action = Some(format!("xpst connect {platform}"));
            (
                TokenState::Unconfigured,
                format!("no credentials configured — {failure}"),
            )
        } else if entry.get("probe_class").and_then(Value::as_str) == Some("unverified") {
            action = Some("xpst health".to_string());
            (
                TokenState::Unknown,
                format!(
                    "live check could not verify the account (unverified probe result, not a \
                     proven expiry) — {failure}"
                ),
            )
        } else if has_refresh && !refresh.is_some_and(|record| record.failed) {
            (
                TokenState::ExpiredRefreshable,
                format!(
                    "access token expired ({failure}); a refresh token is available and xPST will \
                     refresh automatically"
                ),
            )
        } else if refresh.is_some_and(|record| record.failed) {
            action = Some(format!("xpst connect {platform}"));
            let record = refresh.expect("checked above");
            let detail = record
                .error
                .as_ref()
                .map(|error| format!(": {error}"))
                .unwrap_or_default();
            let reason = if record.attempts > 0 {
                format!(
                    "automatic refresh failed after {} attempt(s){detail}",
                    record.attempts
                )
            } else {
                format!("automatic refresh failed{detail}")
            };
            (TokenState::ExpiredUnrefreshable, reason)
        } else {
            action = Some(format!("xpst connect {platform}"));
            (
                TokenState::ExpiredUnrefreshable,
                format!("{failure}; no refresh path for this auth mode"),
            )
        }
    };

    BadgeTruth {
        token_state: state,
        badge: state.badge(),
        reason,
        action,
        checked_at,
        check_age_seconds: age,
        expires_at,
        expires_in_seconds: expires_in,
        auto_refresh: has_refresh,
    }
}

/// `attach_badge_truth()`: stamp `checked_at` and attach the badge fields.
///
/// Platforms outside [`PLATFORM_ORDER`] (the local source, Rumble) keep their
/// own truth and carry no badge.
pub fn attach_badge_truth(
    canonical: &mut [(Platform, Value)],
    facts: &[ProviderFacts],
    now: f64,
    refresh: &dyn Fn(Platform) -> Option<RefreshRecord>,
) {
    for platform in PLATFORM_ORDER {
        let Some(provider) = facts.iter().find(|item| item.platform == platform) else {
            continue;
        };
        let Some((_, entry)) = canonical.iter_mut().find(|(name, _)| *name == platform) else {
            continue;
        };
        // The stamp goes on a COPY: `attach_badge_truth` derives from a
        // `checked_at`-stamped clone but writes the fields back in
        // `BADGE_FIELD_NAMES` order, so `checked_at` lands after `badge_action`
        // rather than at the end of the pre-badge entry.
        let mut stamped = entry.clone();
        if stamped
            .get("live_checked")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            if let Some(map) = stamped.as_object_mut() {
                map.insert("checked_at".to_string(), number(now));
            }
        }
        let metadata = token_metadata(provider);
        let record = refresh(platform);
        let truth = derive_token_state(platform, &stamped, &metadata, now, record.as_ref());
        if let Some(map) = entry.as_object_mut() {
            for (key, value) in truth.fields(&metadata) {
                map.insert(key.to_string(), value);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn provider(platform: Platform) -> ProviderFacts {
        ProviderFacts::no_credentials(platform)
    }

    #[test]
    fn a_stored_credential_is_never_green() {
        let mut facts = provider(Platform::YouTube);
        facts.facts.token_file_exists = true;
        let metadata = token_metadata(&facts);
        let entry = serde_json::json!({
            "auth_mode": "oauth",
            "authenticated": false,
            "session_valid": false,
            "live_checked": true,
            "error": "token expired",
            "enabled": true,
        });
        let truth = derive_token_state(Platform::YouTube, &entry, &metadata, 1_000.0, None);
        assert_eq!(truth.token_state, TokenState::ExpiredUnrefreshable);
        assert_eq!(truth.badge, Badge::NeedsReauth);
        assert!(!truth.badge.is_green());
    }

    #[test]
    fn a_disabled_platform_is_disabled_not_broken() {
        let mut facts = provider(Platform::Bluesky);
        facts.facts.enabled = false;
        let metadata = token_metadata(&facts);
        let entry = serde_json::json!({
            "auth_mode": "app_password",
            "authenticated": false,
            "session_valid": false,
            "live_checked": true,
            "error": "disabled",
            "enabled": false,
        });
        let truth = derive_token_state(Platform::Bluesky, &entry, &metadata, 1_000.0, None);
        assert_eq!(truth.token_state, TokenState::Disabled);
        assert_eq!(truth.reason, "disabled in config");
    }

    #[test]
    fn a_source_only_route_says_so() {
        let facts = provider(Platform::TikTok);
        let metadata = token_metadata(&facts);
        let entry = serde_json::json!({
            "auth_mode": "source_only",
            "authenticated": false,
            "session_valid": false,
            "live_checked": true,
            "error": "No TikTok cookies available",
            "enabled": true,
        });
        let truth = derive_token_state(Platform::TikTok, &entry, &metadata, 1_000.0, None);
        assert_eq!(truth.token_state, TokenState::SourceOnly);
        assert!(truth.reason.contains("source check failed"));
    }

    #[test]
    fn humanize_matches_the_python_formatter() {
        assert_eq!(humanize_seconds(Some(45.0)).unwrap(), "45s");
        assert_eq!(humanize_seconds(Some(90.0)).unwrap(), "1m");
        assert_eq!(humanize_seconds(Some(3600.0)).unwrap(), "1h");
        assert_eq!(humanize_seconds(Some(3900.0)).unwrap(), "1h 5m");
        assert_eq!(humanize_seconds(Some(86_400.0)).unwrap(), "1d");
        assert_eq!(humanize_seconds(Some(90_000.0)).unwrap(), "1d 1h");
    }
}
