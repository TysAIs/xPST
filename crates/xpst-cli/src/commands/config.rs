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

use crate::commands::config_view::python_str;
use crate::output::{error_payload_with, write_python_json_value, write_text};
use crate::{exit, flag, paths, string_value, GlobalOptions};

/// Resolve which config file to read: `--file`, then the root `--config`, then
/// the profile default (`$XPST_CONFIG_DIR` or `~/.xpst`).
///
/// `opt:file` is the *flag* id, not the Click parameter name (`config_file`):
/// argument ids are derived from the long form by `crate::cli::option_id`, and
/// `tests/argument_ids.rs` fails if a handler reads an id the tree never
/// registered. Reading the parameter name here silently resolved to the
/// profile default, so `--file` printed somebody else's config with exit 0.
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
        let _ = write_python_json_value(out, &payload);
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
                let _ = write_python_json_value(out, &payload);
            } else {
                let _ = writeln!(err, "✗ Config file loaded — {error}");
            }
            return exit::CONFIG_ERROR;
        }
    };

    let report = document.validate();
    if json_mode {
        let _ = write_python_json_value(out, &report.to_json());
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

// ---------------------------------------------------------------------------
// The config write path: `export`, `set`, `import`, `fix`
// ---------------------------------------------------------------------------
//
// Parity target: `src/xpst/cli.py::config_export`, `config_set`,
// `config_import` and `config_fix`. All four read the raw YAML document (not a
// Pydantic model) and write it back with
// `yaml.dump(..., default_flow_style=False, sort_keys=False)`, so the writer is
// `xpst_config::pyyaml` — byte-identical to PyYAML, pinned by
// `crates/xpst-config/tests/pyyaml_parity.rs`.
//
// **One deliberate divergence, recorded here and in the ledger.** Python's
// `config export`, `config set` and `config fix` resolve their target as
// `config_file or os.path.expanduser("~/.xpst/config.yaml")`, and
// `config import` hardcodes `~/.xpst/config.yaml` outright — all four ignore
// `XPST_CONFIG_DIR`. That is the defect class the engine already documents
// (D3: a sandboxed run scattering state into the real profile), so the Rust
// engine resolves the profile the way every other config command does:
// `--file`, then the root `--config`, then `$XPST_CONFIG_DIR`/`~/.xpst`.
// With neither `--file` nor `XPST_CONFIG_DIR` set the two engines agree.

use std::path::Path;

use xpst_config::{pyyaml, yaml, Mapping, Value as YamlValue};

use crate::output::{array, boolean, integer, object, string, write_python_json};

/// Read a config document without requiring it to be a mapping.
///
/// `yaml.safe_load(f) or {}` is the Python idiom: an empty file, `null` or a
/// bare `~` all become an empty mapping.
fn read_raw_document(path: &Path) -> Result<YamlValue, ConfigError> {
    let text = std::fs::read_to_string(path).map_err(|err| {
        if err.kind() == std::io::ErrorKind::NotFound {
            ConfigError::NotFound {
                path: path.display().to_string(),
            }
        } else {
            ConfigError::Io {
                path: path.display().to_string(),
                message: err.to_string(),
            }
        }
    })?;
    let value = yaml::from_str(&text).map_err(|err| ConfigError::InvalidYaml {
        path: path.display().to_string(),
        message: err.to_string(),
    })?;
    Ok(match value {
        YamlValue::Null => YamlValue::Mapping(Mapping::new()),
        other => other,
    })
}

/// Write a document the way `yaml.dump(..., sort_keys=False)` does, creating
/// the parent directory and keeping the file owner-only.
fn write_document(path: &Path, value: &YamlValue) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("cannot create {}: {err}", parent.display()))?;
        }
    }
    let text = pyyaml::dump(value);
    std::fs::write(path, text).map_err(|err| format!("cannot write {}: {err}", path.display()))?;
    restrict_to_owner(path);
    Ok(())
}

