//! `messenger_set_rules` — the Messenger ManyChat-lite auto-reply rules.
//!
//! The handler is four lines of Python:
//!
//! ```text
//! rules = args.get("rules") or {}
//! config.messenger.auto_reply = bool(args.get("auto_reply", True))
//! config.messenger.reply_rules = {str(k): str(v) for k, v in rules.items()}
//! config.save()
//! ```
//!
//! so the whole slice is the coercion order plus the config write. Three
//! details are the contract, not incidental:
//!
//! * `or {}` is TRUTHINESS, not presence — `null`, `{}`, `""`, `0` and `[]` all
//!   become an empty map, while a truthy non-object reaches `.items()` and
//!   raises `'<type>' object has no attribute 'items'`;
//! * `bool(args.get("auto_reply", True))` means an ABSENT key is true, a `null`
//!   is false, and the string `"false"` is true;
//! * both sides of the map are `str()`-coerced, so a numeric reply reaches the
//!   file as `'5'`.
//!
//! `config.save()` rewrites the whole `config.yaml` (see
//! [`crate::config_save`]), which is why this tool can drop a setting it never
//! touched — `accounts.messenger.comment_reply_enabled` is the one an operator
//! is most likely to notice.

use serde_json::Value as Json;

use xpst_config::yaml::Mapping;
use xpst_config::Value;

use crate::config_save::{save_document, write, SaveRequest};

/// The tool's payload, in the handler's key order.
#[derive(Debug, Clone, PartialEq)]
pub struct RulesOutcome {
    pub auto_reply: bool,
    pub reply_rules: Vec<(String, String)>,
}

impl RulesOutcome {
    pub fn to_payload(&self) -> Value {
        let mut rules = Mapping::new();
        for (key, value) in &self.reply_rules {
            rules.insert(Value::from(key.as_str()), Value::String(value.clone()));
        }
        xpst_config::output::object(vec![
            ("ok", Value::Bool(true)),
            ("auto_reply", Value::Bool(self.auto_reply)),
            ("reply_rules", Value::Mapping(rules)),
        ])
    }
}

/// Why the handler refused, with Python's own sentence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulesError {
    pub message: String,
}

/// Apply `messenger_set_rules` to a profile and persist the config.
pub fn set_rules(
    arguments: &Json,
    file: Option<&Value>,
    request: &SaveRequest<'_>,
) -> Result<RulesOutcome, RulesError> {
    // `args.get("rules") or {}`
    let rules = match arguments.get("rules") {
        Some(Json::Object(map)) if !map.is_empty() => map.clone(),
        Some(value) if truthy_json(Some(value)) => {
            // `.items()` on a truthy non-object.
            return Err(RulesError {
                message: format!(
                    "'{}' object has no attribute 'items'",
                    python_type_name(value)
                ),
            });
        }
        _ => serde_json::Map::new(),
    };

    // `bool(args.get("auto_reply", True))`
    let auto_reply = match arguments.get("auto_reply") {
        None => true,
        Some(value) => truthy_json(Some(value)),
    };

    // `{str(k): str(v) for k, v in rules.items()}`
    let mut coerced: Vec<(String, String)> = Vec::new();
    for (key, value) in &rules {
        coerced.push((key.clone(), python_str_json(value)));
    }

    let mut document = save_document(file, request).map_err(|message| RulesError { message })?;
    let mut mapping = Mapping::new();
    for (key, value) in &coerced {
        mapping.insert(Value::from(key.as_str()), Value::String(value.clone()));
    }
    set(
        &mut document,
        &["accounts", "messenger", "auto_reply"],
        Value::Bool(auto_reply),
    );
    set(
        &mut document,
        &["accounts", "messenger", "reply_rules"],
        Value::Mapping(mapping),
    );
    write(&request.config_dir.join("config.yaml"), &document)
        .map_err(|message| RulesError { message })?;

    Ok(RulesOutcome {
        auto_reply,
        reply_rules: coerced,
    })
}

fn set(document: &mut Value, path: &[&str], value: Value) {
    let Some((last, parents)) = path.split_last() else {
        return;
    };
    let mut current = document;
    for segment in parents {
        let Some(next) = current
            .as_mapping_mut()
            .and_then(|map| map.get_mut(segment))
        else {
            return;
        };
        current = next;
    }
    if let Some(map) = current.as_mapping_mut() {
        if let Some(slot) = map.get_mut(last) {
            *slot = value;
        }
    }
}

