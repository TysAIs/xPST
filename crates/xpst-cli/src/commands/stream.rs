//! The `xpst stream` local surface: destinations, app credentials, ingest keys
//! and the MediaMTX relay config.
//!
//! Parity targets: `src/xpst/cli.py::stream_*`, `src/xpst/stream/destinations.py`,
//! `src/xpst/stream/ingest.py` (the credential half) and
//! `src/xpst/stream/relay.py`.
//!
//! Nothing here uploads video or contacts a platform: `destinations` reports
//! which platforms can hand an app an ingest key and which are copy/paste by
//! design, `set-key`/`clear-key` write the encrypted store, and `relay-config`
//! renders the MediaMTX file. The registry data is captured from the shipped
//! Python (`xtask/scripts/capture_stream_registry.py`), so a row that changes
//! there fails the capture instead of drifting here.
//!
//! Two rules carried over from the Python module:
//!
//! * **A key never appears in a payload.** Every serialisable view goes through
//!   `masked()`; the relay YAML is the one place a key is printed, and only when
//!   the operator asked for it on stdout.
//! * **Refuse, never repair.** An ingest URL that is not `rtmp`/`rtmps`, a key
//!   with whitespace or `#`, an unknown platform or a duplicate destination is
//!   an error — a relay that silently drops a destination is worse than one that
//!   refuses to start.
//!
//! **Divergences, recorded in the ledger note:** text mode renders plain lines
//! instead of Rich tables (`--json` is the parity surface), and the interactive
//! prompts (`set-key` without `--ingest-url`/`--key`, `app-credentials` without
//! its flags) read one line from stdin instead of Click's hidden-input prompt.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::Value as Json;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value;

use crate::commands::config_view::{self, ConfigView};
use crate::output::{array, boolean, null, object, string, wrap_at, write_python_json, write_text};
use crate::{exit, flag, leaf_flag, leaf_string, GlobalOptions};

/// Rich's default console width when stdout is not a TTY.
const WIDTH: usize = 80;
/// `APP_CREDENTIAL_PREFIX`.
const APP_CREDENTIAL_PREFIX: &str = "stream_app_";
/// `KEY_PREFIX`.
const KEY_PREFIX: &str = "stream_destination_";
/// `DEFAULT_PATH_NAME`.
const DEFAULT_PATH_NAME: &str = "xpst";
/// `DEFAULT_RTMP_PORT`.
const DEFAULT_RTMP_PORT: i64 = 1935;

/// The captured registry, shared with the tests.
const REGISTRY: &str = include_str!("../resources/stream_registry.json");

/// One `StreamIngestPath` row.
#[derive(Debug, Clone)]
pub struct IngestPath {
    pub platform: String,
    pub display_name: String,
    pub ingest_class: String,
    pub endpoint: Option<String>,
    pub requires: String,
    pub operator_action: String,
    pub source_url: Option<String>,
    pub note: String,
}

impl IngestPath {
    fn api_available(&self) -> bool {
        self.ingest_class == "api"
    }
}

/// One `AppRegistration` row.
#[derive(Debug, Clone)]
pub struct AppRegistration {
    pub platform: String,
    pub display_name: String,
    pub create_url: String,
    pub scopes: Vec<String>,
    pub steps: Vec<String>,
    pub docs_url: String,
}

impl AppRegistration {
    fn to_json(&self) -> Value {
        object(vec![
            ("platform", string(self.platform.clone())),
            ("display_name", string(self.display_name.clone())),
            ("create_url", string(self.create_url.clone())),
            (
                "scopes",
                array(self.scopes.iter().cloned().map(string).collect()),
            ),
            (
                "steps",
                array(self.steps.iter().cloned().map(string).collect()),
            ),
            ("docs_url", string(self.docs_url.clone())),
        ])
    }
}

/// The captured tables.
pub struct Registry {
    pub paths: Vec<IngestPath>,
    pub registrations: Vec<AppRegistration>,
    pub refusals: BTreeMap<String, String>,
}

impl Registry {
    fn path(&self, platform: &str) -> Option<&IngestPath> {
        self.paths.iter().find(|row| row.platform == platform)
    }

    fn registration(&self, platform: &str) -> Option<&AppRegistration> {
        self.registrations
            .iter()
            .find(|row| row.platform == platform)
    }
}

