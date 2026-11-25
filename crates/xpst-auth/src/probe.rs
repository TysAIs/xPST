//! The live-probe source a status report is built from.
//!
//! The shipped Python engine probes each provider over the network
//! (`xpst.auth_status.collect_live_auth_status_async`). The Rust engine has no
//! live transports yet, so this module ports the part of that collector that is
//! decided **before** any socket opens — missing credentials, disabled accounts,
//! the local source, TikTok's two independent capabilities — and reports a
//! credentialed provider as *unverified* rather than inventing a verdict.
//!
//! That split is the whole point of the trait: a caller (a test, or the future
//! P1 transport) supplies its own [`ProbeSource`], and the report assembly
//! cannot tell the difference. Nothing here opens a socket, so the fixture runs
//! offline.
//!
//! The error strings are the shipped engine's, verbatim — they are what a user
//! copies into a terminal, and what `describe_remediation` matches on.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::canonical::{ProviderFacts, ProviderProbe};
use crate::facts::{CredentialFacts, ProbeDetails, ProbeResult};
use crate::state::Platform;

/// Where a report's live verdicts come from.
pub trait ProbeSource {
    /// One entry per provider the source could speak about. A provider absent
    /// from the map was never probed (Rumble), which the canonical builder
    /// renders as `null` facts rather than `false`.
    fn collect(&self, facts: &[ProviderFacts]) -> BTreeMap<Platform, ProviderProbe>;
}

/// The offline probe: every verdict the shipped engine reaches without a
/// network call.
#[derive(Debug, Clone, Default)]
pub struct OfflineProbe {
    /// The resolved `yt-dlp` path, when one exists (`resolve_ytdlp_path`).
    pub ytdlp_path: Option<String>,
    /// The instant the probe ran, epoch seconds.
    pub now: f64,
}

/// The text a credentialed-but-unproven provider reports.
///
/// Deliberately not "expired" and not "invalid": nothing was proven, and a
/// surface that sends the user into a credential re-entry for a failure that
/// may clear on its own is how a ban-risky password login gets triggered.
pub const UNVERIFIED_ERROR: &str =
    "Live check not available in the Rust engine yet — the credential is stored but unproven";

fn probe(
    authenticated: bool,
    session_valid: bool,
    auth_mode: &str,
    error: Option<String>,
    details: Value,
) -> ProbeResult {
    ProbeResult {
        authenticated,
        session_valid: Some(session_valid),
        live_checked: true,
        error,
        details: ProbeDetails::default(),
        raw_details: details,
        auth_mode: Some(auth_mode.to_string()),
        credentials_stored: false,
        configured: false,
    }
}

fn unverified(auth_mode: &str) -> ProbeResult {
    let mut details = Map::new();
    details.insert(
        "probe_class".to_string(),
        Value::String("unverified".to_string()),
    );
    details.insert(
        "probe_error".to_string(),
        Value::String(UNVERIFIED_ERROR.to_string()),
    );
    details.insert("probe_retryable".to_string(), Value::Bool(true));
    let mut result = probe(
        false,
        false,
        auth_mode,
        Some(UNVERIFIED_ERROR.to_string()),
        Value::Object(details),
    );
    result.details = ProbeDetails {
        probe_class: Some("unverified".to_string()),
    };
    result
}

fn disabled(auth_mode: &str) -> ProbeResult {
    probe(
        false,
        false,
        auth_mode,
        Some("disabled".to_string()),
        Value::Object(Map::new()),
    )
}

fn empty_details() -> Value {
    Value::Object(Map::new())
}

impl OfflineProbe {
    fn youtube(&self, facts: &CredentialFacts) -> ProbeResult {
        if !facts.enabled {
            return disabled("oauth");
        }
        if facts.token_file_exists || facts.token_document_present {
            return unverified("oauth");
        }
        probe(
            false,
            false,
            "oauth",
            Some(
                "YouTube is not configured yet (no stored OAuth token). Run: xpst auth youtube"
                    .to_string(),
            ),
            empty_details(),
        )
    }

    fn x(&self, facts: &CredentialFacts) -> ProbeResult {
        if !facts.enabled {
            return disabled(if facts.auth_mode == "api_v2" {
                "api_v2"
            } else {
                "cookies"
            });
        }
        if facts.auth_mode == "api_v2" {
            return if facts.bearer_token {
                unverified("api_v2")
            } else {
                probe(
                    false,
                    false,
                    "api_v2",
                    Some("X API v2 bearer token is not configured. Run: xpst auth x".to_string()),
                    empty_details(),
                )
            };
        }
        if facts.cookies_file_exists {
            return unverified("cookies");
        }
        probe(
            false,
            false,
            "cookies",
            Some("X cookies not found. Run: xpst auth x".to_string()),
            empty_details(),
        )
    }