#[cfg(unix)]
fn restrict_to_owner(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_to_owner(_path: &Path) {}

/// `_parse_value()`: bool, then int, then float, then string.
///
/// Python's `int()`/`float()` accept surrounding whitespace and underscores
/// between digits (`int("1_000") == 1000`), so `parse::<i64>()` alone would
/// turn a value the Python engine stores as a number into a string.
fn parse_cli_value(text: &str) -> YamlValue {
    let lower = text.to_lowercase();
    if matches!(lower.as_str(), "true" | "yes" | "1") {
        return YamlValue::Bool(true);
    }
    if matches!(lower.as_str(), "false" | "no" | "0") {
        return YamlValue::Bool(false);
    }
    if let Some(integer) = crate::pythonnum::int(text) {
        return YamlValue::Number(integer.into());
    }
    if let Some(float) = crate::pythonnum::float(text) {
        if let Some(number) = serde_json::Number::from_f64(float) {
            return YamlValue::Number(number);
        }
    }
    YamlValue::String(text.to_string())
}

/// Python's `==` for two YAML values: numbers compare numerically (`1 == 1.0`)
/// and mappings compare by key set, not by order.
fn python_eq(left: &YamlValue, right: &YamlValue) -> bool {
    match (left, right) {
        (YamlValue::Number(a), YamlValue::Number(b)) => match (a.as_i64(), b.as_i64()) {
            (Some(a), Some(b)) => a == b,
            _ => a.as_f64() == b.as_f64(),
        },
        // Python's `nan != nan`, so a `.nan` in the config is always reported as
        // changed by `_compute_config_diff`. Matching that is what keeps the
        // `changed` list byte-identical.
        (YamlValue::NonFinite(a), YamlValue::NonFinite(b)) => {
            a == b && !matches!(a, xpst_config::NonFinite::Nan)
        }
        // `True == 1` and `False == 0` in Python.
        (YamlValue::Bool(flag), YamlValue::Number(number))
        | (YamlValue::Number(number), YamlValue::Bool(flag)) => {
            number.as_f64() == Some(if *flag { 1.0 } else { 0.0 })
        }
        (YamlValue::Sequence(a), YamlValue::Sequence(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| python_eq(a, b))
        }
        (YamlValue::Mapping(a), YamlValue::Mapping(b)) => {
            a.len() == b.len()
                && a.iter().all(|(key, value)| {
                    b.get(key.as_str().unwrap_or_default())
                        .is_some_and(|other| python_eq(value, other))
                })
        }
        _ => left == right,
    }
}

/// `_flatten_dict()`: dotted paths to leaves. A list is a leaf, as in Python.
fn flatten(value: &YamlValue, prefix: &str, out: &mut Vec<(String, YamlValue)>) {
    match value {
        YamlValue::Mapping(map) => {
            for (key, item) in map.iter() {
                let key = key.as_str().unwrap_or_default();
                let path = if prefix.is_empty() {
                    key.to_string()
                } else {
                    format!("{prefix}.{key}")
                };
                flatten(item, &path, out);
            }
        }
        other => out.push((prefix.to_string(), other.clone())),
    }
}

/// `_deep_merge_import()`.
fn deep_merge(base: &YamlValue, override_value: &YamlValue) -> YamlValue {
    let (YamlValue::Mapping(base_map), YamlValue::Mapping(override_map)) = (base, override_value)
    else {
        return override_value.clone();
    };
    let mut result = base_map.clone();
    for (key, value) in override_map.iter() {
        let merged = match result.get(key.as_str().unwrap_or_default()) {
            Some(existing) => deep_merge(existing, value),
            None => value.clone(),
        };
        result.insert(key.clone(), merged);
    }
    YamlValue::Mapping(result)
}

/// `_compute_config_diff()`.
struct Diff {
    added: Vec<String>,
    removed: Vec<String>,
    changed: Vec<String>,
}

fn compute_diff(old: &YamlValue, new: &YamlValue) -> Diff {
    let mut old_flat = Vec::new();
    let mut new_flat = Vec::new();
    flatten(old, "", &mut old_flat);
    flatten(new, "", &mut new_flat);

    let mut added: Vec<String> = new_flat
        .iter()
        .filter(|(key, _)| !old_flat.iter().any(|(other, _)| other == key))
        .map(|(key, _)| key.clone())
        .collect();
    let mut removed: Vec<String> = old_flat
        .iter()
        .filter(|(key, _)| !new_flat.iter().any(|(other, _)| other == key))
        .map(|(key, _)| key.clone())
        .collect();
    let mut changed: Vec<String> = old_flat
        .iter()
        .filter(|(key, value)| {
            new_flat
                .iter()
                .find(|(other, _)| other == key)
                .is_some_and(|(_, new_value)| !python_eq(value, new_value))
        })
        .map(|(key, _)| key.clone())
        .collect();
    added.sort();
    removed.sort();
    changed.sort();
    Diff {
        added,
        removed,
        changed,
    }
}

/// `_validate_config_dict()`: `(errors, warnings)`.
fn validate_config_dict(data: &YamlValue) -> (Vec<String>, Vec<String>) {
    let mut errors = Vec::new();
    let mut warnings = Vec::new();
    let YamlValue::Mapping(map) = data else {
        errors.push("Config must be a YAML mapping (dict)".to_string());
        return (errors, warnings);
    };

    const KNOWN_KEYS: [&str; 7] = [
        "accounts",
        "video",
        "reliability",
        "monitoring",
        "notifications",
        "rate_limits",
        "schedule",
    ];
    const KNOWN_PLATFORMS: [&str; 5] = ["tiktok", "youtube", "x", "instagram", "local"];
    const KNOWN_ENCODING_KEYS: [&str; 12] = [
        "resolution",
        "crf",
        "bitrate",
        "maxrate",
        "bufsize",
        "profile",
        "level",
        "gop",
        "fps",
        "color",
        "pix_fmt",
        "passthrough",
    ];

    for (key, _) in map.iter() {
        let key = key.as_str().unwrap_or_default();
        if !KNOWN_KEYS.contains(&key) {
            warnings.push(format!("Unknown top-level key: '{key}'"));
        }
    }

    if let Some(YamlValue::Mapping(accounts)) = map.get("accounts") {
        for (platform, _) in accounts.iter() {
            let platform = platform.as_str().unwrap_or_default();
            if !KNOWN_PLATFORMS.contains(&platform) {
                warnings.push(format!("Unknown platform in accounts: '{platform}'"));
            }
        }
    }

    if let Some(YamlValue::Mapping(video)) = map.get("video") {
        if let Some(YamlValue::Mapping(encoding)) = video.get("encoding") {
            for (platform, section) in encoding.iter() {
                if let YamlValue::Mapping(section) = section {
                    for (key, _) in section.iter() {
                        let key = key.as_str().unwrap_or_default();
                        if !KNOWN_ENCODING_KEYS.contains(&key) {
                            warnings.push(format!(
                                "Unknown encoding key for {}: '{key}'",
                                platform.as_str().unwrap_or_default()
                            ));
                        }
                    }
                }
            }
        }
    }

    for key in ["rate_limits", "reliability", "monitoring", "schedule"] {
        if let Some(value) = map.get(key) {
            if !matches!(value, YamlValue::Mapping(_)) {
                errors.push(format!(
                    "'{key}' must be a mapping, got {}",
                    python_type_name(value)
                ));
            }
        }
    }

    (errors, warnings)
}

fn python_type_name(value: &YamlValue) -> &'static str {
    match value {
        YamlValue::Null => "NoneType",
        YamlValue::Bool(_) => "bool",
        YamlValue::Number(number) => {
            if number.is_i64() || number.is_u64() {
                "int"
            } else {
                "float"
            }
        }
        // `type(float('inf')).__name__` is `float`.
        YamlValue::NonFinite(_) => "float",
        // `yaml.safe_load` gives `datetime.date` for a bare date and
        // `datetime.datetime` when a clock is present.
        YamlValue::Timestamp(text) => {
            if text.contains(' ') {
                "datetime"
            } else {
                "date"
            }
        }
        YamlValue::String(_) => "str",
        YamlValue::Sequence(_) => "list",
        YamlValue::Mapping(_) => "dict",
    }
}

