//! The canonical provider catalog — `xpst.provider_truth.canonical_provider_catalog()`.
//!
//! One document, three surfaces: `xpst providers` (the CLI), the `providers`
//! section of `xpst diagnostics`, and the `xpst_capabilities` MCP tool. Python
//! has ONE implementation and every surface consumes it, so a second copy here
//! would be a second opinion about whether an account can post — exactly the
//! defect `provider_truth` was written to end.
//!
//! The role statuses come from [`crate::provider_status::canonical_status`] —
//! the same function `xpst readiness` reads. What this module adds is the
//! catalog's own fields (`capabilities`, `is_official_api`, `byo_app`,
//! `legacy_roles`) and the replacement of `roles` (the per-role status map
//! `build_canonical_status` puts there) with the plain role-name list, in place,
//! so the key keeps its position.
//!
//! Nothing here opens a socket: the catalog is the offline view, so every
//! live-derived fact is `null` ("not probed") rather than `false`.

use xpst_auth::credentials::CredentialStore;
use xpst_config::output::{array, boolean, null, object, string};
use xpst_config::Value as YamlValue;

use crate::byo;
use crate::config_view::ConfigView;
use crate::provider_status::{self, canonical_status, manifest_for, Definition, Registry};

/// One catalog entry, in `canonical_provider_catalog()`'s key order.
pub fn catalog_entry(
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

/// Every catalog entry, in `SUPPORTED_PROVIDERS` order.
pub fn catalog(registry: &Registry, view: &ConfigView, store: &CredentialStore) -> Vec<YamlValue> {
    registry
        .definitions
        .iter()
        .map(|definition| catalog_entry(registry, view, store, definition))
        .collect()
}

/// The entries whose `roles` list names `role` — `video_destinations`,
/// `messaging` and `analytics` in the catalog payload.
pub fn with_role(providers: &[YamlValue], role: &str) -> Vec<YamlValue> {
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
}

/// The role vocabulary, in `canonical_provider_catalog()`'s order.
pub fn roles(registry: &Registry) -> Vec<YamlValue> {
    registry
        .roles
        .iter()
        .map(|role| string(role.clone()))
        .collect()
}

/// The registry, or the embedded-resource error as a message.
pub fn registry() -> Result<Registry, String> {
    provider_status::registry()
}
