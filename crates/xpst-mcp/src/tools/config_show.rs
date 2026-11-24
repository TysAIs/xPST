//! `xpst_config_show` — the configuration with secrets masked.
//!
//! Mirrors `_handle_config_show()`: seven account sections plus `video`,
//! `monitoring` and `schedule`, each rendered as `dict(section.__dict__)` — the
//! **whole model**, defaults included — with secrets masked by
//! `_mask_sensitive_values()` and nested dataclasses rendered as `repr()`
//! strings (`EncodingConfig(resolution=1920, ...)`).
//!
//! The defaults are not hand-written here. `tests/fixtures/python_config_show_model.json`
//! is captured from the shipped model by `scripts/capture_mcp_tool_registry.py`
//! (nested dicts, field order preserved, plus the class name of every nested
//! dataclass), and the operator's `config.yaml` is merged onto it field by
//! field. That is what makes a defaulted field visible to an agent: a
//! file-only projection would report `accounts.youtube.enabled` as absent when
//! the engine actually runs with `true`.
//!
//! A missing `config.yaml` is not an error — `XPSTConfig.load()` materializes
//! the defaults, so the tool answers the defaults.

use serde_json::{Map, Value};

use crate::error::ToolError;

use super::ToolContext;

/// Captured from the shipped model; regenerate, never edit by hand.
const MODEL: &str = include_str!("../../tests/fixtures/python_config_show_model.json");

/// Fields the Python model types as `Path`, so `XPSTConfig.load()` expands a
/// leading `~` before the tool ever sees them. A string field that is not a
/// path (`proxy`, `username`) is left exactly as written.
const PATH_FIELDS: &[&str] = &[
    "credentials_path",
    "cookies_file",
    "session_file",
    "token_file",
    "client_secrets",
    "download_dir",
    "log_file",
    "path",
];

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let document: Value = serde_json::from_str(MODEL).map_err(|err| ToolError::Malformed {
        kind: "config projection fixture",
        path: "tests/fixtures/python_config_show_model.json".to_string(),
        detail: err.to_string(),
    })?;
    let model = document.get("model").cloned().unwrap_or(Value::Null);
    let classes = document
        .get("repr_classes")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let defaults = document
        .get("repr_defaults")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));

    let file = read_config(ctx)?;
    let mut projection = project(&model, file.as_ref(), &classes, &defaults);
    // Reprs first, then masking: Python's `_mask_sensitive_values()` walks
    // dicts and lists only, so a nested dataclass is rendered by `repr()` with
    // its values intact (`TikTokAggregatorConfig(api_key_ref='zernio_api_key',
    // ...)`). Masking first would blank a field Python leaves visible.
    render_reprs(&mut projection, &classes);
    mask(&mut projection);
    expand_paths(&mut projection);
    Ok(projection)
}

/// The operator's `config.yaml`, or `None` when the profile has never been used.
fn read_config(ctx: &ToolContext) -> Result<Option<Value>, ToolError> {
    let path = ctx.paths.config_file();
    let path_text = path.display().to_string();
    match xpst_config::ConfigDocument::read(&path) {
        Ok(document) => serde_json::to_value(document.raw())
            .map(Some)
            .map_err(|err| ToolError::Malformed {
                kind: "config file",
                path: path_text,
                detail: err.to_string(),
            }),
        Err(xpst_config::ConfigError::NotFound { .. }) => Ok(None),
        Err(err) => Err(ToolError::Malformed {
            kind: "config file",
            path: path_text,
            detail: err.to_string(),
        }),
    }
}

/// Merge the file onto the model: objects merge per key, everything else is
/// replaced by the file's value when the file has one.
///
/// Nested dataclasses are the exception, and the reason `repr_defaults` is
/// captured: Python constructs them as `ClassName(**block)`, so a partial block
/// in `config.yaml` falls back to the *dataclass* defaults (usually `None`),
/// not to the shipped preset. `video.encoding_<p>` is the model's flattened
/// name for the file's `video.encoding.<p>` block.
fn project(model: &Value, file: Option<&Value>, classes: &Value, defaults: &Value) -> Value {
    let mut projection = merge(model, file);
    let Some(classes) = classes.as_object() else {
        return projection;
    };
    for path in classes.keys() {
        let segments: Vec<&str> = path.split('.').collect();
        if segments.len() < 2 || (segments.len() == 2 && segments[0] == "accounts") {
            continue;
        }
        let file_segments = file_segments(&segments);
        let Some(file_block) = lookup(file, &file_segments) else {
            continue;
        };
        // `repr_defaults` is keyed by the dotted path, not nested.
        let base = defaults.get(path).cloned().unwrap_or(Value::Null);
        let merged = merge(&base, Some(file_block));
        if let Some(slot) = navigate_mut(&mut projection, &segments) {
            *slot = merged;
        }
    }
    projection
}