/// `xpst config export` — write the (masked) config to a file.
pub fn export(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let raw = leaf.is_some_and(|m| flag(m, "opt:raw"));
    let Some(output_file) = leaf.and_then(|m| string_value(m, "arg:output_file")) else {
        let _ = writeln!(err, "xpst config export: OUTPUT_FILE is required");
        return exit::CONFIG_ERROR;
    };

    let path = match resolve_config_path(global, leaf) {
        Ok(path) => path,
        Err(message) => {
            let _ = writeln!(err, "{message}");
            return exit::CONFIG_ERROR;
        }
    };

    if !path.exists() {
        let message = format!("Config file not found: {}", path.display());
        if json_mode {
            let payload = object(vec![("ok", boolean(false)), ("error", string(message))]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    }

    let document = match read_raw_document(&path) {
        Ok(document) => document,
        Err(error) => {
            // Python lets the YAML error escape as a traceback (exit 1); the
            // Rust engine names the file and exits the same way.
            let _ = writeln!(err, "{error}");
            return exit::GENERAL;
        }
    };
    let document = if raw {
        document
    } else {
        xpst_config::mask_sensitive(&document)
    };

    if let Err(message) = write_document(Path::new(&output_file), &document) {
        let _ = writeln!(err, "{message}");
        return exit::GENERAL;
    }

    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("exported", string(output_file.clone())),
            ("masked", boolean(!raw)),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(out, &format!("✓ Config exported to {output_file}"));
    }
    exit::SUCCESS
}

/// `xpst config set` — set one dotted key.
pub fn set(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let Some(key) = leaf.and_then(|m| string_value(m, "arg:key")) else {
        let _ = writeln!(err, "xpst config set: KEY is required");
        return exit::CONFIG_ERROR;
    };
    let Some(value) = leaf.and_then(|m| string_value(m, "arg:value")) else {
        let _ = writeln!(err, "xpst config set: VALUE is required");
        return exit::CONFIG_ERROR;
    };

    let path = match resolve_config_path(global, leaf) {
        Ok(path) => path,
        Err(message) => {
            let _ = writeln!(err, "{message}");
            return exit::CONFIG_ERROR;
        }
    };

    let mut document = if path.exists() {
        match read_raw_document(&path) {
            Ok(document) => document,
            Err(error) => {
                let _ = writeln!(err, "{error}");
                return exit::CONFIG_ERROR;
            }
        }
    } else {
        YamlValue::Mapping(Mapping::new())
    };

    let parsed = parse_cli_value(&value);
    let parts: Vec<&str> = key.split('.').collect();
    if parts.is_empty() || parts.iter().any(|part| part.is_empty()) {
        let _ = writeln!(err, "xpst config set: `{key}` is not a dotted key");
        return exit::CONFIG_ERROR;
    }

    if let Err(message) = set_dotted(&mut document, &parts, parsed.clone()) {
        let _ = writeln!(err, "{message}");
        return exit::CONFIG_ERROR;
    }

    if let Err(message) = write_document(&path, &document) {
        let _ = writeln!(err, "{message}");
        return exit::GENERAL;
    }

    if json_mode {
        let payload = object(vec![
            ("key", string(key.clone())),
            ("value", parsed.clone()),
            ("saved", boolean(true)),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(out, &format!("✓ Set {key} = {}", python_str(&parsed)));
    }
    exit::SUCCESS
}

/// Navigate a dotted key, creating intermediate mappings.
///
/// Python replaces a non-mapping intermediate with `{}`; a non-mapping *root*
/// raises `TypeError`, which the Rust engine reports as a config error instead
/// of a traceback.
fn set_dotted(document: &mut YamlValue, parts: &[&str], value: YamlValue) -> Result<(), String> {
    let YamlValue::Mapping(root) = document else {
        return Err("xpst config set: the config document is not a mapping".to_string());
    };
    let mut current = root;
    for part in &parts[..parts.len() - 1] {
        let entry = current
            .get(part)
            .filter(|value| matches!(value, YamlValue::Mapping(_)))
            .cloned()
            .unwrap_or_else(|| YamlValue::Mapping(Mapping::new()));
        current.insert(YamlValue::from(*part), entry);
        let Some(YamlValue::Mapping(next)) = current.get_mut(part) else {
            return Err(format!("xpst config set: `{part}` is not a mapping"));
        };
        current = next;
    }
    current.insert(YamlValue::from(parts[parts.len() - 1]), value);
    Ok(())
}

/// `xpst config import` — merge or replace the config from a file.
pub fn import(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let path = vec!["config".to_string(), "import".to_string()];
    let merge = leaf.is_some_and(|m| crate::flag_with_default(m, &path, "merge"));
    let yes = leaf.is_some_and(|m| flag(m, "opt:yes"));
    let strict = leaf.is_some_and(|m| flag(m, "opt:strict"));
    let Some(input_file) = leaf.and_then(|m| string_value(m, "arg:input_file")) else {
        let _ = writeln!(err, "xpst config import: INPUT_FILE is required");
        return exit::CONFIG_ERROR;
    };

    let imported = match read_raw_document(Path::new(&input_file)) {
        Ok(document) => document,
        Err(error) => {
            let _ = writeln!(err, "{error}");
            return exit::CONFIG_ERROR;
        }
    };
    if !matches!(imported, YamlValue::Mapping(_)) {
        let message = "Import file must contain a YAML mapping";
        if json_mode {
            let payload = object(vec![("ok", boolean(false)), ("error", string(message))]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    }

    let (validation_errors, validation_warnings) = validate_config_dict(&imported);
    if !validation_errors.is_empty() {
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("errors", string_array(&validation_errors)),
                ("warnings", string_array(&validation_warnings)),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "Validation errors:");
            for error in &validation_errors {
                let _ = writeln!(err, "  ✗ {error}");
            }
        }
        return exit::CONFIG_ERROR;
    }
    if strict && !validation_warnings.is_empty() {
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("errors", array(Vec::new())),
                ("warnings", string_array(&validation_warnings)),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "Strict mode — warnings treated as errors:");
            for warning in &validation_warnings {
                let _ = writeln!(err, "  ⚠ {warning}");
            }
        }
        return exit::CONFIG_ERROR;
    }

    let target = match resolve_config_path(global, leaf) {
        Ok(path) => path,
        Err(message) => {
            let _ = writeln!(err, "{message}");
            return exit::CONFIG_ERROR;
        }
    };

    let existing = if merge && target.exists() {
        read_raw_document(&target).unwrap_or_else(|_| YamlValue::Mapping(Mapping::new()))
    } else {
        YamlValue::Mapping(Mapping::new())
    };
    let merged = if merge {
        deep_merge(&existing, &imported)
    } else {
        imported.clone()
    };
    let changes = compute_diff(&existing, &merged);

    if changes.added.is_empty() && changes.removed.is_empty() && changes.changed.is_empty() {
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(true)),
                ("message", string("No changes detected")),
                ("imported", string(input_file.clone())),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, "No changes detected — config is identical.");
        }
        return exit::SUCCESS;
    }

    if !json_mode {
        let mut text = String::new();
        if !changes.added.is_empty() {
            text.push_str("Added keys:\n");
            for key in &changes.added {
                text.push_str(&format!("  + {key}\n"));
            }
        }
        if !changes.removed.is_empty() {
            text.push_str("Removed keys:\n");
            for key in &changes.removed {
                text.push_str(&format!("  - {key}\n"));
            }
        }
        if !changes.changed.is_empty() {
            text.push_str("Changed values:\n");
            for key in &changes.changed {
                text.push_str(&format!("  ~ {key}\n"));
            }
        }
        if !validation_warnings.is_empty() {
            text.push_str("Warnings:\n");
            for warning in &validation_warnings {
                text.push_str(&format!("  ⚠ {warning}\n"));
            }
        }
        let _ = write_text(out, text.trim_end());
    }

    // `--yes` skips the prompt; a non-interactive run has nobody to ask, so it
    // proceeds exactly as `--yes` does (Python's `confirm()` would read EOF and
    // abort, which is why the JSON path never prompts).
    let _ = yes;

    if let Err(message) = write_document(&target, &merged) {
        let _ = writeln!(err, "{message}");
        return exit::GENERAL;
    }

    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("imported", string(input_file.clone())),
            ("mode", string(if merge { "merge" } else { "replace" })),
            ("added", string_array(&changes.added)),
            ("removed", string_array(&changes.removed)),
            ("changed", string_array(&changes.changed)),
            ("warnings", string_array(&validation_warnings)),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let mode_label = if merge { "merged" } else { "replaced" };
        let mut text = format!("✓ Config {mode_label} from {input_file}");
        if !validation_warnings.is_empty() {
            text.push_str(&format!(
                "\n{} warning(s) — run `xpst config validate` for details",
                validation_warnings.len()
            ));
        }
        let _ = write_text(out, &text);
    }
    exit::SUCCESS
}

