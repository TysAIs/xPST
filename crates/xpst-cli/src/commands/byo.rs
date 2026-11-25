//! `xpst byo status` and `xpst byo clear` — bring-your-own developer app.
//!
//! Parity target: `src/xpst/byo.py` (`byo_status`, `byo_status_for_platform`,
//! `resolve_byo_app`, `clear_byo_app`, `mask_app_id`) and the two Click
//! commands that wrap them.
//!
//! The registry (`BYO_PLATFORMS`) is captured, not transcribed:
//! `xtask/scripts/capture_byo_platforms.py` runs the shipped Python and writes
//! `resources/byo_platforms.json`, because a wrong `store_id_key` in a
//! hand-copied table reads as "no app configured" forever and nothing would
//! catch it. `tests/local_surface.rs` diffs the whole `byo status` payload
//! against a live `python -m xpst byo status --json`.
//!
//! Credential resolution order is Python's: the encrypted store first, then the
//! config fields (with the `XPST_*_APP_ID`/`_APP_SECRET` environment overrides
//! `XPSTConfig.load` applies), then "id without secret" as `config-incomplete`.
//!
//! **Divergences, both recorded in the ledger note:**
//!
//! * The OS keyring is not consulted. Python's store is keyring-first only when
//!   `XPST_USE_KEYRING=1` (opt-in, off by default); with it set, a credential
//!   that lives in the keychain reads as absent here.
//! * `byo clear` clears the two fields in the raw document instead of
//!   rewriting the whole file from the Pydantic model, so the file differs from
//!   Python's while the payload does not.

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use serde_json::Value;
use xpst_auth::credentials::CredentialStore;
use xpst_config::{Mapping, Value as YamlValue};