/// Parse the captured registry.
pub fn registry() -> Result<Registry, String> {
    let document: Json = serde_json::from_str(REGISTRY).map_err(|error| error.to_string())?;
    let text = |value: &Json, key: &str| {
        value
            .get(key)
            .and_then(Json::as_str)
            .unwrap_or_default()
            .to_string()
    };
    let optional =
        |value: &Json, key: &str| value.get(key).and_then(Json::as_str).map(str::to_string);
    let list = |value: &Json, key: &str| -> Vec<String> {
        value
            .get(key)
            .and_then(Json::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Json::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };

    let paths: Vec<IngestPath> = document
        .get("paths")
        .and_then(Json::as_array)
        .map(|rows| {
            rows.iter()
                .map(|row| IngestPath {
                    platform: text(row, "platform"),
                    display_name: text(row, "display_name"),
                    ingest_class: text(row, "ingest_class"),
                    endpoint: optional(row, "endpoint"),
                    requires: text(row, "requires"),
                    operator_action: text(row, "operator_action"),
                    source_url: optional(row, "source_url"),
                    note: text(row, "note"),
                })
                .collect()
        })
        .unwrap_or_default();
    if paths.is_empty() {
        return Err("the captured stream registry has no ingest paths".to_string());
    }

    let registrations: Vec<AppRegistration> = document
        .get("app_registrations")
        .and_then(Json::as_array)
        .map(|rows| {
            rows.iter()
                .map(|row| AppRegistration {
                    platform: text(row, "platform"),
                    display_name: text(row, "display_name"),
                    create_url: text(row, "create_url"),
                    scopes: list(row, "scopes"),
                    steps: list(row, "steps"),
                    docs_url: text(row, "docs_url"),
                })
                .collect()
        })
        .unwrap_or_default();

    let mut refusals = BTreeMap::new();
    if let Some(map) = document.get("simulcast_refusals").and_then(Json::as_object) {
        for (key, value) in map {
            if let Some(value) = value.as_str() {
                refusals.insert(key.clone(), value.to_string());
            }
        }
    }

    Ok(Registry {
        paths,
        registrations,
        refusals,
    })
}

// ── app credentials ─────────────────────────────────────────────────────────

/// `AppCredentials`.
#[derive(Debug, Clone, Default)]
pub struct AppCredentials {
    pub platform: String,
    pub client_id: String,
    pub access_token: String,
    pub user_id: String,
    pub source: String,
}

impl AppCredentials {
    fn complete(&self) -> bool {
        !self.client_id.is_empty() && !self.access_token.is_empty()
    }

    fn missing_fields(&self) -> Vec<String> {
        ["client_id", "access_token"]
            .into_iter()
            .filter(|field| match *field {
                "client_id" => self.client_id.is_empty(),
                _ => self.access_token.is_empty(),
            })
            .map(str::to_string)
            .collect()
    }

    /// `masked()`: presence flags plus a tail, never the value.
    fn masked(&self) -> Value {
        object(vec![
            ("platform", string(self.platform.clone())),
            ("client_id", string(mask(&self.client_id))),
            ("access_token", string(mask(&self.access_token))),
            ("user_id", string(self.user_id.clone())),
            ("source", string(self.source.clone())),
            ("complete", boolean(self.complete())),
        ])
    }
}

/// `_mask`: `…` plus the last four characters, or a bare `…` for a short value.
fn mask(secret: &str) -> String {
    if secret.is_empty() {
        return String::new();
    }
    let chars: Vec<char> = secret.chars().collect();
    if chars.len() > 8 {
        format!("…{}", chars[chars.len() - 4..].iter().collect::<String>())
    } else {
        "…".to_string()
    }
}

/// `_env_name`.
fn env_name(platform: &str, name: &str) -> String {
    format!(
        "XPST_STREAM_{}_{}",
        platform.to_uppercase(),
        name.to_uppercase()
    )
}

/// `load_app_credentials`: env first, then the encrypted store.
fn load_app_credentials(
    _dir: &Path,
    platform: &str,
    registry: &Registry,
    store: &CredentialStore,
) -> AppCredentials {
    let name = platform.trim().to_lowercase();
    let payload = if registry.registration(&name).is_some() {
        store
            .retrieve_json(&format!("{APP_CREDENTIAL_PREFIX}{name}"))
            .unwrap_or(Json::Null)
    } else {
        Json::Null
    };

    let mut values = BTreeMap::new();
    let mut used_env = false;
    for field in ["client_id", "access_token", "user_id"] {
        let stored = payload
            .get(field)
            .and_then(Json::as_str)
            .unwrap_or_default()
            .to_string();
        let from_env = std::env::var(env_name(&name, field))
            .map(|value| value.trim().to_string())
            .unwrap_or_default();
        if !from_env.is_empty() {
            used_env = true;
        }
        values.insert(
            field,
            if from_env.is_empty() {
                stored
            } else {
                from_env
            },
        );
    }
    let any = values.values().any(|value| !value.is_empty());
    let source = if used_env {
        "env"
    } else if any {
        "store"
    } else {
        ""
    };
    AppCredentials {
        platform: name,
        client_id: values["client_id"].clone(),
        access_token: values["access_token"].clone(),
        user_id: values["user_id"].clone(),
        source: source.to_string(),
    }
}

/// `store_app_credentials`: validate, then persist.
fn store_app_credentials(
    dir: &Path,
    platform: &str,
    registry: &Registry,
    client_id: &str,
    access_token: &str,
    user_id: &str,
) -> Result<AppCredentials, (String, String)> {
    let name = platform.trim().to_lowercase();
    if registry.registration(&name).is_none() {
        let known = registry
            .registrations
            .iter()
            .map(|row| row.platform.clone())
            .collect::<Vec<_>>()
            .join(", ");
        return Err((
            "not_supported".to_string(),
            format!(
                "{} has no app-registration path in xPST (known: {known})",
                python_repr(platform)
            ),
        ));
    }
    if client_id.trim().is_empty() {
        return Err((
            "credentials_missing".to_string(),
            "client id is empty".to_string(),
        ));
    }
    if access_token.trim().is_empty() {
        return Err((
            "credentials_missing".to_string(),
            "access token is empty".to_string(),
        ));
    }
    let payload = serde_json::json!({
        "client_id": client_id.trim(),
        "access_token": access_token.trim(),
        "user_id": user_id.trim(),
    });
    CredentialStore::new(dir)
        .store_json(&format!("{APP_CREDENTIAL_PREFIX}{name}"), &payload)
        .map_err(|error| ("store_error".to_string(), error))?;
    Ok(AppCredentials {
        platform: name,
        client_id: client_id.trim().to_string(),
        access_token: access_token.trim().to_string(),
        user_id: user_id.trim().to_string(),
        source: "store".to_string(),
    })
}

/// `clear_app_credentials`.
fn clear_app_credentials(dir: &Path, platform: &str) -> bool {
    CredentialStore::new(dir).delete(&format!(
        "{APP_CREDENTIAL_PREFIX}{}",
        platform.trim().to_lowercase()
    ))
}

// ── readiness ───────────────────────────────────────────────────────────────

/// `FetchReadiness`.
struct FetchReadiness {
    platform: String,
    endpoint: String,
    credentials_present: bool,
    missing: Vec<String>,
    action: String,
    registration_url: String,
    scopes: Vec<String>,
}

impl FetchReadiness {
    fn to_json(&self) -> Value {
        object(vec![
            ("platform", string(self.platform.clone())),
            ("endpoint", string(self.endpoint.clone())),
            ("credentials_present", boolean(self.credentials_present)),
            (
                "missing",
                array(self.missing.iter().cloned().map(string).collect()),
            ),
            ("action", string(self.action.clone())),
            ("registration_url", string(self.registration_url.clone())),
            (
                "scopes",
                array(self.scopes.iter().cloned().map(string).collect()),
            ),
        ])
    }
}

/// `_missing_action`.
fn missing_action(registry: &Registry, platform: &str) -> (String, String, Vec<String>) {
    if let Some(registration) = registry.registration(platform) {
        return (
            registration.steps.join(" "),
            registration.create_url.clone(),
            registration.scopes.clone(),
        );
    }
    if platform == "youtube" {
        return (
            "Connect the destination channel with `xpst auth youtube` \
             (OAuth scopes youtube.upload, youtube.readonly, youtube.force-ssl)."
                .to_string(),
            "https://console.cloud.google.com/apis/credentials".to_string(),
            Vec::new(),
        );
    }
    if platform == "facebook" {
        return (
            "Connect the Page with `xpst auth facebook`; only a Page access token can \
             create a Page live video."
                .to_string(),
            "https://developers.facebook.com/docs/pages/access-tokens".to_string(),
            Vec::new(),
        );
    }
    match registry.path(platform) {
        Some(row) => (
            row.operator_action.clone(),
            row.source_url.clone().unwrap_or_default(),
            Vec::new(),
        ),
        None => (String::new(), String::new(), Vec::new()),
    }
}

/// `fetch_readiness`: the credential state that gates a fetch, no network.
fn fetch_readiness(
    dir: &Path,
    platform: &str,
    registry: &Registry,
    store: &CredentialStore,
    view: &ConfigView,
) -> FetchReadiness {
    let name = platform.trim().to_lowercase();
    let row = registry.path(&name);
    let endpoint = row.and_then(|row| row.endpoint.clone()).unwrap_or_default();
    let (action, registration_url, scopes) = missing_action(registry, &name);

    let missing: Vec<String> = if row.is_some_and(|row| row.ingest_class == "manual") {
        vec!["manual_handoff".to_string()]
    } else if row.is_some_and(|row| !row.api_available()) {
        vec!["no_ingest".to_string()]
    } else if registry.registration(&name).is_some() {
        let credentials = load_app_credentials(dir, &name, registry, store);
        let mut missing = credentials.missing_fields();
        if credentials.user_id.is_empty() {
            missing.push("user_id".to_string());
        }
        missing
    } else if name == "facebook" {
        if facebook_page_token(store) {
            Vec::new()
        } else {
            vec!["page_access_token".to_string()]
        }
    } else if name == "youtube" {
        if youtube_token_present(dir, store, view) {
            Vec::new()
        } else {
            vec!["oauth_token".to_string()]
        }
    } else {
        vec!["endpoint".to_string()]
    };

    FetchReadiness {
        platform: name,
        endpoint,
        credentials_present: missing.is_empty(),
        missing,
        action,
        registration_url,
        scopes,
    }
}

/// `_facebook_page_token`: both the Page id and its token must be stored.
fn facebook_page_token(store: &CredentialStore) -> bool {
    let page_id = store.retrieve("facebook_page_id").unwrap_or_default();
    let token = store.retrieve("facebook_page_token").unwrap_or_default();
    !page_id.is_empty() && !token.is_empty()
}

/// `_youtube_token_present`: the store, the configured token file, or the
/// default-named files in the config directory.
fn youtube_token_present(dir: &Path, store: &CredentialStore, view: &ConfigView) -> bool {
    if store
        .retrieve("youtube_token")
        .is_some_and(|token| !token.is_empty())
    {
        return true;
    }
    let token_file = view.text("youtube", "token_file").unwrap_or_default();
    if !token_file.is_empty() && Path::new(&token_file).is_file() {
        return true;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return false;
    };
    entries.flatten().any(|entry| {
        let name = entry.file_name().to_string_lossy().to_string();
        name.starts_with("youtube") && name.contains("_token") && name.ends_with(".json")
    })
}

// ── relay ───────────────────────────────────────────────────────────────────

/// `RelayDestination`.
#[derive(Debug, Clone)]
pub struct RelayDestination {
    pub platform: String,
    pub ingest_url: String,
    pub stream_key: String,
}

impl RelayDestination {
    /// `__post_init__`: validate, never repair.
    fn new(
        registry: &Registry,
        platform: &str,
        ingest_url: &str,
        stream_key: &str,
    ) -> Result<Self, String> {
        let name = platform.trim().to_lowercase();
        let Some(row) = registry.path(&name) else {
            let mut known: Vec<String> = registry
                .paths
                .iter()
                .map(|row| row.platform.clone())
                .collect();
            known.sort();
            return Err(format!(
                "{} is not a registered stream destination (known: {})",
                python_repr(platform),
                known.join(", ")
            ));
        };
        if row.ingest_class == "unsupported" {
            return Err(format!("{name} has no live ingest path ({})", row.note));
        }
        if let Some(reason) = registry.refusals.get(&name) {
            return Err(format!("{name} cannot be a relay destination: {reason}"));
        }

        let ingest_url = ingest_url.trim().to_string();
        let scheme = ingest_url.split("://").next().unwrap_or_default();
        if !ingest_url.contains("://") || !matches!(scheme, "rtmp" | "rtmps") {
            return Err(format!(
                "{name}: ingest URL must start with rtmp:// or rtmps:// (got {})",
                if ingest_url.contains("://") {
                    scheme.to_string()
                } else {
                    "no scheme".to_string()
                }
            ));
        }
        let authority = ingest_url
            .split("://")
            .nth(1)
            .unwrap_or_default()
            .split('/')
            .next()
            .unwrap_or_default();
        if authority.is_empty() {
            return Err(format!("{name}: ingest URL has no host"));
        }

        let key = stream_key.trim().to_string();
        if key.is_empty() {
            return Err(format!("{name}: stream key is empty"));
        }
        if key.chars().any(char::is_whitespace) {
            return Err(format!("{name}: stream key contains whitespace"));
        }
        if key.contains('#') {
            return Err(format!(
                "{name}: stream key contains '#', which MediaMTX uses to separate the URL \
                 from the key — paste the key without it"
            ));
        }

        Ok(Self {
            platform: name,
            ingest_url,
            stream_key: key,
        })
    }

    /// `forward_url`.
    fn forward_url(&self) -> String {
        format!("{}#{}", self.ingest_url, self.stream_key)
    }

    /// `masked()`.
    fn masked(&self) -> Value {
        let chars: Vec<char> = self.stream_key.chars().collect();
        let masked = if chars.len() > 6 {
            format!(
                "{}…{}",
                chars[..2].iter().collect::<String>(),
                chars[chars.len() - 2..].iter().collect::<String>()
            )
        } else {
            "…".to_string()
        };
        object(vec![
            ("platform", string(self.platform.clone())),
            ("ingest_url", string(self.ingest_url.clone())),
            ("stream_key", string(masked)),
        ])
    }
}

/// `load_destinations`: every stored destination, in registry order.
fn load_destinations(dir: &Path, registry: &Registry) -> Result<Vec<RelayDestination>, String> {
    let store = CredentialStore::new(dir);
    let mut stored: Vec<RelayDestination> = Vec::new();
    for key in store.list_keys() {
        let Some(platform) = key.strip_prefix(KEY_PREFIX) else {
            continue;
        };
        let payload = store.retrieve_json(&key).unwrap_or(Json::Null);
        let ingest_url = payload
            .get("ingest_url")
            .and_then(Json::as_str)
            .unwrap_or_default();
        let stream_key = payload
            .get("stream_key")
            .and_then(Json::as_str)
            .unwrap_or_default();
        let destination = RelayDestination::new(registry, platform, ingest_url, stream_key)
            .map_err(|error| {
                format!(
                    "stored destination {platform:?} is unusable: {error}. \
                     Re-add it with `xpst stream set-key {platform}`."
                )
            })?;
        stored.push(destination);
    }
    let order: Vec<&str> = registry
        .paths
        .iter()
        .map(|row| row.platform.as_str())
        .collect();
    stored.sort_by_key(|destination| {
        order
            .iter()
            .position(|name| *name == destination.platform)
            .unwrap_or(order.len())
    });
    Ok(stored)
}

/// `build_relay_config`.
fn build_relay_config(
    destinations: &[RelayDestination],
    path_name: &str,
    rtmp_port: i64,
) -> Result<Value, String> {
    if destinations.is_empty() {
        return Err("no destinations configured — nothing to forward to. \
                    Add one with `xpst stream set-key <platform>`."
            .to_string());
    }
    if !(1..=65535).contains(&rtmp_port) {
        return Err(format!("rtmp port {rtmp_port} is out of range"));
    }
    if path_name.is_empty()
        || !path_name
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_alphanumeric())
        || !path_name.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        })
    {
        return Err(format!(
            "path name {path_name:?} must match ^[A-Za-z0-9][A-Za-z0-9._-]*$ \
             (it is part of the relay URL the encoder publishes to)"
        ));
    }

    let mut seen: BTreeMap<String, String> = BTreeMap::new();
    let mut forward: Vec<Value> = Vec::new();
    for destination in destinations {
        if let Some(previous) = seen.get(&destination.platform) {
            return Err(format!(
                "{} is configured twice ({previous} and {}); one platform may only forward once",
                destination.platform, destination.ingest_url
            ));
        }
        seen.insert(destination.platform.clone(), destination.ingest_url.clone());
        forward.push(object(vec![("dest", string(destination.forward_url()))]));
    }

    let mut path = xpst_config::Mapping::new();
    path.insert(Value::from("source"), Value::from("publisher"));
    path.insert(Value::from("forward"), Value::Sequence(forward));
    let mut paths = xpst_config::Mapping::new();
    paths.insert(Value::from(path_name), Value::Mapping(path));

    Ok(object(vec![
        ("logLevel", string("info")),
        ("rtmp", boolean(true)),
        ("rtmpAddress", string(format!(":{rtmp_port}"))),
        ("rtsp", boolean(false)),
        ("hls", boolean(false)),
        ("webrtc", boolean(false)),
        ("srt", boolean(false)),
        ("moq", boolean(false)),
        ("api", boolean(false)),
        ("paths", Value::Mapping(paths)),
    ]))
}