    fn instagram(&self, facts: &CredentialFacts) -> ProbeResult {
        // `_auth_mode_for`: graph_api only when the mode is selected AND a token
        // is actually present; otherwise the session path is what would really
        // be used. The raw entry carries THIS mode, while the probe's own
        // `details` name the route it tried — which is why the shipped payload
        // reads `auth_mode: "session"` next to `details.probe: "graph_api"`.
        let effective = if facts.auth_mode == "graph_api" && facts.graph_access_token {
            "graph_api"
        } else {
            "session"
        };
        if !facts.enabled {
            return disabled(effective);
        }
        if facts.auth_mode == "graph_api" {
            if facts.graph_access_token && facts.graph_ig_user_id {
                return unverified(effective);
            }
            let mut details = Map::new();
            details.insert(
                "auth_mode".to_string(),
                Value::String("graph_api".to_string()),
            );
            details.insert("probe".to_string(), Value::String("graph_api".to_string()));
            return probe(
                false,
                false,
                effective,
                Some(
                    "IG_GRAPH_API_NOT_CONFIGURED: graph_access_token and graph_ig_user_id are \
                     required"
                        .to_string(),
                ),
                Value::Object(details),
            );
        }
        if facts.session_file_exists {
            return unverified(effective);
        }
        probe(
            false,
            false,
            effective,
            Some("Instagram session file not found. Run: xpst auth instagram".to_string()),
            empty_details(),
        )
    }

    fn bluesky(&self, facts: &CredentialFacts) -> ProbeResult {
        if !facts.enabled {
            return disabled("app_password");
        }
        if !facts.identifier {
            let mut details = Map::new();
            details.insert(
                "auth_mode".to_string(),
                Value::String("app_password".to_string()),
            );
            return probe(
                false,
                false,
                "app_password",
                Some("Bluesky handle is not configured.".to_string()),
                Value::Object(details),
            );
        }
        unverified("app_password")
    }

    /// `_tiktok_source_check()`: yt-dlp present + a usable cookie jar.
    fn tiktok_source(&self, facts: &CredentialFacts) -> ProbeResult {
        let ytdlp = self.ytdlp_path.clone().unwrap_or_default();
        let cookies_available = facts.cookies_from_browser || facts.cookies_file_exists;
        let ok = !ytdlp.is_empty() && cookies_available;
        let error = if ytdlp.is_empty() {
            Some("yt-dlp not found — TikTok source cannot download".to_string())
        } else if !cookies_available {
            Some(
                "No TikTok cookies available (cookie jar missing / cookies_from_browser unset)"
                    .to_string(),
            )
        } else {
            None
        };
        let mut details = Map::new();
        details.insert(
            "yt_dlp_installed".to_string(),
            Value::Bool(!ytdlp.is_empty()),
        );
        details.insert(
            "yt_dlp_path".to_string(),
            if ytdlp.is_empty() {
                Value::Null
            } else {
                Value::String(ytdlp)
            },
        );
        details.insert(
            "cookies_available".to_string(),
            Value::Bool(cookies_available),
        );
        details.insert(
            "username_configured".to_string(),
            Value::Bool(facts.username),
        );
        probe(ok, ok, "source_only", error, Value::Object(details))
    }

    fn tiktok(&self, facts: &CredentialFacts) -> ProviderProbe {
        let publish_mode = if facts.publish_mode.is_empty() {
            "auto"
        } else {
            facts.publish_mode.as_str()
        };
        let aggregator = facts.provider.trim().to_lowercase() == "zernio";
        let browser = !aggregator && matches!(publish_mode, "browser" | "browser_only");
        let base_mode = if aggregator {
            "api_key"
        } else if facts.client_key && (facts.access_token || facts.refresh_token) {
            "content_posting_api"
        } else if browser {
            "session"
        } else {
            "source_only"
        };

        let (source_check, destination_check) = if !facts.enabled {
            let off = disabled(base_mode);
            (off.clone(), off)
        } else {
            let source = self.tiktok_source(facts);
            let destination = if browser {
                unverified("session")
            } else if aggregator {
                unverified("api_key")
            } else if base_mode == "content_posting_api" {
                unverified("content_posting_api")
            } else {
                let mut details = Map::new();
                details.insert(
                    "auth_mode".to_string(),
                    Value::String("source_only".to_string()),
                );
                probe(
                    false,
                    false,
                    "source_only",
                    Some("TikTok Content Posting API is not configured".to_string()),
                    Value::Object(details),
                )
            };
            (source, destination)
        };

        // Which verdict speaks for the provider: on a publish route the posting
        // destination is the promise; only a source-only route is described by
        // the source check.
        let publish_route = matches!(base_mode, "content_posting_api" | "session" | "api_key");
        let compat = if publish_route {
            destination_check.clone()
        } else {
            source_check.clone()
        };
        let mut live = compat;
        live.auth_mode = Some(base_mode.to_string());
        ProviderProbe {
            live: Some(live),
            source_live: Some(source_check),
            destination_live: Some(destination_check),
            session_age_days: None,
            checked_at: Some(self.now),
            refresh_failed: false,
        }
    }

