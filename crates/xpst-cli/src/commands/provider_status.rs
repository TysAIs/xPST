//! The canonical provider status, shared by every surface that reports it.
//!
//! `xpst providers` renders this as a catalog and `xpst readiness` reads it for
//! its source/destination rows. Python has ONE implementation
//! (`xpst.provider_truth.build_canonical_status`) and every surface consumes it,
//! so a second copy here would be a second opinion about whether an account can
//! post — exactly the defect the Python module was written to end.
//!
//! The *data* (provider identities, manifests, config defaults) is captured from
//! the shipped Python by `xtask/scripts/capture_provider_registry.py`; the
//! *behaviour* is ported here and held to the shipped engine by
//! `tests/local_surface.rs`, which diffs whole `--json` payloads against a live
//! `python -m xpst …` on the same scratch profile.
//!
//! Nothing here opens a socket: this is the offline view, so every live-derived
//! fact is `None` ("not probed") rather than `false`.

use std::path::Path;

use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;

use crate::commands::config_view::{self, ConfigView};

/// One `ProviderDefinition` from `provider_truth.SUPPORTED_PROVIDERS`.
pub struct Definition {
    pub name: String,
    pub display_name: String,
    pub roles: Vec<String>,
    pub auth_mode: String,
    pub official_api: bool,
    pub docs_url: Option<String>,
}

pub struct Registry {
    pub definitions: Vec<Definition>,
    pub roles: Vec<String>,
    pub manifests: YamlValue,
}

