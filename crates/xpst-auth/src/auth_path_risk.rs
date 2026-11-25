//! Ban-risk advisories for platforms driven by an unofficial auth path
//! (`xpst.auth_path_risk`).
//!
//! Two shipped integrations are not a provider's official API: Instagram's
//! `session` mode (instagrapi drives the private mobile API) and X's `cookies`
//! mode (twikit replays the logged-in web session). Neither is hidden in the
//! code, but nothing at the *decision point* said "this is the ban-risky path",
//! and readiness reporting called such an account simply "ready".
//!
//! The rule that keeps this honest: **an advisory never changes the verdict.** A
//! platform on an unofficial path still posts today, so it is reported (never
//! hidden) and kept out of `issues`, which is what `all_clear` and the exit code
//! read. A platform disabled in config is a deliberate pause, not a warning.
//!
//! Severity is two-tier on purpose: `high` means the provider's terms forbid the
//! path outright; `advisory` means unofficial but xPST's documented default.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{Map, Value};

use crate::state::{BanRisk, Platform, Severity};

/// The operator-choice record's file name.
pub const ACK_FILE_NAME: &str = "ban_risk_acknowledgements.json";
/// The record's schema version.
pub const ACK_FILE_VERSION: u32 = 1;

/// One static description of a non-official integration.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UnofficialAuthPath {
    pub platform: Platform,
    pub display_name: &'static str,
    pub auth_mode: &'static str,
    pub ban_risk: BanRisk,
    pub severity: Severity,
    pub official_mode: &'static str,
    pub official_label: &'static str,
    pub switch_command: &'static str,
    pub docs_url: &'static str,
    pub consequence: &'static str,
    pub requirement: &'static str,
}

/// The registry. Adding a platform whose integration automates a web/mobile
/// client outside the provider's API means adding an entry here.
pub const UNOFFICIAL_AUTH_PATHS: [UnofficialAuthPath; 2] = [
    UnofficialAuthPath {
        platform: Platform::Instagram,
        display_name: "Instagram",
        auth_mode: "session",
        ban_risk: BanRisk::UnofficialPrivateApi,
        severity: Severity::High,
        official_mode: "graph_api",
        official_label: "Meta Graph API",
        switch_command: "xpst connect instagram",
        docs_url: "docs/setup-instagram.md",
        consequence: "Instagram's terms forbid automated access outside the official API, and \
                      enforcement is account action-blocks, challenges and suspension — not a \
                      polite error.",
        requirement: "an Instagram Creator/Business account linked to a Facebook Page",
    },
    UnofficialAuthPath {
        platform: Platform::X,
        display_name: "X",
        auth_mode: "cookies",
        ban_risk: BanRisk::UnofficialWebClient,
        severity: Severity::Advisory,
        official_mode: "api_v2",
        official_label: "X API v2",
        switch_command: "xpst connect x",
        docs_url: "docs/setup-x.md",
        consequence: "twikit replays the logged-in web session: X rate-limits and can suspend \
                      automated clients that use it at volume.",
        requirement: "X API v2 access (free tier: 17 posts/day)",
    },
];

/// The registry entry for a platform, when it has one.
#[must_use]
pub fn unofficial_path(platform: Platform) -> Option<&'static UnofficialAuthPath> {
    UNOFFICIAL_AUTH_PATHS
        .iter()
        .find(|entry| entry.platform == platform)
}

/// The advisory for one platform, as this config currently stands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthPathRisk {
    pub platform: Platform,
    pub display_name: &'static str,
    pub auth_mode: &'static str,
    pub ban_risk: BanRisk,
    pub severity: Severity,
    pub message: String,
    pub official_mode: &'static str,
    pub official_label: &'static str,
    pub switch_command: &'static str,
    pub docs_url: &'static str,
    pub consequence: &'static str,
    pub requirement: &'static str,
    pub enabled: bool,
    pub acknowledged: bool,
    pub acknowledgement: Option<Value>,
}

impl AuthPathRisk {
    /// `AuthPathRisk.to_dict()`, in Python's key order.
    #[must_use]
    pub fn to_value(&self) -> Value {
        let mut map = Map::new();
        map.insert(
            "platform".to_string(),
            Value::String(self.platform.as_str().to_string()),
        );
        map.insert(
            "display_name".to_string(),
            Value::String(self.display_name.to_string()),
        );
        map.insert(
            "auth_mode".to_string(),
            Value::String(self.auth_mode.to_string()),
        );
        map.insert(
            "ban_risk".to_string(),
            Value::String(self.ban_risk.as_str().to_string()),
        );
        map.insert(
            "severity".to_string(),
            Value::String(self.severity.as_str().to_string()),
        );
        map.insert("message".to_string(), Value::String(self.message.clone()));
        map.insert(
            "official_mode".to_string(),
            Value::String(self.official_mode.to_string()),
        );
        map.insert(
            "official_label".to_string(),
            Value::String(self.official_label.to_string()),
        );
        map.insert(
            "switch_command".to_string(),
            Value::String(self.switch_command.to_string()),
        );
        map.insert(
            "docs_url".to_string(),
            Value::String(self.docs_url.to_string()),
        );
        map.insert(
            "consequence".to_string(),
            Value::String(self.consequence.to_string()),
        );
        map.insert(
            "requirement".to_string(),
            Value::String(self.requirement.to_string()),
        );
        map.insert("enabled".to_string(), Value::Bool(self.enabled));
        map.insert("acknowledged".to_string(), Value::Bool(self.acknowledged));
        map.insert(
            "acknowledgement".to_string(),
            self.acknowledgement.clone().unwrap_or(Value::Null),
        );
        let mut advisory = Map::new();
        advisory.insert(
            "code".to_string(),
            Value::String(self.ban_risk.advisory_code().to_string()),
        );
        advisory.insert("message".to_string(), Value::String(self.message.clone()));
        map.insert("advisory".to_string(), Value::Object(advisory));
        Value::Object(map)
    }
}

