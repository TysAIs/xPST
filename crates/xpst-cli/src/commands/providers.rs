//! `xpst providers` — the provider registry and the canonical role catalog.
//!
//! Parity target: `src/xpst/cli.py::providers`, which joins three things:
//!
//! * `SourceRegistry.list_manifests()` / `PlatformRegistry.list_manifests()` —
//!   the per-adapter `ProviderManifest` objects;
//! * `xpst.provider_truth.canonical_provider_catalog()` — the role-aware
//!   catalog (`build_canonical_status` + `with_posting_truth`);
//! * `XPSTConfig.provider_mode` / `is_community_platform()` /
//!   `should_show_platform()` — the per-destination `provider_mode` and
//!   `visible` flags.
//!
//! The *data* (provider identities, manifests, config defaults) is captured
//! from the shipped Python by `xtask/scripts/capture_provider_registry.py`; the
//! *behaviour* is ported here and held to the shipped engine by
//! `tests/local_surface.rs`, which diffs the whole `providers --json` payload
//! against a live `python -m xpst providers --json` on the same scratch profile.
//!
//! Nothing here opens a socket: the catalog is the offline view, so every
//! live-derived fact is `null` ("not probed") rather than `false`.
//!
//! **Divergences, recorded in the ledger note:** text mode renders a plain
//! table instead of Rich's box drawing (the same choice every other ported
//! command makes; `--json` is the parity surface), and the config view does not
//! run Python's `auto_migrate()` on read.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;