/// Python truthiness for a JSON argument.
fn truthy_json(value: Option<&Json>) -> bool {
    match value {
        None | Some(Json::Null) => false,
        Some(Json::Bool(flag)) => *flag,
        Some(Json::Number(number)) => number.as_f64().is_some_and(|value| value != 0.0),
        Some(Json::String(text)) => !text.is_empty(),
        Some(Json::Array(items)) => !items.is_empty(),
        Some(Json::Object(map)) => !map.is_empty(),
    }
}

/// `type(value).__name__` for a JSON value, as Python names it.
fn python_type_name(value: &Json) -> &'static str {
    match value {
        Json::Null => "NoneType",
        Json::Bool(_) => "bool",
        Json::Number(number) => {
            if number.is_i64() || number.is_u64() {
                "int"
            } else {
                "float"
            }
        }
        Json::String(_) => "str",
        Json::Array(_) => "list",
        Json::Object(_) => "dict",
    }
}

/// Python's `str()` for a JSON value.
fn python_str_json(value: &Json) -> String {
    match value {
        Json::Null => "None".to_string(),
        Json::Bool(true) => "True".to_string(),
        Json::Bool(false) => "False".to_string(),
        Json::String(text) => text.clone(),
        Json::Number(number) => match number.as_i64() {
            Some(integer) => integer.to_string(),
            None => number
                .as_f64()
                .map(xpst_config::pyyaml::python_float_repr)
                .unwrap_or_else(|| number.to_string()),
        },
        // `str([1, 2])` is `[1, 2]` and `str({"a": 1})` is `{'a': 1}` — Python's
        // `repr`, not JSON. A container reply is nonsense, but the coercion is
        // what the shipped handler does, so it is reproduced rather than
        // refused.
        other => python_repr(other),
    }
}

fn python_repr(value: &Json) -> String {
    match value {
        Json::Null => "None".to_string(),
        Json::Bool(true) => "True".to_string(),
        Json::Bool(false) => "False".to_string(),
        Json::String(text) => format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'")),
        Json::Number(_) => python_str_json(value),
        Json::Array(items) => {
            let parts: Vec<String> = items.iter().map(python_repr).collect();
            format!("[{}]", parts.join(", "))
        }
        Json::Object(map) => {
            let parts: Vec<String> = map
                .iter()
                .map(|(key, item)| format!("'{}': {}", key, python_repr(item)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_truthy_non_object_is_pythons_attribute_error() {
        for (value, name) in [
            (serde_json::json!("abc"), "str"),
            (serde_json::json!([1, 2]), "list"),
            (serde_json::json!(5), "int"),
            (serde_json::json!(true), "bool"),
        ] {
            let arguments = serde_json::json!({ "rules": value });
            let error = set_rules(
                &arguments,
                None,
                &SaveRequest {
                    config_dir: std::path::Path::new("/nonexistent-profile"),
                    home: std::path::Path::new("/nonexistent-home"),
                    env: &|_| None,
                    store_secret: &|_, _, _| true,
                },
            )
            .expect_err("must refuse");
            assert_eq!(
                error.message,
                format!("'{name}' object has no attribute 'items'")
            );
        }
    }

    #[test]
    fn a_falsy_rules_argument_is_an_empty_map() {
        for value in [
            serde_json::json!(null),
            serde_json::json!({}),
            serde_json::json!(""),
            serde_json::json!(0),
            serde_json::json!([]),
        ] {
            let arguments = serde_json::json!({ "rules": value });
            // The write fails on a nonexistent profile, but the coercion runs
            // first — so the error names the write, not the argument.
            let error = set_rules(
                &arguments,
                None,
                &SaveRequest {
                    config_dir: std::path::Path::new("/nonexistent-profile"),
                    home: std::path::Path::new("/nonexistent-home"),
                    env: &|_| None,
                    store_secret: &|_, _, _| true,
                },
            )
            .expect_err("the write must fail");
            assert!(
                !error.message.contains("has no attribute"),
                "a falsy rules value must not reach .items(): {error:?}"
            );
        }
    }

    #[test]
    fn python_str_coerces_a_numeric_reply() {
        assert_eq!(python_str_json(&serde_json::json!(5)), "5");
        assert_eq!(python_str_json(&serde_json::json!(5.5)), "5.5");
        assert_eq!(python_str_json(&serde_json::json!(true)), "True");
        assert_eq!(python_str_json(&serde_json::json!(null)), "None");
        assert_eq!(python_str_json(&serde_json::json!("x")), "x");
        assert_eq!(python_str_json(&serde_json::json!([1, 2])), "[1, 2]");
        assert_eq!(python_str_json(&serde_json::json!({"a": 1})), "{'a': 1}");
    }
}