/// `render_relay_config`.
fn render_relay_config(config: &Value) -> String {
    let header = "# Generated by xPST — live-stream relay (MediaMTX).\n\
                  # This file contains stream keys. Keep it mode 0600 and never commit it.\n\
                  # Encoder setup: push one RTMP stream to rtmp://127.0.0.1:1935/<path>.\n";
    format!("{header}{}", xpst_config::pyyaml::dump(config))
}

/// `relay_summary`: key-free description, safe to print or log.
fn relay_summary(destinations: &[RelayDestination], path_name: &str, rtmp_port: i64) -> Value {
    let rows: Vec<Value> = destinations.iter().map(RelayDestination::masked).collect();
    object(vec![
        ("ok", boolean(!rows.is_empty())),
        (
            "relay_ingest_url",
            string(format!("rtmp://127.0.0.1:{rtmp_port}/{path_name}")),
        ),
        (
            "destination_count",
            Value::Number((rows.len() as i64).into()),
        ),
        ("destinations", array(rows)),
        (
            "note",
            string(
                "Each destination receives the same bytes; nothing is re-encoded. \
                 A platform that issues per-session keys (Instagram, TikTok, Rumble) \
                 must be re-pasted before every stream.",
            ),
        ),
    ])
}

// ── handlers ────────────────────────────────────────────────────────────────