use crate::commands::bootstrap;
use crate::output::{array, boolean, object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// The captured registry.
const REGISTRY: &str = include_str!("../resources/byo_platforms.json");

/// One `ByoPlatformSpec`.
struct Spec {
    platform: String,
    display_name: String,
    id_label: String,
    secret_label: String,
    store_id_key: String,
    store_secret_key: String,
    redirect_uri: String,
    scopes: Vec<String>,
    create_url: String,
    docs_url: String,
    enables: String,
    config_id_field: String,
    config_secret_field: String,
    configurable: bool,
    env_id_var: String,
    env_secret_var: String,
}

struct Registry {
    order: Vec<String>,
    specs: Vec<Spec>,
    meta_redirect_uri: String,
}

fn registry() -> Result<Registry, String> {
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
fn mask_app_id(app_id: &str) -> String {
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
struct Resolved {
    app_id: String,
    app_secret: String,
    source: &'static str,
}

/// `resolve_byo_app()`.
fn resolve(spec: &Spec, config: &YamlValue, store: &CredentialStore) -> Option<Resolved> {
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
fn status_for(spec: &Spec, config: &YamlValue, store: &CredentialStore) -> YamlValue {
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
pub(crate) fn status_for_platform(
    config: &YamlValue,
    platform: &str,
    store: &CredentialStore,
) -> Option<YamlValue> {
    let registry = registry().ok()?;
    let spec = registry
        .specs
        .iter()
        .find(|spec| spec.platform == platform)?;
    Some(status_for(spec, config, store))
}

/// The note `byo_status()` carries, verbatim.
const NOTE: &str = "One Meta app covers Instagram, Threads and Messenger: enter it once and both platforms pick it up. Secrets live only in the encrypted store under the xPST config dir and are never echoed back.";

/// Resolve the profile and its config document.
fn load(
    global: &GlobalOptions,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> Result<(std::path::PathBuf, YamlValue), i32> {
    let (config_path, document) = match bootstrap::load_document(global) {
        Ok(loaded) => loaded,
        Err(error) => return Err(bootstrap::report(out, err, &error)),
    };
    let document = document.raw().clone();
    let dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    Ok((dir, document))
}

/// `xpst byo status`.
pub fn status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let registry = match registry() {
        Ok(registry) => registry,
        Err(message) => {
            let _ = writeln!(err, "xpst byo status: {message}");
            return exit::GENERAL;
        }
    };
    let (dir, config) = match load(global, out, err) {
        Ok(loaded) => loaded,
        Err(code) => return code,
    };
    let store = CredentialStore::new(&dir);

    let mut platforms = Mapping::new();
    for spec in &registry.specs {
        platforms.insert(
            YamlValue::from(spec.platform.as_str()),
            status_for(spec, &config, &store),
        );
    }

    if json_mode {
        let payload = object(vec![
            ("platforms", YamlValue::Mapping(platforms)),
            (
                "meta_redirect_uri",
                string(registry.meta_redirect_uri.clone()),
            ),
            ("note", string(NOTE)),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let mut text = String::from("Bring-your-own app setup\n");
    for spec in &registry.specs {
        let status = status_for(spec, &config, &store);
        let mark = if status.get("configured").and_then(YamlValue::as_bool) == Some(true) {
            format!(
                "configured ({}, from {})",
                status
                    .get("app_id_masked")
                    .and_then(YamlValue::as_str)
                    .unwrap_or(""),
                status
                    .get("source")
                    .and_then(YamlValue::as_str)
                    .unwrap_or("")
            )
        } else if status.get("half_configured").and_then(YamlValue::as_bool) == Some(true) {
            "app id stored, secret missing".to_string()
        } else if !spec.configurable {
            "tracked via its own connect flow".to_string()
        } else {
            "no app yet".to_string()
        };
        text.push_str(&format!("  {}: {mark}\n", spec.display_name));
    }
    text.push_str(NOTE);
    let _ = write_text(out, &text);
    exit::SUCCESS
}

/// `xpst byo clear`.
pub fn clear(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let Some(platform) = leaf.and_then(|m| crate::string_value(m, "arg:platform")) else {
        let _ = writeln!(err, "xpst byo clear: PLATFORM is required");
        return exit::CONFIG_ERROR;
    };
    let registry = match registry() {
        Ok(registry) => registry,
        Err(message) => {
            let _ = writeln!(err, "xpst byo clear: {message}");
            return exit::GENERAL;
        }
    };
    let key = platform.trim().to_lowercase();
    let Some(spec) = registry.specs.iter().find(|spec| spec.platform == key) else {
        let options = {
            let mut names: Vec<&str> = registry.order.iter().map(String::as_str).collect();
            names.sort_unstable();
            names.join(", ")
        };
        // Python prints this with `console.print` even under `--json`, so it
        // lands on stdout as text; matching that keeps the two engines
        // byte-identical on the error path too.
        let message = format!("Unknown platform for BYO setup: {platform} (options: {options})");
        let _ = write_text(out, &crate::output::wrap_at(&message, 80));
        return exit::CONFIG_ERROR;
    };

    let (dir, mut config) = match load(global, out, err) {
        Ok(loaded) => loaded,
        Err(code) => return code,
    };
    let store = CredentialStore::new(&dir);
    let mut removed: Vec<YamlValue> = Vec::new();
    for store_key in [&spec.store_id_key, &spec.store_secret_key] {
        if store.delete(store_key) {
            removed.push(string(store_key.clone()));
        }
    }

    // Clear the config mirror, as `clear_byo_app` does. Python rewrites the
    // whole document from the Pydantic model here; the Rust engine edits the
    // two fields in place (documented divergence).
    if !spec.config_id_field.is_empty() {
        if let YamlValue::Mapping(root) = &mut config {
            let section = root
                .get_mut("accounts")
                .and_then(|accounts| match accounts {
                    YamlValue::Mapping(map) => map.get_mut(&spec.platform),
                    _ => None,
                });
            if let Some(YamlValue::Mapping(section)) = section {
                section.insert(YamlValue::from(spec.config_id_field.as_str()), string(""));
                if !spec.config_secret_field.is_empty() {
                    section.insert(
                        YamlValue::from(spec.config_secret_field.as_str()),
                        string(""),
                    );
                }
                let path = dir.join("config.yaml");
                if let Err(message) = write_config(&path, &config) {
                    let _ = writeln!(err, "{message}");
                    return exit::GENERAL;
                }
            }
        }
    }

    let note = format!(
        "{} no longer has an app on this machine; sign-in is off until one is added.",
        spec.display_name
    );
    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("platform", string(spec.platform.clone())),
            ("configured", boolean(false)),
            ("removed", array(removed)),
            ("sign_in_available", boolean(false)),
            ("docs_url", string(spec.docs_url.clone())),
            ("note", string(note)),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(out, &format!("✅ {note}"));
    }
    exit::SUCCESS
}

/// `xpst byo set` — validate, store encrypted, and report the masked view.
///
/// Parity target: `src/xpst/cli.py::byo_set_cmd` over
/// `src/xpst/byo.py::validate_byo_app` / `store_byo_app`.
///
/// The credential pair comes from `--app-id`/`--app-secret`, then the
/// platform's `XPST_*_APP_ID`/`_APP_SECRET` environment variables, then a
/// prompt. The secret is written only to the encrypted store; the app id is
/// mirrored into `accounts.<platform>.<id field>` so a surface can show which
/// app is in play without decrypting anything.
///
/// **Divergences, recorded in the ledger note:** Python rewrites `config.yaml`
/// from the Pydantic model after the mirror write, while this edits the one
/// field in the raw document (the same choice `byo clear` makes); and the
/// interactive secret prompt reads a line from stdin instead of `getpass`, so
/// it is not hidden. The non-interactive path (flags or env) is identical.
pub fn set(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let Some(platform) = leaf.and_then(|m| crate::string_value(m, "arg:platform")) else {
        let _ = writeln!(err, "xpst byo set: PLATFORM is required");
        return exit::CONFIG_ERROR;
    };
    let registry = match registry() {
        Ok(registry) => registry,
        Err(message) => {
            let _ = writeln!(err, "xpst byo set: {message}");
            return exit::GENERAL;
        }
    };
    let key = platform.trim().to_lowercase();
    let Some(spec) = registry.specs.iter().find(|spec| spec.platform == key) else {
        let options = {
            let mut names: Vec<&str> = registry.order.iter().map(String::as_str).collect();
            names.sort_unstable();
            names.join(", ")
        };
        // `console.print` even under `--json`, so this lands on stdout as text.
        let message = format!("Unknown platform: {platform} (options: {options})");
        let _ = write_text(out, &crate::output::wrap_at(&message, 80));
        return exit::CONFIG_ERROR;
    };
    if !spec.configurable {
        let message = format!(
            "{} keeps its app credential in its own connect flow.",
            spec.display_name
        );
        let _ = write_text(out, &crate::output::wrap_at(&message, 80));
        let _ = write_text(out, &spec.docs_url);
        return exit::SUCCESS;
    }

    let (dir, mut config) = match load(global, json_mode, out, err) {
        Ok(loaded) => loaded,
        Err(code) => return code,
    };

    let mut app_id = leaf_string(leaf, "opt:app-id").unwrap_or_default();
    let mut app_secret = leaf_string(leaf, "opt:app-secret").unwrap_or_default();
    if app_id.is_empty() {
        app_id = std::env::var(&spec.env_id_var)
            .unwrap_or_default()
            .trim()
            .to_string();
    }
    if app_secret.is_empty() {
        app_secret = std::env::var(&spec.env_secret_var)
            .unwrap_or_default()
            .trim()
            .to_string();
    }
    if app_id.is_empty() {
        let _ = write!(out, "{}: ", spec.id_label);
        let _ = out.flush();
        match read_line() {
            Some(line) => app_id = line.trim().to_string(),
            None => {
                // Python's `console.input` raises EOFError, which nothing
                // catches: the traceback goes to stderr and the exit is 1.
                let _ = writeln!(err, "EOFError: EOF when reading a line");
                return exit::GENERAL;
            }
        }
    }
    if app_secret.is_empty() {
        // `getpass.getpass` writes its prompt to stderr and answers EOF with an
        // empty string (the `except (EOFError, KeyboardInterrupt)` branch).
        let _ = write!(err, "{} (hidden): ", spec.secret_label);
        let _ = err.flush();
        app_secret = read_line().unwrap_or_default().trim().to_string();
    }

    let app_id = app_id.trim().to_string();
    let app_secret = app_secret.trim().to_string();
    let mut errors: Vec<(&str, String)> = Vec::new();
    let id_ok = if spec.platform == "tiktok" {
        valid_tiktok_key(&app_id)
    } else {
        valid_meta_app_id(&app_id)
    };
    if app_id.is_empty() {
        errors.push(("app_id", format!("{} is required.", spec.id_label)));
    } else if !id_ok {
        let expected = if spec.platform == "tiktok" {
            "starts with 'aw' followed by letters/digits"
        } else {
            "must be digits"
        };
        errors.push((
            "app_id",
            format!("{} looks wrong ({expected}).", spec.id_label),
        ));
    }
    if app_secret.is_empty() {
        errors.push(("app_secret", format!("{} is required.", spec.secret_label)));
    } else if !valid_secret(&app_secret) {
        errors.push((
            "app_secret",
            format!(
                "{} looks wrong (8–256 chars, no spaces).",
                spec.secret_label
            ),
        ));
    }
    if !errors.is_empty() {
        if json_mode {
            let mut fields = xpst_config::Mapping::new();
            for (field, message) in &errors {
                fields.insert(YamlValue::from(*field), string(message.clone()));
            }
            let payload = object(vec![
                ("ok", boolean(false)),
                ("error", string("Those app credentials were not accepted.")),
                ("errors", YamlValue::Mapping(fields)),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, "Those app credentials were not accepted.");
            for (field, message) in &errors {
                let _ = write_text(out, &format!("  {field}: {message}"));
            }
            let _ = write_text(out, &format!("How to create one: {}", spec.create_url));
        }
        return exit::CONFIG_ERROR;
    }

    let store = CredentialStore::new(&dir);
    if let Err(message) = store
        .store(&spec.store_id_key, &app_id)
        .and_then(|()| store.store(&spec.store_secret_key, &app_secret))
    {
        let message = format!("Could not store the app credential encrypted: {message}");
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("error", string(message)),
                ("errors", object(Vec::new())),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, &message);
        }
        return exit::CONFIG_ERROR;
    }

    // Mirror the (non-secret) app id into the config document, as
    // `store_byo_app` does. Python rewrites the whole file from the model; this
    // edits the field in place (documented divergence).
    if !spec.config_id_field.is_empty() {
        if let YamlValue::Mapping(root) = &mut config {
            if !root.contains_key("accounts") {
                root.insert(
                    YamlValue::from("accounts"),
                    YamlValue::Mapping(Mapping::new()),
                );
            }
            if let Some(YamlValue::Mapping(accounts)) = root.get_mut("accounts") {
                if !accounts.contains_key(&spec.platform) {
                    accounts.insert(
                        YamlValue::from(spec.platform.as_str()),
                        YamlValue::Mapping(Mapping::new()),
                    );
                }
                if let Some(YamlValue::Mapping(section)) = accounts.get_mut(&spec.platform) {
                    section.insert(
                        YamlValue::from(spec.config_id_field.as_str()),
                        string(app_id.clone()),
                    );
                }
            }
        }
        let path = dir.join("config.yaml");
        if let Err(message) = write_config(&path, &config) {
            let _ = writeln!(err, "{message}");
            return exit::GENERAL;
        }
    }

    let status = status_for(spec, &config, &store);
    if json_mode {
        let mut payload = vec![("ok", boolean(true))];
        if let YamlValue::Mapping(map) = &status {
            for (key, value) in map {
                if let YamlValue::String(key) = key {
                    payload.push((key.as_str(), value.clone()));
                }
            }
        }
        let _ = write_python_json(out, &object(payload));
        return exit::SUCCESS;
    }

    let masked = status
        .get("app_id_masked")
        .and_then(YamlValue::as_str)
        .unwrap_or("");
    let _ = write_text(
        out,
        &format!(
            "✅ {}: app {masked} stored encrypted. {}",
            spec.display_name, spec.enables
        ),
    );
    let _ = write_text(
        out,
        &format!("Register this redirect on the app: {}", spec.redirect_uri),
    );
    exit::SUCCESS
}

/// One line from stdin, or `None` at EOF.
fn read_line() -> Option<String> {
    let mut line = String::new();
    match std::io::stdin().read_line(&mut line) {
        Ok(0) => None,
        Ok(_) => Some(line),
        Err(_) => None,
    }
}

/// `_APP_ID_RE = ^[0-9]{4,24}$`.
fn valid_meta_app_id(text: &str) -> bool {
    (4..=24).contains(&text.len()) && text.chars().all(|character| character.is_ascii_digit())
}

/// `_TIKTOK_KEY_RE = ^aw[0-9a-z]{6,32}$` (case-insensitive).
fn valid_tiktok_key(text: &str) -> bool {
    let lowered = text.to_lowercase();
    let Some(rest) = lowered.strip_prefix("aw") else {
        return false;
    };
    (6..=32).contains(&rest.len())
        && rest
            .chars()
            .all(|character| character.is_ascii_alphanumeric())
}

/// `_SECRET_RE = ^[A-Za-z0-9_\-\.]{8,256}$`.
fn valid_secret(text: &str) -> bool {
    (8..=256).contains(&text.len())
        && text.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '_' | '-' | '.')
        })
}

/// Write the config document with the PyYAML-identical emitter, owner-only.
fn write_config(path: &Path, document: &YamlValue) -> Result<(), String> {
    std::fs::write(path, xpst_config::pyyaml::dump(document))
        .map_err(|err| format!("cannot write {}: {err}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}
