//! Bring-your-own developer app: the registry and the masked status payload.
//!
//! Parity target: `src/xpst/byo.py` (`BYO_PLATFORMS`, `resolve_byo_app`,
//! `byo_status_for_platform`, `mask_app_id`).
//!
//! The registry is captured, not transcribed: `xtask/scripts/capture_byo_platforms.py`
//! runs the shipped Python and writes `resources/byo_platforms.json`, because a
//! wrong `store_id_key` in a hand-copied table reads as "no app configured"
//! forever and nothing would catch it.
//!
//! Credential resolution order is Python's: the encrypted store first, then the
//! config fields (with the `XPST_*_APP_ID`/`_APP_SECRET` environment overrides
//! `XPSTConfig.load` applies), then "id without secret" as `config-incomplete`.
//!
//! **Divergence, recorded in the ledger note:** the OS keyring is not consulted.
//! Python's store is keyring-first only when `XPST_USE_KEYRING=1` (opt-in, off
//! by default); with it set, a credential that lives in the keychain reads as
//! absent here.
//!
//! This module lives in `xpst-surface` because three surfaces read it: the
//! `xpst byo` commands, the `byo_app` field of the canonical provider catalog
//! (`xpst providers`, `xpst diagnostics`, `xpst_capabilities`) and the
//! `xpst_byo_app` MCP tool.

use serde_json::Value;
use xpst_auth::credentials::CredentialStore;
use xpst_config::output::{array, boolean, object, string};
use xpst_config::{Mapping, Value as YamlValue};

/// The captured registry.
const REGISTRY: &str = include_str!("resources/byo_platforms.json");

/// One `ByoPlatformSpec`.
pub struct Spec {
    pub platform: String,
    pub display_name: String,
    pub id_label: String,
    pub secret_label: String,
    pub store_id_key: String,
    pub store_secret_key: String,
    pub redirect_uri: String,
    pub scopes: Vec<String>,
    pub create_url: String,
    pub docs_url: String,
    pub enables: String,
    pub config_id_field: String,
    pub config_secret_field: String,
    pub configurable: bool,
    pub env_id_var: String,
    pub env_secret_var: String,
}

pub struct Registry {
    pub order: Vec<String>,
    pub specs: Vec<Spec>,
    pub meta_redirect_uri: String,
}

impl Registry {
    /// The spec for one platform name, matched the way Python's dict lookup is
    /// (exact key, after the caller lowercases and trims).
    pub fn spec(&self, platform: &str) -> Option<&Spec> {
        self.specs.iter().find(|spec| spec.platform == platform)
    }

    /// The sorted platform list Python prints in an unknown-platform error.
    pub fn options(&self) -> String {
        let mut names: Vec<&str> = self.order.iter().map(String::as_str).collect();
        names.sort_unstable();
        names.join(", ")
    }
}