fn json_mode(global: &GlobalOptions, leaf: Option<&ArgMatches>) -> bool {
    global.json || leaf.is_some_and(|m| flag(m, "opt:json"))
}

fn resolve(
    global: &GlobalOptions,
    json: bool,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> Result<(ConfigView, PathBuf), i32> {
    match config_view::resolve(global) {
        Ok(view) => {
            let dir = view.config_dir().to_path_buf();
            Ok((view, dir))
        }
        Err(error) => Err(crate::commands::bootstrap::report(err, json, out, &error)),
    }
}

/// `xpst stream destinations`.
pub fn destinations(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let (view, dir) = match resolve(global, json, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };
    let registry = match registry() {
        Ok(registry) => registry,
        Err(error) => {
            let _ = writeln!(err, "xpst stream destinations: {error}");
            return exit::GENERAL;
        }
    };

    let (stored, error) = match load_destinations(&dir, &registry) {
        Ok(stored) => (stored, None),
        Err(error) => (Vec::new(), Some(error)),
    };
    let store = CredentialStore::new(&dir);

    struct Row {
        platform: String,
        display_name: String,
        ingest_class: String,
        key_present: bool,
        ingest_url: Option<String>,
        operator_action: String,
        note: String,
        fetch: Option<FetchReadiness>,
    }

    let mut rows: Vec<Row> = Vec::new();
    for path in &registry.paths {
        let configured = stored.iter().find(|row| row.platform == path.platform);
        let readiness = if path.api_available() {
            Some(fetch_readiness(
                &dir,
                &path.platform,
                &registry,
                &store,
                &view,
            ))
        } else {
            None
        };
        rows.push(Row {
            platform: path.platform.clone(),
            display_name: path.display_name.clone(),
            ingest_class: path.ingest_class.clone(),
            key_present: configured.is_some(),
            ingest_url: configured.map(|row| row.ingest_url.clone()),
            operator_action: path.operator_action.clone(),
            note: path.note.clone(),
            fetch: readiness,
        });
    }

    let configured: Vec<Value> = rows
        .iter()
        .filter(|row| row.key_present)
        .map(|row| string(row.platform.clone()))
        .collect();
    let fetchable: Vec<Value> = rows
        .iter()
        .filter(|row| {
            row.fetch
                .as_ref()
                .is_some_and(|fetch| fetch.credentials_present)
        })
        .map(|row| string(row.platform.clone()))
        .collect();
    let missing_credentials: Vec<Value> = rows
        .iter()
        .filter(|row| {
            row.fetch
                .as_ref()
                .is_some_and(|fetch| !fetch.credentials_present)
        })
        .map(|row| {
            let fetch = row.fetch.as_ref().expect("filtered on Some");
            object(vec![
                ("platform", string(row.platform.clone())),
                (
                    "missing",
                    array(fetch.missing.iter().cloned().map(string).collect()),
                ),
                ("action", string(fetch.action.clone())),
                ("registration_url", string(fetch.registration_url.clone())),
            ])
        })
        .collect();

    let platform_rows: Vec<Value> = rows
        .iter()
        .map(|row| {
            object(vec![
                ("platform", string(row.platform.clone())),
                ("display_name", string(row.display_name.clone())),
                ("ingest_class", string(row.ingest_class.clone())),
                ("key_present", boolean(row.key_present)),
                (
                    "ingest_url",
                    row.ingest_url.clone().map_or_else(null, string),
                ),
                ("operator_action", string(row.operator_action.clone())),
                ("note", string(row.note.clone())),
                (
                    "fetch",
                    row.fetch
                        .as_ref()
                        .map_or_else(null, FetchReadiness::to_json),
                ),
            ])
        })
        .collect();

    let payload = object(vec![
        ("ok", boolean(error.is_none())),
        (
            "error",
            error
                .as_ref()
                .map_or_else(null, |text| string(text.clone())),
        ),
        ("configured", array(configured)),
        (
            "ready_to_relay",
            boolean(!stored.is_empty() && error.is_none()),
        ),
        ("fetchable", array(fetchable)),
        ("missing_credentials", array(missing_credentials)),
        ("platforms", array(platform_rows)),
    ]);

    if json {
        let _ = write_python_json(out, &payload);
        if error.is_some() {
            return exit::CONFIG_ERROR;
        }
        return exit::SUCCESS;
    }

    let mut text = String::from("Live stream destinations\n");
    for row in &rows {
        if row.ingest_class == "unsupported" {
            continue;
        }
        let key = if row.key_present { "stored" } else { "—" };
        let fetch = match &row.fetch {
            None => "—".to_string(),
            Some(fetch) => {
                if fetch.credentials_present {
                    "ready".to_string()
                } else {
                    fetch.missing.join(", ")
                }
            }
        };
        text.push_str(&format!(
            "  {} | {} | key {} | fetch {}\n",
            row.display_name, row.ingest_class, key, fetch
        ));
    }
    if let Some(error) = &error {
        text.push_str(&format!("{error}\n"));
    }
    let _ = write_text(out, &text);
    if error.is_some() {
        return exit::CONFIG_ERROR;
    }
    exit::SUCCESS
}

/// `xpst stream app-credentials PLATFORM`.
pub fn app_credentials(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let registry = match registry() {
        Ok(registry) => registry,
        Err(error) => {
            let _ = writeln!(err, "xpst stream app-credentials: {error}");
            return exit::GENERAL;
        }
    };
    let platform = leaf_string(leaf, "arg:platform").unwrap_or_default();
    let name = platform.trim().to_lowercase();
    let Some(spec) = registry.registration(&name) else {
        let mut known: Vec<String> = registry
            .registrations
            .iter()
            .map(|row| row.platform.clone())
            .collect();
        known.sort();
        let message = format!(
            "{} does not use xPST app credentials (known: {})",
            python_repr(&platform),
            known.join(", ")
        );
        if json {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("code", string("not_supported")),
                ("message", string(message.clone())),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, &message);
            let _ = write_text(
                out,
                "YouTube reuses `xpst auth youtube`; Facebook reuses `xpst auth facebook`.",
            );
        }
        return exit::CONFIG_ERROR;
    };

    let (view, dir) = match resolve(global, json, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };
    let _ = view;
    let store = CredentialStore::new(&dir);

    if leaf_flag(leaf, "opt:clear") {
        let removed = clear_app_credentials(&dir, &name);
        if json {
            let payload = object(vec![
                ("ok", boolean(removed)),
                ("platform", string(name.clone())),
                ("removed", boolean(removed)),
            ]);
            let _ = write_python_json(out, &payload);
            return exit::SUCCESS;
        }
        let _ = write_text(
            out,
            &if removed {
                format!("Removed the stored {} app credentials.", spec.display_name)
            } else {
                format!("No stored {} app credentials.", spec.display_name)
            },
        );
        return exit::SUCCESS;
    }

    let client_id = leaf_string(leaf, "opt:client-id");
    let access_token = leaf_string(leaf, "opt:access-token");
    let user_id = leaf_string(leaf, "opt:user-id").unwrap_or_default();

    if client_id.is_none() && access_token.is_none() {
        let current = load_app_credentials(&dir, &name, &registry, &store);
        if json {
            let payload = object(vec![
                ("ok", boolean(true)),
                ("credentials", current.masked()),
                ("registration", spec.to_json()),
            ]);
            let _ = write_python_json(out, &payload);
            return exit::SUCCESS;
        }
        let mut text = format!(
            "{} app credentials: {}\n",
            spec.display_name,
            if current.complete() && !current.user_id.is_empty() {
                "stored"
            } else {
                "incomplete"
            }
        );
        text.push_str(&format!("  client id:     {}\n", mask(&current.client_id)));
        text.push_str(&format!(
            "  access token:  {}\n",
            mask(&current.access_token)
        ));
        text.push_str(&format!(
            "  user id:       {}\n",
            if current.user_id.is_empty() {
                "—".to_string()
            } else {
                current.user_id.clone()
            }
        ));
        if !current.source.is_empty() {
            text.push_str(&format!("  source:        {}\n", current.source));
        }
        text.push_str("Registration steps\n");
        for step in &spec.steps {
            text.push_str(&format!("  • {step}\n"));
        }
        text.push_str(&format!("  scopes: {}\n", spec.scopes.join(", ")));
        let _ = write_text(out, &text);
        return exit::SUCCESS;
    }

    let resolved_id =
        client_id.unwrap_or_else(|| prompt(&format!("{} client id", spec.display_name)));
    let resolved_token = access_token.unwrap_or_else(|| prompt("Access token"));
    let resolved_user = if user_id.is_empty() {
        prompt(&format!("{} broadcaster/channel id", spec.display_name))
    } else {
        user_id
    };

    match store_app_credentials(
        &dir,
        &name,
        &registry,
        &resolved_id,
        &resolved_token,
        &resolved_user,
    ) {
        Ok(stored) => {
            if json {
                let mut ordered = xpst_config::Mapping::new();
                ordered.insert(Value::from("ok"), boolean(true));
                if let Value::Mapping(map) = stored.masked() {
                    for (key, value) in map {
                        ordered.insert(key, value);
                    }
                }
                let _ = write_python_json(out, &Value::Mapping(ordered));
                return exit::SUCCESS;
            }
            let _ = write_text(
                out,
                &format!(
                    "Stored {} app credentials (encrypted at rest)",
                    spec.display_name
                ),
            );
            let _ = write_text(out, &format!("Try `xpst stream fetch-key {name}`."));
            exit::SUCCESS
        }
        Err((code, message)) => {
            if json {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("code", string(code)),
                    ("platform", string(name.clone())),
                    ("message", string(message.clone())),
                    ("action", string("")),
                    ("docs_url", string("")),
                    ("status", null()),
                ]);
                let _ = write_python_json(out, &payload);
            } else {
                let _ = write_text(out, &message);
            }
            exit::CONFIG_ERROR
        }
    }
}

