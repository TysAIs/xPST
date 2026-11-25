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

use crate::commands::config_view::ConfigView;
use crate::commands::provider_status::{
    self, canonical_status, is_community_platform, manifest_for, manifests_in,
    should_show_platform, Definition, Registry,
};
use crate::commands::{bootstrap, byo};
use crate::output::{array, boolean, null, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// One catalog entry, in `canonical_provider_catalog()`'s key order.
///
/// The role statuses come from [`provider_status::canonical_status`] — the same
/// function `xpst readiness` reads — so the two surfaces cannot disagree about
/// whether an account can post. What this adds is the catalog's own fields
/// (`capabilities`, `is_official_api`, `byo_app`, `legacy_roles`) and the
/// replacement of `roles` with the plain role-name list.
fn catalog_entry(
    registry: &Registry,
    view: &ConfigView,
    store: &CredentialStore,
    definition: &Definition,
) -> YamlValue {
    let canonical = canonical_status(view, store)
        .ok()
        .and_then(|entries| {
            entries
                .into_iter()
                .find(|entry| entry.name == definition.name)
        })
        .expect("every definition has a canonical entry");
    let enabled = canonical.enabled;
    let aggregate = canonical.aggregate;
    let posting = &canonical.posting;

    let mut role_status = xpst_config::Mapping::new();
    let mut role_states = xpst_config::Mapping::new();
    for status in &canonical.roles {
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
        ("auth_mode", string(canonical.auth_mode.clone())),
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
    entries.push(("posting_destination", boolean(posting.posting_destination)));
    entries.push((
        "posting_role",
        posting
            .posting_role
            .clone()
            .map(string)
            .unwrap_or_else(null),
    ));
    entries.push((
        "posting_state",
        posting.posting_state.map(string).unwrap_or_else(null),
    ));
    entries.push((
        "posting_error",
        posting
            .posting_error
            .clone()
            .map(string)
            .unwrap_or_else(null),
    ));
    entries.push(("can_post", boolean(posting.can_post)));
    entries.push(("source_only", boolean(posting.source_only)));
    entries.push((
        "posting_note",
        posting
            .posting_note
            .clone()
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
    let registry = match provider_status::registry() {
        Ok(registry) => registry,
        Err(message) => {
            let _ = writeln!(err, "xpst providers: {message}");
            return exit::GENERAL;
        }
    };

    let (config_path, document) = match bootstrap::load_document(global) {
        Ok(loaded) => loaded,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let document = document.raw().clone();
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
    fn a_pristine_profile_is_unconfigured_and_hides_the_community_routes() {
        let registry = provider_status::registry().expect("registry parses");
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
        let registry = provider_status::registry().expect("registry parses");
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