pub fn registry() -> Result<Registry, String> {
    let document: Value =
        serde_json::from_str(REGISTRY).map_err(|err| format!("embedded registry: {err}"))?;
    let order: Vec<String> = document["order"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let mut specs = Vec::new();
    for name in &order {
        let spec = &document["platforms"][name];
        let text = |field: &str| spec[field].as_str().unwrap_or("").to_string();
        specs.push(Spec {
            platform: text("platform"),
            display_name: text("display_name"),
            id_label: text("id_label"),
            secret_label: text("secret_label"),
            store_id_key: text("store_id_key"),
            store_secret_key: text("store_secret_key"),
            redirect_uri: text("redirect_uri"),
            scopes: spec["scopes"]
                .as_array()
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
            create_url: text("create_url"),
            docs_url: text("docs_url"),
            enables: text("enables"),
            config_id_field: text("config_id_field"),
            config_secret_field: text("config_secret_field"),
            configurable: spec["configurable"].as_bool().unwrap_or(true),
            env_id_var: text("env_id_var"),
            env_secret_var: text("env_secret_var"),
        });
    }
    Ok(Registry {
        order,
        specs,
        meta_redirect_uri: document["meta_redirect_uri"]
            .as_str()
            .unwrap_or("")
            .to_string(),
    })
}

/// `mask_app_id()`: never echo an identifier in full.
pub fn mask_app_id(app_id: &str) -> String {
    if app_id.is_empty() {
        return String::new();
    }
    let chars: Vec<char> = app_id.chars().collect();
    if chars.len() <= 4 {
        return "•".repeat(chars.len());
    }
    format!("…{}", chars[chars.len() - 4..].iter().collect::<String>())
}

/// One field of `accounts.<platform>` in the config document.
///
/// The account sections live under `accounts:` — `XPSTConfig._merge_config`
/// reads `file_config["accounts"][platform]` and exposes it as the model's
/// `<platform>` attribute, which is what `_account_field` then reads. Looking
/// at the top level instead silently reports every platform as unconfigured.
fn account_field(config: &YamlValue, platform: &str, field: &str) -> String {
    config
        .get("accounts")
        .and_then(|accounts| accounts.get(platform))
        .and_then(|section| section.get(field))
        .and_then(YamlValue::as_str)
        .unwrap_or("")
        .to_string()
}

/// A resolved app credential: `(app_id, app_secret, source)`.
pub struct Resolved {
    pub app_id: String,
    pub app_secret: String,
    pub source: &'static str,
}

/// `resolve_byo_app()`.
pub fn resolve(spec: &Spec, config: &YamlValue, store: &CredentialStore) -> Option<Resolved> {
    let stored_id = store.retrieve(&spec.store_id_key).unwrap_or_default();
    let stored_secret = store.retrieve(&spec.store_secret_key).unwrap_or_default();
    if !stored_id.is_empty() && !stored_secret.is_empty() {
        return Some(Resolved {
            app_id: stored_id,
            app_secret: stored_secret,
            source: "encrypted-store",
        });
    }

    // `XPSTConfig.load` applies these env vars to the config fields, so a
    // caller that set them sees the same answer from both engines.
    let env = |name: &str| std::env::var(name).unwrap_or_default();
    let config_id = if spec.config_id_field.is_empty() {
        String::new()
    } else {
        let from_env = env(&spec.env_id_var);
        if from_env.is_empty() {
            account_field(config, &spec.platform, &spec.config_id_field)
        } else {
            from_env
        }
    };
    let config_secret = if spec.config_secret_field.is_empty() {
        String::new()
    } else {
        let from_env = env(&spec.env_secret_var);
        if from_env.is_empty() {
            account_field(config, &spec.platform, &spec.config_secret_field)
        } else {
            from_env
        }
    };

    if !config_id.is_empty() && !config_secret.is_empty() {
        return Some(Resolved {
            app_id: config_id,
            app_secret: config_secret,
            source: "config",
        });
    }
    if !config_id.is_empty() && stored_id.is_empty() {
        return Some(Resolved {
            app_id: config_id,
            app_secret: String::new(),
            source: "config-incomplete",
        });
    }
    None
}

/// `byo_status_for_platform()`.
pub fn status_for(spec: &Spec, config: &YamlValue, store: &CredentialStore) -> YamlValue {
    let resolved = resolve(spec, config, store);
    let configured = resolved
        .as_ref()
        .is_some_and(|resolved| !resolved.app_secret.is_empty());
    let half_configured = resolved
        .as_ref()
        .is_some_and(|resolved| resolved.app_secret.is_empty());
    let app_id_masked = resolved
        .as_ref()
        .map(|resolved| mask_app_id(&resolved.app_id))
        .unwrap_or_default();
    let source = resolved
        .as_ref()
        .map(|resolved| resolved.source)
        .unwrap_or("");
    let env_vars: Vec<YamlValue> = [&spec.env_id_var, &spec.env_secret_var]
        .into_iter()
        .filter(|name| !name.is_empty())
        .map(|name| string(name.clone()))
        .collect();

    object(vec![
        ("platform", string(spec.platform.clone())),
        ("display_name", string(spec.display_name.clone())),
        ("configurable", boolean(spec.configurable)),
        ("configured", boolean(configured)),
        ("half_configured", boolean(half_configured)),
        ("app_id_masked", string(app_id_masked)),
        ("source", string(source)),
        ("id_label", string(spec.id_label.clone())),
        ("secret_label", string(spec.secret_label.clone())),
        ("redirect_uri", string(spec.redirect_uri.clone())),
        (
            "scopes",
            array(
                spec.scopes
                    .iter()
                    .map(|scope| string(scope.clone()))
                    .collect(),
            ),
        ),
        ("create_url", string(spec.create_url.clone())),
        ("docs_url", string(spec.docs_url.clone())),
        ("enables", string(spec.enables.clone())),
        (
            "sign_in_available",
            boolean(configured && spec.configurable),
        ),
        ("env_vars", array(env_vars)),
    ])
}

/// `byo_status_for_platform()` for one platform, or `None` when the platform
/// has no BYO path.
///
/// `xpst providers` embeds this per provider (`byo_app`), so the catalog and
/// `xpst byo status` cannot disagree about which platforms have an app.
pub fn status_for_platform(
    config: &YamlValue,
    platform: &str,
    store: &CredentialStore,
) -> Option<YamlValue> {
    let registry = registry().ok()?;
    let spec = registry.spec(platform)?;
    Some(status_for(spec, config, store))
}

/// `byo_status()`: every BYO platform's status, in registry order.
pub fn status(config: &YamlValue, store: &CredentialStore) -> Result<YamlValue, String> {
    let registry = registry()?;
    let mut platforms = Mapping::new();
    for spec in &registry.specs {
        platforms.insert(
            YamlValue::from(spec.platform.as_str()),
            status_for(spec, config, store),
        );
    }
    Ok(object(vec![
        ("platforms", YamlValue::Mapping(platforms)),
        (
            "meta_redirect_uri",
            string(registry.meta_redirect_uri.clone()),
        ),
        ("note", string(NOTE)),
    ]))
}

/// The note `byo_status()` carries, verbatim.
pub const NOTE: &str = "One Meta app covers Instagram, Threads and Messenger: enter it once and both platforms pick it up. Secrets live only in the encrypted store under the xPST config dir and are never echoed back.";