/// `auth_path_risk()`: the advisory for `platform`, or `None` when the
/// configured mode is the official one (or the platform has no registry entry).
///
/// `acknowledgement` is the record that covers this platform's CURRENT auth
/// mode — an operator who accepted `session` has not thereby accepted some other
/// unofficial mode a later config edit introduced.
#[must_use]
pub fn auth_path_risk(
    platform: Platform,
    configured_auth_mode: &str,
    enabled: bool,
    acknowledgement: Option<&Value>,
) -> Option<AuthPathRisk> {
    let entry = unofficial_path(platform)?;
    if configured_auth_mode.trim().to_lowercase() != entry.auth_mode {
        return None;
    }
    let ack_at = acknowledgement
        .and_then(|record| record.get("acknowledged_at"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let tail = if ack_at.is_empty() {
        " Nothing has recorded that the operator accepted this risk.".to_string()
    } else {
        format!(" The operator acknowledged this path on {ack_at}.")
    };
    let message = format!(
        "{} is on its unofficial auth path (auth_mode: '{}', ban_risk: {}): {} The ban-safe path \
         is {} (auth_mode: '{}'); it needs {}. Switch with '{}'.{}",
        entry.display_name,
        entry.auth_mode,
        entry.ban_risk.as_str(),
        entry.consequence,
        entry.official_label,
        entry.official_mode,
        entry.requirement,
        entry.switch_command,
        tail
    );
    Some(AuthPathRisk {
        platform,
        display_name: entry.display_name,
        auth_mode: entry.auth_mode,
        ban_risk: entry.ban_risk,
        severity: entry.severity,
        message,
        official_mode: entry.official_mode,
        official_label: entry.official_label,
        switch_command: entry.switch_command,
        docs_url: entry.docs_url,
        consequence: entry.consequence,
        requirement: entry.requirement,
        enabled,
        acknowledged: acknowledgement.is_some(),
        acknowledgement: acknowledgement.cloned(),
    })
}

/// `read_acknowledgements()`: the operator-choice record, keyed by platform.
///
/// A missing file is the normal state; a corrupt one reads as empty rather than
/// aborting a doctor run — the advisory simply reads as unacknowledged again,
/// which is the safe direction.
#[must_use]
pub fn read_acknowledgements(config_dir: &Path) -> BTreeMap<String, Value> {
    let path = config_dir.join(ACK_FILE_NAME);
    let Ok(text) = std::fs::read_to_string(&path) else {
        return BTreeMap::new();
    };
    let Ok(payload) = serde_json::from_str::<Value>(&text) else {
        return BTreeMap::new();
    };
    let Some(entries) = payload.get("acknowledgements").and_then(Value::as_object) else {
        return BTreeMap::new();
    };
    entries
        .iter()
        .filter(|(_, entry)| entry.is_object())
        .map(|(name, entry)| (name.clone(), entry.clone()))
        .collect()
}

/// `acknowledgement_for()`: the record that covers this platform's current mode.
#[must_use]
pub fn acknowledgement_for(
    records: &BTreeMap<String, Value>,
    platform: Platform,
    configured_auth_mode: &str,
) -> Option<Value> {
    let entry = records.get(platform.as_str())?;
    let recorded = entry
        .get("auth_mode")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    if recorded != configured_auth_mode.trim().to_lowercase() {
        return None;
    }
    Some(entry.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_unofficial_mode_raises_a_risk() {
        assert!(auth_path_risk(Platform::Instagram, "session", true, None).is_some());
        assert!(auth_path_risk(Platform::Instagram, "graph_api", true, None).is_none());
        assert!(auth_path_risk(Platform::X, "cookies", true, None).is_some());
        assert!(auth_path_risk(Platform::X, "api_v2", true, None).is_none());
        assert!(auth_path_risk(Platform::YouTube, "oauth", true, None).is_none());
    }

    #[test]
    fn the_message_names_the_ban_safe_path_and_the_acknowledgement() {
        let risk = auth_path_risk(Platform::X, "cookies", true, None).unwrap();
        assert!(risk.message.contains("X API v2"));
        assert!(risk
            .message
            .contains("Nothing has recorded that the operator accepted this risk."));
        let ack = serde_json::json!({"auth_mode": "cookies", "acknowledged_at": "2026-01-01T00:00:00+0000"});
        let risk = auth_path_risk(Platform::X, "cookies", true, Some(&ack)).unwrap();
        assert!(risk.acknowledged);
        assert!(risk
            .message
            .contains("The operator acknowledged this path on 2026-01-01T00:00:00+0000."));
    }

    #[test]
    fn an_acknowledgement_is_bound_to_the_mode_it_was_given_for() {
        let mut records = BTreeMap::new();
        records.insert(
            "instagram".to_string(),
            serde_json::json!({"auth_mode": "session"}),
        );
        assert!(acknowledgement_for(&records, Platform::Instagram, "session").is_some());
        assert!(acknowledgement_for(&records, Platform::Instagram, "graph_api").is_none());
    }
}