/// `video.encoding_youtube` is `video.encoding.youtube` in `config.yaml`.
fn file_segments<'a>(segments: &[&'a str]) -> Vec<&'a str> {
    if segments.len() == 2 && segments[0] == "video" {
        if let Some(platform) = segments[1].strip_prefix("encoding_") {
            return vec!["video", "encoding", platform];
        }
    }
    segments.to_vec()
}

fn lookup<'a>(root: Option<&'a Value>, segments: &[&str]) -> Option<&'a Value> {
    let mut current = root?;
    for segment in segments {
        current = current.as_object()?.get(*segment)?;
    }
    Some(current)
}

fn merge(model: &Value, file: Option<&Value>) -> Value {
    match (model, file) {
        (Value::Object(model_map), Some(Value::Object(file_map))) => {
            let mut out = Map::new();
            for (key, value) in model_map {
                out.insert(key.clone(), merge(value, file_map.get(key)));
            }
            Value::Object(out)
        }
        (Value::Object(_), _) => model.clone(),
        (_, Some(value)) => value.clone(),
        (_, None) => model.clone(),
    }
}

/// `_mask_sensitive_values()`: a key containing a sensitive keyword whose value
/// is a non-empty string becomes `***`. Empty strings stay empty so "not
/// configured" remains visible.
fn mask(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, child) in map.iter_mut() {
                let lower = key.to_lowercase();
                let sensitive = xpst_config::SENSITIVE_KEYWORDS
                    .iter()
                    .any(|keyword| lower.contains(keyword));
                match child {
                    Value::String(text) if sensitive && !text.is_empty() => {
                        *text = "***".to_string();
                    }
                    other => mask(other),
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(mask),
        _ => {}
    }
}

/// Expand a leading `~` in the fields the Python model types as `Path`.
///
/// `XPSTConfig.load()` runs `Path.expanduser()` on them, so the shipped tool
/// reports `$HOME/.xpst/downloads` where the file says `~/.xpst/downloads`.
/// Masked fields are already `***` and are skipped by the `~/` test.
fn expand_paths(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, child) in map.iter_mut() {
                if PATH_FIELDS.contains(&key.as_str()) {
                    if let Value::String(text) = child {
                        if text.starts_with("~/") {
                            *text = xpst_config::expand_home(text);
                        }
                    }
                }
                expand_paths(child);
            }
        }
        Value::Array(items) => items.iter_mut().for_each(expand_paths),
        _ => {}
    }
}

/// Replace every nested dataclass with Python's `repr()` of it.
///
/// `repr_classes` names the paths (`video.encoding_youtube`,
/// `accounts.tiktok.aggregator`); the section roots stay objects because Python
/// renders them with `dict(section.__dict__)`.
fn render_reprs(projection: &mut Value, classes: &Value) {
    let Some(classes) = classes.as_object() else {
        return;
    };
    for (path, class) in classes {
        let Some(class) = class.as_str() else {
            continue;
        };
        let segments: Vec<&str> = path.split('.').collect();
        if segments.len() < 2 {
            continue;
        }
        // `accounts.<platform>` is a section root: Python renders it with
        // `dict(section.__dict__)`, so it stays an object.
        if segments.len() == 2 && segments[0] == "accounts" {
            continue;
        }
        let Some(target) = navigate_mut(projection, &segments) else {
            continue;
        };
        if target.is_object() {
            *target = Value::String(format!("{class}({})", repr_fields(target)));
        }
    }
}

fn navigate_mut<'a>(root: &'a mut Value, segments: &[&str]) -> Option<&'a mut Value> {
    let mut current = root;
    for segment in segments {
        current = current.as_object_mut()?.get_mut(*segment)?;
    }
    Some(current)
}

