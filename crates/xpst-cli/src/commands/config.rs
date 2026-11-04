//! `xpst config show` and `xpst config validate` — read-only config paths.
//!
//! Parity target: `src/xpst/cli.py::config_show` (raw YAML document, secrets
//! masked by keyword) and `src/xpst/cli.py::config_validate` (the
//! `{"valid": bool, "checks": [...]}` payload). Neither writes anything, so
//! both are safe to run against a live install during the rebuild.

use std::io::Write;
use std::path::PathBuf;

use clap::ArgMatches;
use serde_json::json;
use xpst_config::{ConfigDocument, ConfigError};

use crate::output::{error_payload_with, write_json, write_text};
use crate::{exit, flag, paths, string_value, GlobalOptions};

/// Resolve which config file to read: `--file`, then the root `--config`, then
/// the profile default (`$XPST_CONFIG_DIR` or `~/.xpst`).
fn resolve_config_path(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
) -> Result<PathBuf, String> {
    if let Some(from_leaf) = leaf.and_then(|m| string_value(m, "opt:file")) {
        return Ok(PathBuf::from(from_leaf));
    }
    if let Some(from_root) = &global.config_path {
        return Ok(PathBuf::from(from_root));
    }
    paths::config_file().ok_or_else(|| {
        "cannot determine the config directory: neither XPST_CONFIG_DIR nor HOME is set".to_string()
    })
}

/// Report an unreadable config in the shape the caller asked for.
fn report_error<W: Write, E: Write>(
    out: &mut W,
    err: &mut E,
    json_mode: bool,
    error: &ConfigError,
    path: &str,
) -> i32 {
    let (code, hint) = match error {
        ConfigError::NotFound { .. } => (
            "CONFIG_FILE_NOT_FOUND",
            Some("run `xpst wizard` or `xpst setup` to create one"),
        ),
        ConfigError::InvalidYaml { .. } => ("INVALID_YAML", None),
        _ => ("CONFIG_ERROR", None),
    };
    if json_mode {
        let mut extra = json!({ "path": path });
        if let Some(hint) = hint {
            extra["hint"] = json!(hint);
        }
        let payload = error_payload_with(code, &error.to_string(), extra);
        // Machine-readable errors stay on stdout so a `--json` caller can
        // parse them regardless of exit status, exactly like `_error_payload`.
        let _ = write_json(out, &payload);
    } else {
        let _ = writeln!(err, "{}", error);
        if let Some(hint) = hint {
            let _ = writeln!(err, "  {hint}");
        }
    }
    exit::CONFIG_ERROR
}

/// `xpst config show` — print the config document (masked unless `--raw`).
pub fn show<W: Write, E: Write>(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut W,
    err: &mut E,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let raw = leaf.is_some_and(|m| flag(m, "opt:raw"));

    let path = match resolve_config_path(global, leaf) {
        Ok(path) => path,
        Err(message) => {
            let _ = writeln!(err, "{message}");
            return exit::CONFIG_ERROR;
        }
    };

    let document = match ConfigDocument::read(&path) {
        Ok(document) => document,
        Err(error) => {
            let path_text = path.display().to_string();
            return report_error(out, err, json_mode, &error, &path_text);
        }
    };

    // Masked by default: `--raw` is the only way a secret reaches stdout.
    let mask = !raw;
    if json_mode {
        match document.to_json_string(mask) {
            Ok(text) => {
                let _ = writeln!(out, "{text}");
                exit::SUCCESS
            }
            Err(error) => report_error(out, err, json_mode, &error, document.path()),
        }
    } else {
        match document.to_yaml_string(mask) {
            Ok(text) => {
                let _ = write_text(out, &text);
                exit::SUCCESS
            }
            Err(error) => report_error(out, err, json_mode, &error, document.path()),
        }
    }
}

/// `xpst config validate` — read-only validation, exit 2 when invalid.
pub fn validate<W: Write, E: Write>(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut W,
    err: &mut E,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

    let path = match resolve_config_path(global, leaf) {
        Ok(path) => path,
        Err(message) => {
            let _ = writeln!(err, "{message}");
            return exit::CONFIG_ERROR;
        }
    };

    let document = match ConfigDocument::read(&path) {
        Ok(document) => document,
        Err(error) => {
            // Mirrors `config_validate`: a config that cannot load is itself a
            // failed check, reported in the validation payload shape.
            if json_mode {
                let payload = json!({
                    "valid": false,
                    "checks": [{
                        "name": "Config file loaded",
                        "ok": false,
                        "detail": error.to_string(),
                    }],
                });
                let _ = write_json(out, &payload);
            } else {
                let _ = writeln!(err, "✗ Config file loaded — {error}");
            }
            return exit::CONFIG_ERROR;
        }
    };

    let report = document.validate();
    if json_mode {
        let _ = write_json(out, &report.to_json());
    } else {
        let mut text = String::new();
        for check in &report.checks {
            let mark = if check.ok { "✓" } else { "✗" };
            text.push_str(&format!("{mark} {} — {}\n", check.name, check.detail));
        }
        let _ = write_text(out, text.trim_end());
    }

    if report.ok() {
        exit::SUCCESS
    } else {
        exit::CONFIG_ERROR
    }
}