pub fn registry() -> Result<Registry, String> {
    let document = xpst_config::yaml::from_str(config_view::REGISTRY)
        .map_err(|err| format!("embedded provider registry: {err}"))?;
    let text =
        |value: Option<&YamlValue>| value.and_then(YamlValue::as_str).unwrap_or("").to_string();
    let list = |value: Option<&YamlValue>| -> Vec<String> {
        value
            .and_then(YamlValue::as_sequence)
            .map(|items| {
                items
                    .iter()
                    .filter_map(YamlValue::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    let definitions: Vec<Definition> = document
        .get("definitions")
        .and_then(YamlValue::as_sequence)
        .map(|items| {
            items
                .iter()
                .map(|item| Definition {
                    name: text(item.get("name")),
                    display_name: text(item.get("display_name")),
                    roles: list(item.get("roles")),
                    auth_mode: text(item.get("auth_mode")),
                    official_api: item
                        .get("official_api")
                        .and_then(YamlValue::as_bool)
                        .unwrap_or(false),
                    docs_url: item
                        .get("docs_url")
                        .and_then(YamlValue::as_str)
                        .map(str::to_string),
                })
                .collect()
        })
        .unwrap_or_default();
    if definitions.is_empty() {
        return Err("embedded provider registry has no definitions".to_string());
    }
    Ok(Registry {
        definitions,
        roles: list(document.get("roles")),
        manifests: document
            .get("manifests")
            .cloned()
            .unwrap_or(YamlValue::Null),
    })
}

/// The route key a destination manifest is captured under.
///
/// Each adapter's `manifest` switches on the configured route (Instagram
/// session vs Graph API, X cookies vs API v2, TikTok local/browser/aggregator,
/// YouTube auto/shorts/long, Rumble key vs manual handoff). The capture script
/// records one manifest per route; this is the same decision, in Rust.
pub fn destination_variant(name: &str, view: &ConfigView) -> &'static str {
    match name {
        "instagram" => {
            let graph = view.text_or_empty("instagram", "auth_mode") == "graph_api"
                && !view
                    .text_or_empty("instagram", "graph_access_token")
                    .is_empty();
            if graph {
                "graph_api"
            } else {
                "session"
            }
        }
        "x" => {
            if view.text_or_empty("x", "auth_mode") == "api_v2" {
                "api_v2"
            } else {
                "cookies"
            }
        }
        "tiktok" => {
            if view
                .text_or_empty("tiktok", "provider")
                .trim()
                .to_lowercase()
                == "zernio"
            {
                "zernio"
            } else if matches!(
                view.text_or_empty("tiktok", "publish_mode")
                    .to_lowercase()
                    .as_str(),
                "browser" | "browser_only"
            ) {
                "browser"
            } else {
                "local"
            }
        }
        "youtube" => match view
            .text_or_empty("youtube", "video_mode")
            .trim()
            .to_lowercase()
            .as_str()
        {
            "shorts" => "shorts",
            "long" => "long",
            // `auto` — and any unknown spelling — derives the mode from the
            // media probe, which is the `auto` manifest.
            _ => "auto",
        },
        "rumble" => {
            if view.text_or_empty("rumble", "api_key").is_empty() {
                "no_key"
            } else {
                "api_key"
            }
        }
        _ => "default",
    }
}

/// The manifest for one provider, with the account's own video ceiling applied.
///
/// `_manifest_for()` searches the destination registry first, then the source
/// registry, and returns the first name match.
pub fn manifest_for(registry: &Registry, name: &str, view: &ConfigView) -> Option<YamlValue> {
    let manifests = &registry.manifests;
    let destination = manifests
        .get("destinations")
        .and_then(|section| section.get(name))
        .and_then(|variants| variants.get(destination_variant(name, view)))
        .cloned();
    if destination.is_some() {
        return destination.map(|manifest| apply_account_ceiling(manifest, name, view));
    }
    manifests
        .get("sources")
        .and_then(|section| section.get(name))
        .and_then(|variants| variants.get("default"))
        .cloned()
}

/// `X.video_duration_cap`: a verified per-account ceiling replaces the
/// registry default in the manifest the upload guard reads.
fn apply_account_ceiling(manifest: YamlValue, name: &str, view: &ConfigView) -> YamlValue {
    if name != "x" {
        return manifest;
    }
    let configured = view.integer("x", "max_video_duration_seconds");
    if configured <= 0 {
        return manifest;
    }
    let mut manifest = manifest;
    if let Some(YamlValue::Mapping(extra)) = manifest
        .as_mapping_mut()
        .and_then(|map| map.get_mut("extra"))
    {
        extra.insert(
            YamlValue::from("max_video_duration_seconds"),
            YamlValue::Number(configured.into()),
        );
    }
    manifest
}

/// Every manifest in one registry section, sorted by provider name.
///
/// The section is explicit: `_manifest_for()` (used for the catalog's
/// capabilities) searches destinations first, but the `sources` and
/// `destinations` arrays each list their own registry.
pub fn manifests_in(registry: &Registry, section: &str, view: &ConfigView) -> Vec<YamlValue> {
    let Some(table) = registry
        .manifests
        .get(section)
        .and_then(YamlValue::as_mapping)
    else {
        return Vec::new();
    };
    let mut rows: Vec<YamlValue> = Vec::new();
    for (name, variants) in table.iter() {
        let Some(name) = name.as_str() else {
            continue;
        };
        let key = if section == "destinations" {
            destination_variant(name, view)
        } else {
            "default"
        };
        if let Some(manifest) = variants.get(key) {
            // The account's own video ceiling is a *destination* fact: the
            // source manifest has no `max_video_duration_seconds` to replace.
            let manifest = if section == "destinations" {
                apply_account_ceiling(manifest.clone(), name, view)
            } else {
                manifest.clone()
            };
            rows.push(manifest);
        }
    }
    rows.sort_by(|left, right| {
        let key = |value: &YamlValue| {
            value
                .get("name")
                .and_then(YamlValue::as_str)
                .unwrap_or("")
                .to_string()
        };
        key(left).cmp(&key(right))
    });
    rows
}

/// `_path_exists()`: a configured path that is present on disk.
fn path_exists(text: &str) -> bool {
    !text.is_empty() && Path::new(text).exists()
}

/// `_credential_stored()`: presence only, never the value.
fn credential_stored(store: &CredentialStore, key: &str) -> bool {
    store.retrieve(key).is_some_and(|value| !value.is_empty())
}

/// `_configured()`: whether the inputs for one role have been supplied.
fn configured(view: &ConfigView, store: &CredentialStore, name: &str, role: &str) -> bool {
    match name {
        "local" => path_exists(&view.text_or_empty("local", "path")),
        "tiktok" => {
            if role == "source" {
                return view.truthy("tiktok", "username")
                    || view.truthy("tiktok", "cookies_from_browser")
                    || path_exists(&view.text_or_empty("tiktok", "cookies_file"));
            }
            if view
                .text_or_empty("tiktok", "provider")
                .trim()
                .to_lowercase()
                == "zernio"
            {
                if view
                    .text_or_empty("tiktok", "aggregator.account_id")
                    .is_empty()
                {
                    return false;
                }
                let key = view.text_or_empty("tiktok", "aggregator.api_key_ref");
                let key = if key.is_empty() {
                    "zernio_api_key".to_string()
                } else {
                    key
                };
                return credential_stored(store, &key);
            }
            if !view.text_or_empty("tiktok", "client_key").is_empty()
                && (!view.text_or_empty("tiktok", "access_token").is_empty()
                    || !view.text_or_empty("tiktok", "refresh_token").is_empty())
            {
                return true;
            }
            let mode = view.text_or_empty("tiktok", "publish_mode").to_lowercase();
            if matches!(mode.as_str(), "browser" | "browser_only") {
                let jar = view.text_or_empty("tiktok", "cookies_file");
                let jar = if jar.is_empty() {
                    view.config_dir()
                        .join("credentials")
                        .join("tiktok_cookies.txt")
                        .display()
                        .to_string()
                } else {
                    jar
                };
                return path_exists(&jar);
            }
            false
        }
        "x" => {
            if view.text_or_empty("x", "auth_mode") == "api_v2" {
                return view.truthy("x", "bearer_token")
                    || (view.truthy("x", "api_key")
                        && view.truthy("x", "api_secret")
                        && view.truthy("x", "access_token")
                        && view.truthy("x", "access_token_secret"));
            }
            path_exists(&view.text_or_empty("x", "cookies_file"))
        }
        "instagram" => {
            if view.text_or_empty("instagram", "auth_mode") == "graph_api" {
                return view.truthy("instagram", "graph_access_token")
                    || path_exists(&view.text_or_empty("instagram", "session_file"));
            }
            path_exists(&view.text_or_empty("instagram", "session_file"))
        }
        "youtube" => path_exists(&view.text_or_empty("youtube", "token_file")),
        "threads" => {
            view.truthy("threads", "graph_access_token")
                && view.truthy("threads", "threads_user_id")
        }
        "bluesky" => view.truthy("bluesky", "identifier"),
        "messenger" => view.truthy("messenger", "page_access_token"),
        "facebook" => {
            view.truthy("facebook", "page_id") && view.truthy("facebook", "page_access_token")
        }
        _ => false,
    }
}

/// `_auth_mode()`: the mode the provider actually runs in for this config.
///
/// Python reads `str(getattr(account, "auth_mode", "") or "")`, so a numeric
/// value written by `xpst config set` is reported as its `str()` (`2` → `"2"`)
/// instead of falling through to the definition's default.
fn auth_mode(view: &ConfigView, name: &str, definition: &Definition) -> String {
    let configured = view.python_text(name, "auth_mode");
    if !configured.is_empty() {
        return configured;
    }
    if name == "tiktok" {
        if view
            .text_or_empty("tiktok", "provider")
            .trim()
            .to_lowercase()
            == "zernio"
        {
            return "api_key".to_string();
        }
        if !view.text_or_empty("tiktok", "client_key").is_empty()
            && (!view.text_or_empty("tiktok", "access_token").is_empty()
                || !view.text_or_empty("tiktok", "refresh_token").is_empty())
        {
            return "content_posting_api".to_string();
        }
        let mode = view.text_or_empty("tiktok", "publish_mode").to_lowercase();
        if matches!(mode.as_str(), "browser" | "browser_only") {
            return "session".to_string();
        }
        return "source_only".to_string();
    }
    definition.auth_mode.clone()
}

/// One role's offline status.
pub struct RoleStatus {
    pub role: String,
    pub state: &'static str,
    pub enabled: bool,
    pub auth_mode: String,
}

/// `_state_for()` on the offline path (no live probe has run).
fn state_for(
    view: &ConfigView,
    store: &CredentialStore,
    name: &str,
    role: &str,
    enabled: bool,
) -> &'static str {
    if !enabled {
        return "disabled";
    }
    let configured = configured(view, store, name, role);
    if name == "tiktok"
        && role == "video_destination"
        && view.truthy("tiktok", "sandbox")
        && configured
    {
        return "blocked_external_review";
    }
    if configured {
        "ready"
    } else {
        "unconfigured"
    }
}

/// `_aggregate_state()`.
fn aggregate_state(statuses: &[RoleStatus], primary: &str) -> &'static str {
    let primary_state = statuses
        .iter()
        .find(|status| status.role == primary)
        .map(|status| status.state)
        .unwrap_or("unconfigured");
    if primary_state == "ready" {
        return primary_state;
    }
    for candidate in ["degraded", "blocked_external_review", "unconfigured"] {
        if statuses.iter().any(|status| status.state == candidate) {
            return candidate;
        }
    }
    primary_state
}

/// `_enabled()`: `local` is always on; every other provider follows its account.
pub fn enabled(view: &ConfigView, name: &str) -> bool {
    if name == "local" {
        return true;
    }
    view.truthy(name, "enabled")
}

/// `is_community_platform()`: the auth mode routes through an unofficial client.
pub fn is_community_platform(view: &ConfigView, name: &str) -> bool {
    let unofficial = match name {
        "instagram" => "session",
        "x" => "cookies",
        _ => return false,
    };
    view.python_text(name, "auth_mode").trim().to_lowercase() == unofficial
}

/// `should_show_platform()`.
pub fn should_show_platform(view: &ConfigView, name: &str) -> bool {
    if view.provider_mode() == "community" {
        return true;
    }
    !is_community_platform(view, name)
}

/// `_posting_note()`: one truthful sentence explaining why a platform cannot be
/// posted to.
pub fn posting_note(
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

/// `posting_truth()`: the posting verdict for one canonical entry.
///
/// A platform can be perfectly healthy as a SOURCE and still be unable to
/// receive a post (TikTok before its Content Posting API app is approved), so
/// every posting decision goes through this rather than reading the
/// platform-level `authenticated` flag.
pub struct PostingTruth {
    pub posting_destination: bool,
    pub posting_role: Option<String>,
    pub posting_state: Option<&'static str>,
    pub posting_error: Option<String>,
    pub can_post: bool,
    pub source_only: bool,
    pub posting_note: Option<String>,
}

/// One provider's canonical status (`build_canonical_status`'s entry).
pub struct CanonicalEntry {
    pub name: String,
    pub display_name: String,
    /// Per-role status, in the definition's role order.
    pub roles: Vec<RoleStatus>,
    /// The aggregate state (`ready`/`degraded`/`unconfigured`/`disabled`/…).
    pub aggregate: &'static str,
    /// The primary role's auth mode — the platform-level one.
    pub auth_mode: String,
    pub enabled: bool,
    pub posting: PostingTruth,
}

impl CanonicalEntry {
    /// The status of one role, if the provider has it.
    pub fn role(&self, role: &str) -> Option<&RoleStatus> {
        self.roles.iter().find(|status| status.role == role)
    }
}

/// `build_canonical_status()` on the offline path.
///
/// `live_status` is deliberately absent: `readiness` is offline and
/// deterministic, so every live-derived fact is `None` ("not probed") rather
/// than `false`. A surface that emitted `false` here would contradict
/// `xpst auth status`, which does probe.
pub fn canonical_status(
    view: &ConfigView,
    store: &CredentialStore,
) -> Result<Vec<CanonicalEntry>, String> {
    let registry = registry()?;
    Ok(registry
        .definitions
        .iter()
        .map(|definition| entry_for(view, store, definition))
        .collect())
}

fn entry_for(
    view: &ConfigView,
    store: &CredentialStore,
    definition: &Definition,
) -> CanonicalEntry {
    let enabled = enabled(view, &definition.name);
    let roles: Vec<RoleStatus> = definition
        .roles
        .iter()
        .map(|role| RoleStatus {
            role: role.clone(),
            state: state_for(view, store, &definition.name, role, enabled),
            enabled,
            auth_mode: auth_mode(view, &definition.name, definition),
        })
        .collect();

    let primary = if definition
        .roles
        .iter()
        .any(|role| role == "video_destination")
    {
        "video_destination"
    } else if definition.roles.iter().any(|role| role == "messaging") {
        "messaging"
    } else {
        "source"
    };
    let aggregate = aggregate_state(&roles, primary);
    let compat_auth_mode = roles
        .iter()
        .find(|status| status.role == primary)
        .expect("the primary role is always in the definition")
        .auth_mode
        .clone();
    let posting = posting_truth(&definition.display_name, &roles, &compat_auth_mode);

    CanonicalEntry {
        name: definition.name.clone(),
        display_name: definition.display_name.clone(),
        roles,
        aggregate,
        auth_mode: compat_auth_mode,
        enabled,
        posting,
    }
}

fn posting_truth(display_name: &str, roles: &[RoleStatus], entry_auth_mode: &str) -> PostingTruth {
    // `POSTING_ROLES` order: video_destination, then messaging.
    let posting = roles
        .iter()
        .find(|status| status.role == "video_destination" || status.role == "messaging");
    let state = posting.map(|status| status.state);
    let can_post = state == Some("ready");
    let source_only = posting.is_some()
        && !can_post
        && (entry_auth_mode == "source_only"
            || posting.is_some_and(|status| status.auth_mode == "source_only"));
    PostingTruth {
        posting_destination: posting.is_some(),
        posting_role: posting.map(|status| status.role.clone()),
        posting_state: state,
        posting_error: None,
        can_post,
        source_only,
        posting_note: posting_note(display_name, source_only, state, None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn view(text: &str) -> ConfigView {
        let document = xpst_config::yaml::from_str(text).expect("valid YAML");
        ConfigView::load(&document, Path::new("/tmp/profile"), &|_| None).expect("defaults")
    }

    fn store() -> CredentialStore {
        CredentialStore::new(Path::new("/tmp/xpst-provider-status-test"))
    }

    #[test]
    fn the_registry_carries_every_provider_and_manifest() {
        let registry = registry().expect("registry parses");
        assert_eq!(registry.definitions.len(), 10);
        assert_eq!(
            registry.roles,
            vec!["source", "video_destination", "messaging", "analytics"]
        );
        let destinations = registry
            .manifests
            .get("destinations")
            .and_then(YamlValue::as_mapping)
            .expect("destinations");
        assert_eq!(destinations.len(), 9);
        let sources = registry
            .manifests
            .get("sources")
            .and_then(YamlValue::as_mapping)
            .expect("sources");
        assert_eq!(sources.len(), 5);
    }

    #[test]
    fn the_route_key_matches_the_configured_route() {
        // Instagram's Graph route needs BOTH the mode and a token: the shipped
        // default is `graph_api` with no token, which takes the session
        // manifest (the adapter's own `graph_api` flag).
        assert_eq!(
            destination_variant("instagram", &view("version: 4\n")),
            "session"
        );
        assert_eq!(
            destination_variant(
                "instagram",
                &view("accounts:\n  instagram:\n    auth_mode: graph_api\n    graph_access_token: t\n")
            ),
            "graph_api"
        );
        assert_eq!(
            destination_variant(
                "instagram",
                &view("accounts:\n  instagram:\n    auth_mode: session\n")
            ),
            "session"
        );
        assert_eq!(destination_variant("x", &view("version: 4\n")), "cookies");
        assert_eq!(
            destination_variant("x", &view("accounts:\n  x:\n    auth_mode: api_v2\n")),
            "api_v2"
        );
        assert_eq!(
            destination_variant("tiktok", &view("version: 4\n")),
            "local"
        );
        assert_eq!(
            destination_variant(
                "tiktok",
                &view("accounts:\n  tiktok:\n    publish_mode: browser\n")
            ),
            "browser"
        );
        assert_eq!(
            destination_variant(
                "tiktok",
                &view("accounts:\n  tiktok:\n    provider: zernio\n")
            ),
            "zernio"
        );
        assert_eq!(
            destination_variant("youtube", &view("version: 4\n")),
            "auto"
        );
        assert_eq!(
            destination_variant(
                "youtube",
                &view("accounts:\n  youtube:\n    video_mode: shorts\n")
            ),
            "shorts"
        );
        assert_eq!(
            destination_variant("rumble", &view("version: 4\n")),
            "no_key"
        );
        assert_eq!(
            destination_variant("rumble", &view("accounts:\n  rumble:\n    api_key: k\n")),
            "api_key"
        );
    }

    #[test]
    fn every_route_key_has_a_captured_manifest() {
        // A route the capture script never recorded would make the command
        // silently fall back to a different manifest, so assert each key.
        let registry = registry().expect("registry parses");
        for (name, keys) in [
            ("instagram", ["session", "graph_api"].as_slice()),
            ("x", ["cookies", "api_v2"].as_slice()),
            ("tiktok", ["local", "browser", "zernio"].as_slice()),
            ("youtube", ["auto", "shorts", "long"].as_slice()),
            ("rumble", ["no_key", "api_key"].as_slice()),
        ] {
            for key in keys {
                assert!(
                    registry
                        .manifests
                        .get("destinations")
                        .and_then(|section| section.get(name))
                        .and_then(|variants| variants.get(key))
                        .is_some(),
                    "{name} has no captured `{key}` manifest"
                );
            }
        }
    }

    #[test]
    fn a_verified_ceiling_replaces_the_destination_manifest_only() {
        // `X.video_duration_cap` is a destination fact. Applying it to the
        // source manifest would invent a key Python never emits there.
        let registry = registry().expect("registry parses");
        let view = view("accounts:\n  x:\n    max_video_duration_seconds: 300\n");
        let destinations = manifests_in(&registry, "destinations", &view);
        let destination = destinations
            .iter()
            .find(|row| row.get("name").and_then(YamlValue::as_str) == Some("x"))
            .expect("x destination");
        assert_eq!(
            destination
                .get("extra")
                .and_then(|extra| extra.get("max_video_duration_seconds"))
                .and_then(YamlValue::as_i64),
            Some(300)
        );
        let sources = manifests_in(&registry, "sources", &view);
        let source = sources
            .iter()
            .find(|row| row.get("name").and_then(YamlValue::as_str) == Some("x"))
            .expect("x source");
        assert!(
            source
                .get("extra")
                .and_then(|extra| extra.get("max_video_duration_seconds"))
                .is_none(),
            "the source manifest must not gain a destination-only key"
        );
    }

    #[test]
    fn a_pristine_profile_is_unconfigured_and_hides_the_community_routes() {
        let view = view("version: 4\n");
        let entries = canonical_status(&view, &store()).expect("status");
        let youtube = entries
            .iter()
            .find(|entry| entry.name == "youtube")
            .expect("youtube");
        assert_eq!(youtube.aggregate, "unconfigured");
        assert!(youtube.enabled);
        assert!(!youtube.posting.can_post);
        // X's default route is the unofficial cookie client, so it is hidden.
        assert!(!should_show_platform(&view, "x"));
        assert!(should_show_platform(&view, "youtube"));
    }

    #[test]
    fn a_source_only_provider_explains_itself() {
        let view = view("version: 4\n");
        let entries = canonical_status(&view, &store()).expect("status");
        let tiktok = entries
            .iter()
            .find(|entry| entry.name == "tiktok")
            .expect("tiktok");
        assert!(tiktok.posting.source_only);
        let note = tiktok.posting.posting_note.clone().unwrap();
        assert!(note.contains("is a source only"), "unexpected note: {note}");
    }

    #[test]
    fn every_provider_reports_one_row_per_declared_role() {
        // `role_readiness` walks the definition's roles, so a provider that
        // lost a role would silently drop a readiness row.
        let view = view("version: 4\n");
        let entries = canonical_status(&view, &store()).expect("status");
        let registry = registry().expect("registry parses");
        assert_eq!(entries.len(), registry.definitions.len());
        for (entry, definition) in entries.iter().zip(registry.definitions.iter()) {
            assert_eq!(entry.name, definition.name);
            assert_eq!(
                entry
                    .roles
                    .iter()
                    .map(|role| role.role.clone())
                    .collect::<Vec<_>>(),
                definition.roles,
                "{} role order drifted",
                definition.name
            );
        }
    }
}
