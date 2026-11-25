//! `xpst byo status`, `xpst byo set` and `xpst byo clear` — bring-your-own
//! developer app.
//!
//! Parity target: `src/xpst/byo.py` (`byo_status`, `byo_status_for_platform`,
//! `resolve_byo_app`, `clear_byo_app`, `mask_app_id`) and the three Click
//! commands that wrap them.
//!
//! The registry and the masked status payload live in `xpst_surface::byo`,
//! because three surfaces read them: these commands, the `byo_app` field of the
//! canonical provider catalog (`xpst providers`, `xpst diagnostics`,
//! `xpst_capabilities`) and the `xpst_byo_app` MCP tool. This module is the
//! clap surface plus the two writers (`clear`, `set`) that mutate the profile.
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
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;

use crate::commands::bootstrap;
use crate::output::{boolean, object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

// The registry, the credential resolution and the masked status payload are
// shared with the MCP surface, so they live below both crates. `byo_status()`
// itself (the whole-platform payload) is not re-exported: this module's `status`
// is the command runner, and the MCP tool imports the builder directly.
pub use xpst_surface::byo::{
    mask_app_id, registry, resolve, status_for, status_for_platform, Registry, Resolved, Spec, NOTE,
};

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

    if json_mode {
        let payload = match xpst_surface::byo::status(&config, &store) {
            Ok(payload) => payload,
            Err(message) => {
                let _ = writeln!(err, "xpst byo status: {message}");
                return exit::GENERAL;
            }
        };
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
    let (cleared, changed) = xpst_surface::byo::clear(&mut config, spec, &store);
    if changed {
        let path = dir.join("config.yaml");
        if let Err(message) = xpst_surface::byo::write_config(&path, &config) {
            let _ = writeln!(err, "{message}");
            return exit::GENERAL;
        }
    }

    let note = xpst_surface::byo::clear_note(spec);
    if json_mode {
        let mut payload = vec![("ok", boolean(true))];
        if let YamlValue::Mapping(map) = &cleared {
            for (key, value) in map {
                if let YamlValue::String(key) = key {
                    payload.push((key.as_str(), value.clone()));
                }
            }
        }
        let _ = write_python_json(out, &object(payload));
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

    let (dir, mut config) = match load(global, out, err) {
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
    let store = CredentialStore::new(&dir);
    let status = match xpst_surface::byo::store(&mut config, spec, &store, &app_id, &app_secret) {
        Ok(status) => status,
        Err(error) => {
            // A validation refusal is CONFIG_ERROR; a store failure is too —
            // the CLI has always answered both with the same code.
            if json_mode {
                let mut fields = xpst_config::Mapping::new();
                for (field, message) in &error.errors {
                    fields.insert(YamlValue::from(field.as_str()), string(message.clone()));
                }
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("error", string(error.message.clone())),
                    ("errors", YamlValue::Mapping(fields)),
                ]);
                let _ = write_python_json(out, &payload);
            } else {
                let _ = write_text(out, &error.message);
                for (field, message) in &error.errors {
                    let _ = write_text(out, &format!("  {field}: {message}"));
                }
                if !error.errors.is_empty() {
                    let _ = write_text(out, &format!("How to create one: {}", spec.create_url));
                }
            }
            return exit::CONFIG_ERROR;
        }
    };

    // Mirror the (non-secret) app id into the config document, as
    // `store_byo_app` does. Python rewrites the whole file from the model; this
    // edits the field in place (documented divergence).
    if !spec.config_id_field.is_empty() {
        let path = dir.join("config.yaml");
        if let Err(message) = xpst_surface::byo::write_config(&path, &config) {
            let _ = writeln!(err, "{message}");
            return exit::GENERAL;
        }
    }
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
