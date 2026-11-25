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

use crate::commands::bootstrap;
use crate::commands::config_view::ConfigView;
use crate::commands::provider_status::{
    self, is_community_platform, manifests_in, should_show_platform,
};
use crate::output::{array, boolean, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

// The catalog entry is shared with `xpst diagnostics` and the `xpst_capabilities`
// MCP tool, so it lives below both crates.
use xpst_surface::catalog::{catalog, with_role};

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

    let providers: Vec<YamlValue> = catalog(&registry, &view, &store);

    if json_mode {
        let payload = object(vec![
            ("sources", array(sources)),
            ("destinations", array(destinations)),
            ("providers", array(providers.clone())),
            (
                "video_destinations",
                array(with_role(&providers, "video_destination")),
            ),
            ("messaging", array(with_role(&providers, "messaging"))),
            ("analytics", array(with_role(&providers, "analytics"))),
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
    use xpst_surface::catalog::catalog_entry;

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