    fn local(&self, facts: &CredentialFacts) -> ProbeResult {
        let exists = facts.local_path_exists;
        let mut details = Map::new();
        details.insert(
            "path_configured".to_string(),
            Value::Bool(!facts.local_path.is_empty()),
        );
        details.insert("path_exists".to_string(), Value::Bool(exists));
        probe(
            exists,
            exists,
            "local",
            if exists {
                None
            } else {
                Some("Local source is not configured".to_string())
            },
            Value::Object(details),
        )
    }
}

impl ProbeSource for OfflineProbe {
    fn collect(&self, facts: &[ProviderFacts]) -> BTreeMap<Platform, ProviderProbe> {
        let mut out = BTreeMap::new();
        for provider in facts {
            let facts = &provider.facts;
            let entry = match provider.platform {
                Platform::YouTube => Some(ProviderProbe {
                    live: Some(self.youtube(facts)),
                    ..ProviderProbe::default()
                }),
                Platform::X => Some(ProviderProbe {
                    live: Some(self.x(facts)),
                    ..ProviderProbe::default()
                }),
                Platform::Instagram => Some(ProviderProbe {
                    live: Some(self.instagram(facts)),
                    ..ProviderProbe::default()
                }),
                Platform::Bluesky => Some(ProviderProbe {
                    live: Some(self.bluesky(facts)),
                    ..ProviderProbe::default()
                }),
                Platform::TikTok => Some(self.tiktok(facts)),
                Platform::Threads | Platform::Messenger | Platform::Facebook => {
                    let live = if facts.enabled {
                        unverified(provider.platform.default_auth_mode())
                    } else {
                        disabled(provider.platform.default_auth_mode())
                    };
                    Some(ProviderProbe {
                        live: Some(live),
                        ..ProviderProbe::default()
                    })
                }
                Platform::Local => Some(ProviderProbe {
                    live: Some(self.local(facts)),
                    ..ProviderProbe::default()
                }),
                // Rumble has no probe: the shipped collector never builds one,
                // so its canonical entry carries `null` live facts.
                Platform::Rumble => None,
            };
            if let Some(mut entry) = entry {
                if entry.checked_at.is_none() {
                    entry.checked_at = Some(self.now);
                }
                out.insert(provider.platform, entry);
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn facts(platform: Platform) -> ProviderFacts {
        ProviderFacts::no_credentials(platform)
    }

    #[test]
    fn a_fresh_profile_reports_the_shipped_engine_errors() {
        // The shipped default config: Instagram selects `graph_api` (with no
        // token), TikTok is a source-only route.
        let mut instagram = facts(Platform::Instagram);
        instagram.facts.auth_mode = "graph_api".to_string();
        let providers = vec![
            facts(Platform::YouTube),
            facts(Platform::X),
            instagram,
            facts(Platform::TikTok),
        ];
        let probes = OfflineProbe {
            ytdlp_path: Some("/usr/bin/yt-dlp".to_string()),
            now: 1_000.0,
        }
        .collect(&providers);
        assert_eq!(
            probes[&Platform::YouTube]
                .live
                .as_ref()
                .unwrap()
                .error
                .as_deref(),
            Some("YouTube is not configured yet (no stored OAuth token). Run: xpst auth youtube")
        );
        assert_eq!(
            probes[&Platform::X].live.as_ref().unwrap().error.as_deref(),
            Some("X cookies not found. Run: xpst auth x")
        );
        assert_eq!(
            probes[&Platform::Instagram]
                .live
                .as_ref()
                .unwrap()
                .error
                .as_deref(),
            Some(
                "IG_GRAPH_API_NOT_CONFIGURED: graph_access_token and graph_ig_user_id are required"
            )
        );
        let tiktok = &probes[&Platform::TikTok];
        assert_eq!(
            tiktok.source_live.as_ref().unwrap().error.as_deref(),
            Some("No TikTok cookies available (cookie jar missing / cookies_from_browser unset)")
        );
        assert_eq!(
            tiktok.destination_live.as_ref().unwrap().error.as_deref(),
            Some("TikTok Content Posting API is not configured")
        );
    }

    #[test]
    fn rumble_is_never_probed() {
        let probes = OfflineProbe::default().collect(&[facts(Platform::Rumble)]);
        assert!(probes.is_empty());
    }

    #[test]
    fn a_stored_credential_is_unverified_not_invalid() {
        let mut provider = facts(Platform::YouTube);
        provider.facts.token_file_exists = true;
        let probes = OfflineProbe::default().collect(&[provider]);
        let live = probes[&Platform::YouTube].live.as_ref().unwrap();
        assert!(!live.authenticated);
        assert_eq!(live.error.as_deref(), Some(UNVERIFIED_ERROR));
        assert_eq!(
            live.raw_details.get("probe_class").and_then(Value::as_str),
            Some("unverified")
        );
    }
}