/// `field=value, field=value` in the model's field order.
fn repr_fields(object: &Value) -> String {
    let Some(map) = object.as_object() else {
        return String::new();
    };
    map.iter()
        .map(|(key, value)| format!("{key}={}", python_repr(value)))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Python's `repr()` for the scalar and collection values these dataclasses
/// hold: `None`, `True`/`False`, numbers, single-quoted strings, and the two
/// collection forms.
fn python_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => python_str_repr(text),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(python_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(key, item)| format!("{}: {}", python_str_repr(key), python_repr(item)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// Python's `repr()` for a string: single quotes unless the value contains one
/// and no double quote, with backslashes and the quote escaped.
fn python_str_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_captured_model_carries_every_section_python_serves() {
        let document: Value = serde_json::from_str(MODEL).expect("fixture parses");
        let model = &document["model"];
        assert_eq!(
            model
                .as_object()
                .map(|map| map.keys().cloned().collect::<Vec<_>>()),
            Some(vec![
                "accounts".to_string(),
                "video".to_string(),
                "monitoring".to_string(),
                "schedule".to_string()
            ])
        );
        let accounts = model["accounts"].as_object().expect("accounts");
        assert_eq!(
            accounts.keys().cloned().collect::<Vec<_>>(),
            vec![
                "tiktok".to_string(),
                "youtube".to_string(),
                "x".to_string(),
                "instagram".to_string(),
                "threads".to_string(),
                "messenger".to_string(),
                "local".to_string()
            ]
        );
        assert_eq!(
            document["repr_classes"]["video.encoding_youtube"],
            "EncodingConfig"
        );
        assert_eq!(
            document["repr_classes"]["accounts.tiktok.aggregator"],
            "TikTokAggregatorConfig"
        );
    }

    #[test]
    fn repr_matches_pythons_for_the_values_these_dataclasses_hold() {
        assert_eq!(python_repr(&Value::Null), "None");
        assert_eq!(python_repr(&Value::Bool(true)), "True");
        assert_eq!(python_repr(&serde_json::json!(1920)), "1920");
        assert_eq!(python_repr(&serde_json::json!("8M")), "'8M'");
        assert_eq!(python_repr(&serde_json::json!(["a", "b"])), "['a', 'b']");
        assert_eq!(python_repr(&serde_json::json!({"k": 1})), "{'k': 1}");
        // A value containing a single quote is double-quoted, like Python.
        assert_eq!(python_repr(&serde_json::json!("it's")), "\"it's\"");
    }

    #[test]
    fn the_file_overrides_the_model_field_by_field() {
        let model = serde_json::json!({
            "accounts": {"youtube": {"enabled": true, "username": ""}},
            "video": {"download_dir": "~/.xpst/downloads", "encoding_youtube": {"resolution": 1920, "fps": 60}},
            "monitoring": {"log_level": "INFO"},
            "schedule": {"check_interval": 300}
        });
        let classes = serde_json::json!({
            "accounts.youtube": "YouTubeAccountConfig",
            "video": "VideoConfig",
            "video.encoding_youtube": "EncodingConfig"
        });
        let defaults = serde_json::json!({
            "video.encoding_youtube": {"resolution": null, "fps": null, "color": "bt709"}
        });
        let file = serde_json::json!({
            "accounts": {"youtube": {"username": "alice"}},
            "video": {"encoding": {"youtube": {"resolution": 1080}}},
            "monitoring": {"log_level": "DEBUG"}
        });
        let projection = project(&model, Some(&file), &classes, &defaults);
        assert_eq!(projection["accounts"]["youtube"]["enabled"], true);
        assert_eq!(projection["accounts"]["youtube"]["username"], "alice");
        // A partial encoding block is `EncodingConfig(**block)`: the fields it
        // omits fall back to the *dataclass* defaults, not to the shipped
        // youtube preset (`fps: 60` in the model).
        assert_eq!(projection["video"]["encoding_youtube"]["resolution"], 1080);
        assert_eq!(projection["video"]["encoding_youtube"]["fps"], Value::Null);
        assert_eq!(projection["video"]["encoding_youtube"]["color"], "bt709");
        assert_eq!(projection["monitoring"]["log_level"], "DEBUG");
        assert_eq!(projection["schedule"]["check_interval"], 300);
    }

    #[test]
    fn masking_keeps_empty_strings_and_masks_non_empty_secrets() {
        let mut value = serde_json::json!({
            "client_secret": "abc",
            "client_key": "",
            "auth_mode": "cookies",
            "nested": {"session_file": "/tmp/x"}
        });
        mask(&mut value);
        assert_eq!(value["client_secret"], "***");
        assert_eq!(value["client_key"], "");
        assert_eq!(value["auth_mode"], "***");
        assert_eq!(value["nested"]["session_file"], "***");
    }
}
