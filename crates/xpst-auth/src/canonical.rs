//! The canonical provider-status payload (`provider_truth.build_canonical_status`).
//!
//! This is the ONE document every surface renders: `xpst doctor`, `xpst auth
//! status`, the MCP status tools and the dashboard all read it, so a fact about
//! an account has exactly one implementation. The truth table in [`crate::table`]
//! decides the states; this module renders them in Python's key order and adds
//! the compatibility fields (`authenticated`, `session_valid`, `live_checked`,
//! `error`) that older clients still read.
//!
//! Two rules the port keeps, because both were bugs in the shipped engine:
//!
//! * **"Not probed" is not "invalid".** A role whose raw result carries no live
//!   fact reports `authenticated: null`, never `false` — a surface must not
//!   assert a fact it never checked.
//! * **A provider's `authenticated` flag describes its PRIMARY role.** For a
//!   source-only provider (TikTok) that role is the download source, which says
//!   nothing about the uploader; every posting decision goes through
//!   [`posting_truth`] instead.
//!
//! Nothing here performs I/O: the caller supplies the credential facts and the
//! probe verdicts, and `now` is an argument.

use serde_json::{Map, Value};

use crate::facts::{CredentialFacts, PlatformInput, ProbeResult};
use crate::state::{Platform, ProviderState, Role};
use crate::table;

/// The config-shaped facts for one provider.
#[derive(Debug, Clone)]
pub struct ProviderFacts {
    /// Which provider this row describes.
    pub platform: Platform,
    /// Credential-presence facts (including whether the account is enabled).
    pub facts: CredentialFacts,
    /// Instagram's last-verified account type (`""` when unknown).
    pub account_type: String,
    /// Where that verdict came from (`""` when unknown).
    pub account_type_source: String,
}

impl ProviderFacts {
    /// An enabled provider with no credentials: the shape a fresh install has.
    #[must_use]
    pub fn no_credentials(platform: Platform) -> Self {
        Self {
            platform,
            facts: CredentialFacts {
                enabled: true,
                ..CredentialFacts::default()
            },
            account_type: String::new(),
            account_type_source: String::new(),
        }
    }
}

/// One provider's live probe results (`collect_live_auth_status_async`'s `raw`).
#[derive(Debug, Clone, Default)]
pub struct ProviderProbe {
    /// The provider-level verdict (the compat role's, for TikTok).
    pub live: Option<ProbeResult>,
    /// TikTok's download-side verdict, independent of posting.
    pub source_live: Option<ProbeResult>,
    /// TikTok's posting-side verdict.
    pub destination_live: Option<ProbeResult>,
    /// Credential-file age in whole days (`_session_health`).
    pub session_age_days: Option<i64>,
    /// When the probe ran, epoch seconds.
    pub checked_at: Option<f64>,
    /// A recorded refresh attempt failed inside its TTL.
    pub refresh_failed: bool,
}

impl ProviderProbe {
    fn input(&self, facts: &ProviderFacts) -> PlatformInput {
        PlatformInput {
            platform: facts.platform,
            facts: facts.facts.clone(),
            live: self.live.clone(),
            source_live: self.source_live.clone(),
            destination_live: self.destination_live.clone(),
            checked_at: self.checked_at,
            refresh_failed: self.refresh_failed,
        }
    }
}

/// The posting verdict for one canonical entry (`provider_truth.posting_truth`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostingTruth {
    /// The provider has a role a post can be delivered to.
    pub posting_destination: bool,
    /// That role's wire name.
    pub posting_role: Option<&'static str>,
    /// That role's state.
    pub posting_state: Option<String>,
    /// That role's error, verbatim.
    pub posting_error: Option<String>,
    /// The posting role is ready.
    pub can_post: bool,
    /// The provider is wired up as a download source, not a destination.
    pub source_only: bool,
    /// One truthful sentence explaining why it cannot be posted to.
    pub posting_note: Option<String>,
}

/// `_posting_note()`.
fn posting_note(
    display_name: &str,
    source_only: bool,
    state: Option<&str>,
    error: Option<&str>,
) -> Option<String> {
    if !source_only {
        return None;
    }
    let mut note = format!(
        "{display_name} is a source only: xPST downloads from it, and it cannot be used as a \
         posting destination."
    );
    if let Some(error) = error {
        note.push_str(&format!(" The uploader reported: {error}"));
    } else if state.is_none() {
        note.push_str(" No destination role is configured for it.");
    }
    Some(note)
}