use crate::commands::config_view::{self, ConfigView};
use crate::commands::{bootstrap, byo};
use crate::output::{array, boolean, null, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// One `ProviderDefinition` from `provider_truth.SUPPORTED_PROVIDERS`.
struct Definition {
    name: String,
    display_name: String,
    roles: Vec<String>,
    auth_mode: String,
    official_api: bool,
    docs_url: Option<String>,
}

struct Registry {
    definitions: Vec<Definition>,
    roles: Vec<String>,
    manifests: YamlValue,
}

fn registry() -> Result<Registry, String> {
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
fn destination_variant(name: &str, view: &ConfigView) -> &'static str {
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
fn manifest_for(registry: &Registry, name: &str, view: &ConfigView) -> Option<YamlValue> {
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
fn manifests_in(registry: &Registry, section: &str, view: &ConfigView) -> Vec<YamlValue> {
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
fn auth_mode(view: &ConfigView, name: &str, definition: &Definition) -> String {
    let configured = view.text_or_empty(name, "auth_mode");
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
struct RoleStatus {
    role: String,
    state: &'static str,
    enabled: bool,
    auth_mode: String,
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
fn enabled(view: &ConfigView, name: &str) -> bool {
    if name == "local" {
        return true;
    }
    view.truthy(name, "enabled")
}

/// `is_community_platform()`: the auth mode routes through an unofficial client.
fn is_community_platform(view: &ConfigView, name: &str) -> bool {
    let unofficial = match name {
        "instagram" => "session",
        "x" => "cookies",
        _ => return false,
    };
    view.text_or_empty(name, "auth_mode").trim().to_lowercase() == unofficial
}

/// `should_show_platform()`.
fn should_show_platform(view: &ConfigView, name: &str) -> bool {
    if view.provider_mode() == "community" {
        return true;
    }
    !is_community_platform(view, name)
}

/// `_posting_note()`.
fn posting_note(display_name: &str, source_only: bool, error: Option<&str>) -> Option<String> {
    if !source_only {
        return None;
    }
    let mut note = format!(
        "{display_name} is a source only: xPST downloads from it, and it cannot be used as a \
         posting destination."
    );
    if let Some(error) = error {
        note.push_str(&format!(" The uploader reported: {error}"));
    }
    Some(note)
}

/// One catalog entry, in `canonical_provider_catalog()`'s key order.
fn catalog_entry(
    registry: &Registry,
    view: &ConfigView,
    store: &CredentialStore,
    definition: &Definition,
) -> YamlValue {
    let enabled = enabled(view, &definition.name);
    let statuses: Vec<RoleStatus> = definition
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
    let aggregate = aggregate_state(&statuses, primary);
    let compat = statuses
        .iter()
        .find(|status| status.role == primary)
        .expect("the primary role is always in the definition");

    let mut role_status = xpst_config::Mapping::new();
    let mut role_states = xpst_config::Mapping::new();
    for status in &statuses {
        role_status.insert(
            YamlValue::from(status.role.as_str()),
            object(vec![
                ("platform", string(definition.name.clone())),
                ("role", string(status.role.clone())),
                ("state", string(status.state)),
                ("ready", boolean(status.state == "ready")),
                ("enabled", boolean(status.enabled)),
                ("authenticated", null()),
                ("session_valid", null()),
                ("auth_mode", string(status.auth_mode.clone())),
                ("live_checked", null()),
                ("error", null()),
                ("details", object(Vec::new())),
            ]),
        );
        role_states.insert(YamlValue::from(status.role.as_str()), string(status.state));
    }

    let mut entries: Vec<(&str, YamlValue)> = vec![
        ("name", string(definition.name.clone())),
        ("display_name", string(definition.display_name.clone())),
        ("state", string(aggregate)),
        ("state_value", string(aggregate)),
        ("roles", YamlValue::Mapping(role_status.clone())),
        ("role_status", YamlValue::Mapping(role_status)),
        ("role_states", YamlValue::Mapping(role_states)),
        (
            "role_names",
            array(
                definition
                    .roles
                    .iter()
                    .map(|role| string(role.clone()))
                    .collect(),
            ),
        ),
        ("auth_mode", string(compat.auth_mode.clone())),
        ("authenticated", null()),
        ("session_valid", null()),
        ("live_checked", null()),
        ("error", null()),
        ("details", object(Vec::new())),
        ("probe_class", null()),
        ("probe_error", null()),
        ("probe_retryable", null()),
    ];
    if definition.name == "instagram" {
        entries.push((
            "account_type",
            string(view.text_or_empty("instagram", "account_type")),
        ));
        entries.push((
            "account_type_source",
            string(view.text_or_empty("instagram", "account_type_source")),
        ));
    }
    entries.push(("enabled", boolean(enabled)));
    entries.push(("legacy_authenticated", boolean(false)));

    // `with_posting_truth()`: the flat posting verdict every surface reads.
    let posting_role = statuses
        .iter()
        .find(|status| status.role == "video_destination" || status.role == "messaging");
    let posting_state = posting_role.map(|status| status.state);
    let can_post = posting_state == Some("ready");
    let source_only = posting_role.is_some()
        && !can_post
        && (compat.auth_mode == "source_only"
            || posting_role.is_some_and(|status| status.auth_mode == "source_only"));
    entries.push(("posting_destination", boolean(posting_role.is_some())));
    entries.push((
        "posting_role",
        posting_role
            .map(|status| string(status.role.clone()))
            .unwrap_or_else(null),
    ));
    entries.push((
        "posting_state",
        posting_state.map(string).unwrap_or_else(null),
    ));
    entries.push(("posting_error", null()));
    entries.push(("can_post", boolean(can_post)));
    entries.push(("source_only", boolean(source_only)));
    entries.push((
        "posting_note",
        posting_note(&definition.display_name, source_only, None)
            .map(string)
            .unwrap_or_else(null),
    ));

    // `canonical_provider_catalog()`'s additions.
    let manifest = manifest_for(registry, &definition.name, view);
    let capabilities = manifest
        .as_ref()
        .and_then(|manifest| manifest.get("capabilities"))
        .and_then(YamlValue::as_sequence)
        .map(|items| {
            items
                .iter()
                .filter_map(YamlValue::as_str)
                .map(|item| string(item.to_string()))
                .collect()
        })
        .unwrap_or_default();
    let legacy_roles = manifest
        .as_ref()
        .and_then(|manifest| manifest.get("roles"))
        .and_then(YamlValue::as_sequence)
        .map(|items| {
            items
                .iter()
                .filter_map(YamlValue::as_str)
                .map(|item| string(item.to_string()))
                .collect()
        })
        .unwrap_or_default();

    let mut official = definition.official_api;
    if definition.name == "tiktok"
        && view
            .text_or_empty("tiktok", "provider")
            .trim()
            .to_lowercase()
            == "zernio"
    {
        // The aggregator is a vendor acting between xPST and TikTok under its
        // own audited app, not TikTok's own API.
        official = false;
    }
    let byo_app = byo::status_for_platform(view.document(), &definition.name, store);
    if definition.name == "instagram" {
        official = byo_app
            .as_ref()
            .is_some_and(|app| app.get("configured").and_then(YamlValue::as_bool) == Some(true))
            && view.text_or_empty("instagram", "auth_mode") == "graph_api";
    }

    entries.push(("capabilities", array(capabilities)));
    entries.push(("is_official_api", boolean(official)));
    entries.push((
        "docs_url",
        definition.docs_url.clone().map(string).unwrap_or_else(null),
    ));
    entries.push(("byo_app", byo_app.unwrap_or_else(null)));
    entries.push(("legacy_roles", array(legacy_roles)));

    // `canonical_provider_catalog()` replaces `roles` (the per-role status map
    // `build_canonical_status` puts there) with the plain role-name list, in
    // place, so the key keeps its position.
    let mut entry = object(entries);
    if let Some(map) = entry.as_mapping_mut() {
        map.insert(
            YamlValue::from("roles"),
            array(
                definition
                    .roles
                    .iter()
                    .map(|role| string(role.clone()))
                    .collect(),
            ),
        );
    }
    entry
}

/// Run `providers`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let registry = match registry() {
        Ok(registry) => registry,
        Err(message) => {
            let _ = writeln!(err, "xpst providers: {message}");
            return exit::GENERAL;
        }
    };

    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let document = match xpst_config::ConfigDocument::read(&config_path) {
        Ok(document) => document.raw().clone(),
        Err(error) => {
            let _ = writeln!(err, "{error}");
            return exit::CONFIG_ERROR;
        }
    };
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    let env = |name: &str| std::env::var(name).ok();
    let view = match ConfigView::load(&document, &config_dir, &env) {
        Ok(view) => view,
        Err(message) => {
            let _ = writeln!(err, "xpst providers: {message}");
            return exit::GENERAL;
        }
    };
    let store = CredentialStore::new(&config_dir);

    let sources = manifests_in(&registry, "sources", &view);
    let mut destinations = manifests_in(&registry, "destinations", &view);
    for destination in &mut destinations {
        let name = destination
            .get("name")
            .and_then(YamlValue::as_str)
            .unwrap_or("")
            .to_string();
        let mode = if is_community_platform(&view, &name) {
            "community"
        } else {
            "official"
        };
        if let Some(map) = destination.as_mapping_mut() {
            map.insert(YamlValue::from("provider_mode"), string(mode));
            map.insert(
                YamlValue::from("visible"),
                boolean(should_show_platform(&view, &name)),
            );
        }
    }

    let providers: Vec<YamlValue> = registry
        .definitions
        .iter()
        .map(|definition| catalog_entry(&registry, &view, &store, definition))
        .collect();

    let with_role = |role: &str| -> Vec<YamlValue> {
        providers
            .iter()
            .filter(|entry| {
                entry
                    .get("roles")
                    .and_then(YamlValue::as_sequence)
                    .is_some_and(|roles| roles.iter().any(|item| item.as_str() == Some(role)))
            })
            .cloned()
            .collect()
    };

    if json_mode {
        let payload = object(vec![
            ("sources", array(sources)),
            ("destinations", array(destinations)),
            ("providers", array(providers.clone())),
            ("video_destinations", array(with_role("video_destination"))),
            ("messaging", array(with_role("messaging"))),
            ("analytics", array(with_role("analytics"))),
            (
                "roles",
                array(
                    registry
                        .roles
                        .iter()
                        .map(|role| string(role.clone()))
                        .collect(),
                ),
            ),
            ("provider_mode", string(view.provider_mode().to_string())),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let _ = write_text(out, &render_text(&sources, &destinations));
    exit::SUCCESS
}

/// The plain-text tables (`--json` is the parity surface).
fn render_text(sources: &[YamlValue], destinations: &[YamlValue]) -> String {
    let mut text = String::new();
    for (title, rows) in [("Sources", sources), ("Destinations", destinations)] {
        text.push_str(&format!("{title}\n"));
        for row in rows {
            let field = |name: &str| row.get(name).and_then(YamlValue::as_str).unwrap_or("");
            let mode = row
                .get("provider_mode")
                .and_then(YamlValue::as_str)
                .unwrap_or("official");
            let official = if row.get("is_official_api").and_then(YamlValue::as_bool) == Some(true)
            {
                "yes"
            } else {
                "no"
            };
            text.push_str(&format!(
                "  {} | {} | official API: {} | auth: {}\n",
                field("display_name"),
                mode,
                official,
                field("auth_mode")
            ));
        }
        text.push('\n');
    }
    text.trim_end().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn view(text: &str) -> ConfigView {
        let document = xpst_config::yaml::from_str(text).expect("valid YAML");
        ConfigView::load(&document, Path::new("/tmp/profile"), &|_| None).expect("defaults")
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
        let registry = registry().expect("registry parses");
        let view = view("version: 4\n");
        let store = CredentialStore::new(Path::new("/tmp/xpst-providers-test"));
        let youtube = registry
            .definitions
            .iter()
            .find(|definition| definition.name == "youtube")
            .expect("youtube");
        let entry = catalog_entry(&registry, &view, &store, youtube);
        assert_eq!(
            entry.get("state").and_then(YamlValue::as_str),
            Some("unconfigured")
        );
        assert_eq!(
            entry.get("enabled").and_then(YamlValue::as_bool),
            Some(true)
        );
        assert_eq!(
            entry.get("can_post").and_then(YamlValue::as_bool),
            Some(false)
        );
        // X's default route is the unofficial cookie client, so it is hidden.
        assert!(!should_show_platform(&view, "x"));
        assert!(should_show_platform(&view, "youtube"));
    }

    #[test]
    fn a_source_only_provider_explains_itself() {
        let registry = registry().expect("registry parses");
        let view = view("version: 4\n");
        let store = CredentialStore::new(Path::new("/tmp/xpst-providers-test"));
        let tiktok = registry
            .definitions
            .iter()
            .find(|definition| definition.name == "tiktok")
            .expect("tiktok");
        let entry = catalog_entry(&registry, &view, &store, tiktok);
        assert_eq!(
            entry.get("source_only").and_then(YamlValue::as_bool),
            Some(true)
        );
        let note = entry
            .get("posting_note")
            .and_then(YamlValue::as_str)
            .unwrap();
        assert!(note.contains("is a source only"), "unexpected note: {note}");
    }
}