/// Python's `repr()` for a short string: single quotes unless the value itself
/// contains one.
fn python_repr(text: &str) -> String {
    if text.contains('\'') && !text.contains('"') {
        format!("\"{text}\"")
    } else {
        format!("'{text}'")
    }
}

/// One line from stdin, trimmed (Click's `prompt`).
fn prompt(label: &str) -> String {
    let mut stdout = std::io::stdout();
    let _ = write!(stdout, "{label}: ");
    let _ = stdout.flush();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
    line.trim().to_string()
}

/// `xpst stream set-key PLATFORM`.
pub fn set_key(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let registry = match registry() {
        Ok(registry) => registry,
        Err(error) => {
            let _ = writeln!(err, "xpst stream set-key: {error}");
            return exit::GENERAL;
        }
    };
    let platform = leaf_string(leaf, "arg:platform").unwrap_or_default();
    let name = platform.trim().to_lowercase();
    let Some(row) = registry.path(&name) else {
        let mut known: Vec<String> = registry
            .paths
            .iter()
            .map(|row| row.platform.clone())
            .collect();
        known.sort();
        let _ = write_text(
            out,
            &wrap_at(
                &format!(
                    "Unknown stream platform '{platform}'. Known: {}",
                    known.join(", ")
                ),
                WIDTH,
            ),
        );
        return exit::CONFIG_ERROR;
    };
    if row.ingest_class == "unsupported" {
        let _ = write_text(
            out,
            &format!("{} has no live ingest path: {}", row.display_name, row.note),
        );
        return exit::CONFIG_ERROR;
    }

    let (_, dir) = match resolve(global, json, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };

    let ingest_url = leaf_string(leaf, "opt:ingest-url")
        .unwrap_or_else(|| prompt(&format!("{} ingest URL", row.display_name)));
    let stream_key = leaf_string(leaf, "opt:key").unwrap_or_else(|| prompt("Stream key"));

    let destination = match RelayDestination::new(&registry, &name, &ingest_url, &stream_key) {
        Ok(destination) => destination,
        Err(error) => {
            if json {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("error", string(error.clone())),
                    ("platform", string(platform.clone())),
                ]);
                let _ = write_python_json(out, &payload);
            }
            let _ = write_text(out, &wrap_at(&error, WIDTH));
            return exit::CONFIG_ERROR;
        }
    };

    let payload = serde_json::json!({
        "ingest_url": destination.ingest_url,
        "stream_key": destination.stream_key,
    });
    if let Err(error) = CredentialStore::new(&dir)
        .store_json(&format!("{KEY_PREFIX}{}", destination.platform), &payload)
    {
        if json {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("error", string(error.clone())),
                ("platform", string(platform.clone())),
            ]);
            let _ = write_python_json(out, &payload);
        }
        let _ = write_text(out, &error);
        return exit::CONFIG_ERROR;
    }

    if json {
        let mut ordered = xpst_config::Mapping::new();
        ordered.insert(Value::from("ok"), boolean(true));
        if let Value::Mapping(map) = destination.masked() {
            for (key, value) in map {
                ordered.insert(key, value);
            }
        }
        let _ = write_python_json(out, &Value::Mapping(ordered));
        return exit::SUCCESS;
    }
    let _ = write_text(
        out,
        &format!(
            "Stored {} ingest credentials ({})",
            row.display_name, destination.ingest_url
        ),
    );
    if row.ingest_class == "manual" {
        let _ = write_text(
            out,
            "This platform issues a per-session key — re-run this command before every stream.",
        );
    }
    exit::SUCCESS
}