/// `posting_truth()` for one already-built canonical entry.
///
/// Reads `role_status` (falling back to `roles`) exactly as the Python function
/// does, so a cached payload from an older build is re-derived rather than
/// trusted.
#[must_use]
pub fn posting_truth(entry: &Value) -> PostingTruth {
    let statuses = entry
        .get("role_status")
        .or_else(|| entry.get("roles"))
        .and_then(Value::as_object);

    let mut role_name: Option<&'static str> = None;
    let mut role: Option<&Map<String, Value>> = None;
    for candidate in [Role::VideoDestination, Role::Messaging] {
        let status = statuses
            .and_then(|map| map.get(candidate.as_str()))
            .and_then(Value::as_object);
        if status.is_some_and(|status| !status.is_empty()) {
            role_name = Some(candidate.as_str());
            role = status;
            break;
        }
    }

    let state = role
        .and_then(|role| role.get("state"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let entry_mode = entry
        .get("auth_mode")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let role_mode = role
        .and_then(|role| role.get("auth_mode"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let can_post = state.as_deref() == Some(ProviderState::Ready.as_str());
    let source_only = role_name.is_some()
        && !can_post
        && (entry_mode == "source_only" || role_mode == "source_only");
    let error = role
        .and_then(|role| role.get("error"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let display_name = entry
        .get("display_name")
        .or_else(|| entry.get("name"))
        .and_then(Value::as_str)
        .unwrap_or("This platform");

    PostingTruth {
        posting_destination: role_name.is_some(),
        posting_role: role_name,
        posting_state: state.clone(),
        posting_error: error.clone(),
        can_post,
        source_only,
        posting_note: posting_note(
            display_name,
            source_only,
            state.as_deref(),
            error.as_deref(),
        ),
    }
}

/// `with_posting_truth()`: the flat posting fields every surface reads.
///
/// Idempotent — re-applying it recomputes the same values instead of trusting
/// the ones already present.
#[must_use]
pub fn with_posting_truth(entry: &Value) -> Value {
    let truth = posting_truth(entry);
    let mut enriched = entry.clone();
    if let Some(map) = enriched.as_object_mut() {
        map.insert(
            "posting_destination".to_string(),
            Value::Bool(truth.posting_destination),
        );
        map.insert(
            "posting_role".to_string(),
            truth
                .posting_role
                .map_or(Value::Null, |role| Value::String(role.to_string())),
        );
        map.insert(
            "posting_state".to_string(),
            truth
                .posting_state
                .clone()
                .map_or(Value::Null, Value::String),
        );
        map.insert(
            "posting_error".to_string(),
            truth.posting_error.map_or(Value::Null, Value::String),
        );
        map.insert("can_post".to_string(), Value::Bool(truth.can_post));
        map.insert("source_only".to_string(), Value::Bool(truth.source_only));
        map.insert(
            "posting_note".to_string(),
            truth.posting_note.map_or(Value::Null, Value::String),
        );
    }
    enriched
}

/// The live facts a raw result must carry to count as "probed"
/// (`provider_truth.LIVE_FACT_FIELDS`).
fn probed(probe: Option<&ProbeResult>) -> bool {
    probe.is_some()
}

/// `_status_for_role()`: one role's rendered status.
fn role_status(facts: &ProviderFacts, input: &PlatformInput, role: Role, enabled: bool) -> Value {
    let probe = table::probe_for(input, role);
    let state = table::role_state(input, role);
    let auth_mode = table::resolve_auth_mode(facts.platform, &facts.facts, probe);
    let is_probed = probed(probe);
    let mut map = Map::new();
    map.insert(
        "platform".to_string(),
        Value::String(facts.platform.as_str().to_string()),
    );
    map.insert("role".to_string(), Value::String(role.as_str().to_string()));
    map.insert(
        "state".to_string(),
        Value::String(state.as_str().to_string()),
    );
    map.insert(
        "ready".to_string(),
        Value::Bool(state == ProviderState::Ready),
    );
    map.insert("enabled".to_string(), Value::Bool(enabled));
    map.insert(
        "authenticated".to_string(),
        if is_probed {
            Value::Bool(probe.is_some_and(|probe| probe.authenticated))
        } else {
            Value::Null
        },
    );
    map.insert(
        "session_valid".to_string(),
        if is_probed {
            Value::Bool(probe.is_some_and(|probe| probe.session_valid.unwrap_or(false)))
        } else {
            Value::Null
        },
    );
    map.insert("auth_mode".to_string(), Value::String(auth_mode));
    map.insert(
        "live_checked".to_string(),
        if is_probed {
            Value::Bool(probe.is_some_and(|probe| probe.live_checked))
        } else {
            Value::Null
        },
    );
    map.insert(
        "error".to_string(),
        probe
            .and_then(|probe| probe.error.clone())
            .map_or(Value::Null, Value::String),
    );
    map.insert(
        "details".to_string(),
        probe
            .map(|probe| probe.raw_details.clone())
            .filter(Value::is_object)
            .unwrap_or_else(|| Value::Object(Map::new())),
    );
    Value::Object(map)
}

/// `build_canonical_status()`: the canonical per-provider mapping.
///
/// Returns entries in `Platform::ALL` order (the order Python's
/// `SUPPORTED_PROVIDERS` declares), each already carrying its posting truth.
/// Providers with no probe entry still get a row — "no live probe has run" is a
/// state, not an omission.
#[must_use]
pub fn build_canonical_status(
    facts: &[ProviderFacts],
    probes: &std::collections::BTreeMap<Platform, ProviderProbe>,
) -> Vec<(Platform, Value)> {
    let mut out = Vec::new();
    for provider in facts {
        let empty = ProviderProbe::default();
        let probe = probes.get(&provider.platform).unwrap_or(&empty);
        let input = probe.input(provider);
        let enabled = provider.facts.enabled;

        let statuses: Vec<(Role, Value)> = provider
            .platform
            .roles()
            .iter()
            .map(|role| (*role, role_status(provider, &input, *role, enabled)))
            .collect();
        let states: Vec<(Role, ProviderState)> = statuses
            .iter()
            .map(|(role, value)| {
                let state = value
                    .get("state")
                    .and_then(Value::as_str)
                    .and_then(parse_state)
                    .unwrap_or(ProviderState::Unconfigured);
                (*role, state)
            })
            .collect();
        let aggregate = table::aggregate(provider.platform, &states);
        let compat_role = table::compat_role(&input);
        let compat = statuses
            .iter()
            .find(|(role, _)| *role == compat_role)
            .map(|(_, value)| value.clone())
            .unwrap_or_else(|| Value::Object(Map::new()));

        let live_flags: Vec<Option<bool>> = states
            .iter()
            .map(|(role, _)| table::probe_for(&input, *role).map(|probe| probe.live_checked))
            .collect();
        let live_checked = if live_flags.contains(&Some(true)) {
            Value::Bool(true)
        } else if live_flags.iter().all(Option::is_none) {
            Value::Null
        } else {
            Value::Bool(false)
        };

        // The compat fields describe the primary (or, for TikTok source-only,
        // the source) role. When that role has no error but the provider is
        // nonetheless not usable, surface the role error that explains it —
        // otherwise the payload reads "state: unconfigured, authenticated: true,
        // error: null", a status that contradicts itself.
        let mut error = compat.get("error").cloned().unwrap_or(Value::Null);
        if error.is_null()
            && aggregate != ProviderState::Ready
            && aggregate != ProviderState::Disabled
        {
            error = states
                .iter()
                .filter_map(|(role, _)| {
                    table::probe_for(&input, *role).and_then(|probe| probe.error.clone())
                })
                .next()
                .map_or(Value::Null, Value::String);
        }

        let compat_details = compat
            .get("details")
            .cloned()
            .filter(Value::is_object)
            .unwrap_or_else(|| Value::Object(Map::new()));
        let detail_field = |key: &str| compat_details.get(key).cloned().unwrap_or(Value::Null);

        let mut role_status_map = Map::new();
        let mut role_states_map = Map::new();
        for (role, value) in &statuses {
            role_status_map.insert(role.as_str().to_string(), value.clone());
            role_states_map.insert(
                role.as_str().to_string(),
                value.get("state").cloned().unwrap_or(Value::Null),
            );
        }

        let mut entry = Map::new();
        entry.insert(
            "name".to_string(),
            Value::String(provider.platform.as_str().to_string()),
        );
        entry.insert(
            "display_name".to_string(),
            Value::String(provider.platform.display_name().to_string()),
        );
        entry.insert(
            "state".to_string(),
            Value::String(aggregate.as_str().to_string()),
        );
        entry.insert(
            "state_value".to_string(),
            Value::String(aggregate.as_str().to_string()),
        );
        entry.insert("roles".to_string(), Value::Object(role_status_map.clone()));
        entry.insert("role_status".to_string(), Value::Object(role_status_map));
        entry.insert("role_states".to_string(), Value::Object(role_states_map));
        entry.insert(
            "role_names".to_string(),
            Value::Array(
                provider
                    .platform
                    .roles()
                    .iter()
                    .map(|role| Value::String(role.as_str().to_string()))
                    .collect(),
            ),
        );
        entry.insert(
            "auth_mode".to_string(),
            compat.get("auth_mode").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "authenticated".to_string(),
            compat.get("authenticated").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "session_valid".to_string(),
            compat.get("session_valid").cloned().unwrap_or(Value::Null),
        );
        entry.insert("live_checked".to_string(), live_checked);
        entry.insert("error".to_string(), error);
        entry.insert("details".to_string(), compat_details.clone());
        entry.insert("probe_class".to_string(), detail_field("probe_class"));
        entry.insert("probe_error".to_string(), detail_field("probe_error"));
        entry.insert(
            "probe_retryable".to_string(),
            detail_field("probe_retryable"),
        );
        if provider.platform == Platform::Instagram {
            // Instagram's account type is a last-VERIFIED fact stored at connect
            // or by a live probe (Meta only publishes to Business/Creator), so
            // the offline catalog carries it too.
            entry.insert(
                "account_type".to_string(),
                Value::String(provider.account_type.clone()),
            );
            entry.insert(
                "account_type_source".to_string(),
                Value::String(provider.account_type_source.clone()),
            );
        }
        entry.insert("enabled".to_string(), Value::Bool(enabled));
        entry.insert(
            "legacy_authenticated".to_string(),
            Value::Bool(
                probe
                    .live
                    .as_ref()
                    .is_some_and(|probe| probe.credentials_stored),
            ),
        );

        let value = with_posting_truth(&Value::Object(entry));
        out.push((provider.platform, value));
    }
    out
}

fn parse_state(text: &str) -> Option<ProviderState> {
    [
        ProviderState::Ready,
        ProviderState::Unconfigured,
        ProviderState::Disabled,
        ProviderState::Degraded,
        ProviderState::BlockedExternalReview,
    ]
    .into_iter()
    .find(|state| state.as_str() == text)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn facts(platform: Platform) -> ProviderFacts {
        ProviderFacts::no_credentials(platform)
    }

    #[test]
    fn an_unprobed_provider_reports_unknown_not_invalid() {
        let mut rumble = facts(Platform::Rumble);
        rumble.facts.enabled = false;
        let rows = build_canonical_status(&[rumble], &Default::default());
        let (_, entry) = &rows[0];
        assert_eq!(entry["authenticated"], Value::Null);
        assert_eq!(entry["session_valid"], Value::Null);
        assert_eq!(entry["live_checked"], Value::Null);
        assert_eq!(entry["state"], "disabled");
    }

    #[test]
    fn a_source_only_provider_is_not_a_posting_destination() {
        let mut provider = facts(Platform::TikTok);
        provider.facts.username = true;
        let mut probes = std::collections::BTreeMap::new();
        probes.insert(
            Platform::TikTok,
            ProviderProbe {
                live: Some(ProbeResult {
                    authenticated: false,
                    session_valid: Some(false),
                    live_checked: true,
                    error: Some("no cookies".to_string()),
                    ..ProbeResult::default()
                }),
                source_live: Some(ProbeResult {
                    authenticated: false,
                    session_valid: Some(false),
                    live_checked: true,
                    error: Some("no cookies".to_string()),
                    ..ProbeResult::default()
                }),
                destination_live: Some(ProbeResult {
                    authenticated: false,
                    session_valid: Some(false),
                    live_checked: true,
                    error: Some("TikTok Content Posting API is not configured".to_string()),
                    ..ProbeResult::default()
                }),
                ..ProviderProbe::default()
            },
        );
        let rows = build_canonical_status(&[provider], &probes);
        let (_, entry) = &rows[0];
        assert_eq!(entry["source_only"], true);
        assert_eq!(entry["can_post"], false);
        assert_eq!(entry["posting_role"], "video_destination");
        assert!(entry["posting_note"]
            .as_str()
            .unwrap()
            .contains("source only"));
    }

    #[test]
    fn posting_truth_reads_role_status_and_falls_back_to_roles() {
        let entry = serde_json::json!({
            "name": "x",
            "display_name": "X",
            "auth_mode": "cookies",
            "roles": {"video_destination": {"state": "ready", "auth_mode": "cookies"}},
        });
        let truth = posting_truth(&entry);
        assert!(truth.can_post);
        assert!(!truth.source_only);
        assert_eq!(truth.posting_role, Some("video_destination"));
    }
}