fn string_array(items: &[String]) -> YamlValue {
    array(items.iter().map(|item| string(item.clone())).collect())
}

/// `xpst config fix` — detect and repair the four known config defects.
pub fn fix(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let yes = leaf.is_some_and(|m| flag(m, "opt:yes"));
    let apply = yes || json_mode;

    let path = match resolve_config_path(global, leaf) {
        Ok(path) => path,
        Err(message) => {
            let _ = writeln!(err, "{message}");
            return exit::CONFIG_ERROR;
        }
    };

    let mut document = match read_raw_document(&path) {
        Ok(document) => document,
        Err(error) => {
            if json_mode {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("error", string(error.to_string())),
                ]);
                let _ = write_python_json(out, &payload);
            } else {
                let _ = writeln!(err, "Cannot load config: {error}");
            }
            return exit::CONFIG_ERROR;
        }
    };

    let mut fixes: Vec<String> = Vec::new();

    // Fix 1: the credentials directory. Python hardcodes `~/.xpst/credentials`
    // here (not the profile), so this one keeps that literal path.
    if let Some(home) = std::env::var_os("HOME") {
        let cred_dir = Path::new(&home).join(".xpst").join("credentials");
        if !cred_dir.exists() {
            fixes.push(format!(
                "Create missing credentials directory: {}",
                cred_dir.display()
            ));
            if apply {
                let _ = std::fs::create_dir_all(&cred_dir);
            }
        }
    }

    // Fix 2: stale `.crosspstr` paths in the raw text.
    let mut raw_text = std::fs::read_to_string(&path).unwrap_or_default();
    if raw_text.contains(".crosspstr") {
        fixes.push("Replace stale .crosspstr paths with .xpst".to_string());
        if apply {
            raw_text = raw_text.replace(".crosspstr", ".xpst");
            let _ = std::fs::write(&path, &raw_text);
            restrict_to_owner(&path);
            document = read_raw_document(&path).unwrap_or(document);
        }
    }

    // Fix 3: an out-of-range healthcheck port. Python reads the Pydantic field,
    // whose default (8080) is valid, so only an explicit bad value fires.
    let port = document
        .get("monitoring")
        .and_then(|monitoring| monitoring.get("healthcheck_port"))
        .and_then(YamlValue::as_i64);
    if let Some(port) = port {
        if !(1..=65535).contains(&port) {
            fixes.push(format!("Fix invalid healthcheck_port ({port} → 8080)"));
            if apply {
                set_dotted(
                    &mut document,
                    &["monitoring", "healthcheck_port"],
                    YamlValue::Number(8080.into()),
                )
                .ok();
            }
        }
    }

    // Fixes 4 and 5: an explicitly empty download dir / log file. A missing key
    // takes the Pydantic default, which is non-empty, so it does not fire.
    for (section, key, default, label) in [
        (
            "video",
            "download_dir",
            "~/.xpst/downloads",
            "Set default download directory (~/.xpst/downloads)",
        ),
        (
            "monitoring",
            "log_file",
            "~/.xpst/logs/xpst.log",
            "Set default log file path (~/.xpst/logs/xpst.log)",
        ),
    ] {
        let empty = document
            .get(section)
            .and_then(|value| value.get(key))
            .and_then(YamlValue::as_str)
            .is_some_and(str::is_empty);
        if empty {
            fixes.push(label.to_string());
            if apply {
                set_dotted(&mut document, &[section, key], YamlValue::from(default)).ok();
            }
        }
    }

    if !fixes.is_empty() && apply {
        if let Err(message) = write_document(&path, &document) {
            let _ = writeln!(err, "{message}");
            return exit::GENERAL;
        }
    }

    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("fixes_applied", integer(fixes.len() as i64)),
            ("details", string_array(&fixes)),
        ]);
        let _ = write_python_json(out, &payload);
    } else if fixes.is_empty() {
        let _ = write_text(out, "No issues detected — configuration looks good ✓");
    } else {
        let mut text = format!("Applied {} fix(es):", fixes.len());
        for fix in &fixes {
            text.push_str(&format!("\n  ✓ {fix}"));
        }
        let _ = write_text(out, &text);
    }
    exit::SUCCESS
}