/// `xpst stream clear-key PLATFORM`.
pub fn clear_key(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let (_, dir) = match resolve(global, json, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };
    let platform = leaf_string(leaf, "arg:platform").unwrap_or_default();
    let removed = CredentialStore::new(&dir)
        .delete(&format!("{KEY_PREFIX}{}", platform.trim().to_lowercase()));
    if json {
        let payload = object(vec![
            ("ok", boolean(removed)),
            ("platform", string(platform.clone())),
            ("removed", boolean(removed)),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }
    let _ = write_text(
        out,
        &if removed {
            format!("Removed the stored {platform} ingest key.")
        } else {
            format!("No stored ingest key for {platform}.")
        },
    );
    exit::SUCCESS
}

/// `xpst stream relay-config`.
pub fn relay_config(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let (_, dir) = match resolve(global, json, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };
    let registry = match registry() {
        Ok(registry) => registry,
        Err(error) => {
            let _ = writeln!(err, "xpst stream relay-config: {error}");
            return exit::GENERAL;
        }
    };
    let path_name =
        leaf_string(leaf, "opt:path-name").unwrap_or_else(|| DEFAULT_PATH_NAME.to_string());
    let rtmp_port = leaf_string(leaf, "opt:rtmp-port")
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(DEFAULT_RTMP_PORT);
    let output = leaf_string(leaf, "opt:output").filter(|value| !value.is_empty());

    let built = load_destinations(&dir, &registry).and_then(|destinations| {
        if let Some(output) = &output {
            let config = build_relay_config(&destinations, &path_name, rtmp_port)?;
            let text = render_relay_config(&config);
            let target = crate::commands::state::expanduser(output);
            if let Some(parent) = target.parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
                }
            }
            crate::fsutil::write_private(&target, text.as_bytes())?;
            let mut summary = relay_summary(&destinations, &path_name, rtmp_port);
            if let Value::Mapping(map) = &mut summary {
                map.insert(Value::from("output"), string(target.display().to_string()));
            }
            return Ok((summary, None));
        }
        let config = build_relay_config(&destinations, &path_name, rtmp_port)?;
        let rendered = render_relay_config(&config);
        Ok((
            relay_summary(&destinations, &path_name, rtmp_port),
            Some(rendered),
        ))
    });

    match built {
        Ok((summary, rendered)) => {
            if json {
                let mut payload = summary;
                if let (Some(rendered), Value::Mapping(map)) = (rendered, &mut payload) {
                    map.insert(Value::from("config"), string(rendered));
                }
                let _ = write_python_json(out, &payload);
                return exit::SUCCESS;
            }
            match rendered {
                Some(rendered) => {
                    let _ = write_text(out, &rendered);
                }
                None => {
                    let target = summary
                        .get("output")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    let count = summary
                        .get("destination_count")
                        .and_then(Value::as_i64)
                        .unwrap_or_default();
                    let _ = write_text(
                        out,
                        &format!("Wrote the relay config to {target} ({count} destination(s), mode 0600)"),
                    );
                    let _ = write_text(
                        out,
                        &format!(
                            "Encoder: push RTMP to {}",
                            summary
                                .get("relay_ingest_url")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                        ),
                    );
                }
            }
            exit::SUCCESS
        }
        Err(error) => {
            if json {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("error", string(error.clone())),
                ]);
                let _ = write_python_json(out, &payload);
            }
            let _ = write_text(out, &wrap_at(&error, WIDTH));
            exit::CONFIG_ERROR
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_registry_carries_every_ingest_path() {
        let registry = registry().expect("registry parses");
        assert_eq!(registry.paths.len(), 10);
        assert_eq!(registry.paths[0].platform, "youtube");
        assert_eq!(registry.registrations.len(), 2);
        assert_eq!(registry.refusals.len(), 2);
    }

    #[test]
    fn a_destination_refuses_a_non_rtmp_url() {
        let registry = registry().unwrap();
        let error = RelayDestination::new(&registry, "youtube", "https://x/y", "k").unwrap_err();
        assert!(error.contains("rtmp://"), "{error}");
    }

    #[test]
    fn a_destination_refuses_a_key_with_a_hash() {
        let registry = registry().unwrap();
        let error =
            RelayDestination::new(&registry, "youtube", "rtmps://a/live2", "k#1").unwrap_err();
        assert!(error.contains('#'), "{error}");
    }

    #[test]
    fn an_unsupported_platform_is_refused() {
        let registry = registry().unwrap();
        let error = RelayDestination::new(&registry, "x", "rtmp://a/b", "k").unwrap_err();
        assert!(error.contains("no live ingest path"), "{error}");
    }

    #[test]
    fn the_relay_config_is_the_documented_shape() {
        let registry = registry().unwrap();
        let destination =
            RelayDestination::new(&registry, "youtube", "rtmps://a.example/live2", "secret")
                .unwrap();
        let config = build_relay_config(&[destination], "xpst", 1935).unwrap();
        let rendered = render_relay_config(&config);
        assert!(rendered.starts_with("# Generated by xPST"));
        assert!(
            rendered.contains("rtmpAddress: ':1935'") || rendered.contains("rtmpAddress: :1935")
        );
        assert!(rendered.contains("dest: rtmps://a.example/live2#secret"));
        assert!(rendered.contains("rtsp: false"));
    }

    #[test]
    fn the_summary_never_carries_the_key() {
        let registry = registry().unwrap();
        let destination = RelayDestination::new(
            &registry,
            "youtube",
            "rtmps://a.example/live2",
            "secret-key",
        )
        .unwrap();
        let summary = relay_summary(&[destination], "xpst", 1935);
        let text = xpst_config::python_style_json(&summary).unwrap();
        assert!(!text.contains("secret-key"), "{text}");
        assert!(text.contains("se…ey"), "{text}");
    }

    #[test]
    fn an_empty_destination_list_is_refused() {
        let error = build_relay_config(&[], "xpst", 1935).unwrap_err();
        assert!(error.contains("no destinations configured"), "{error}");
    }
}
