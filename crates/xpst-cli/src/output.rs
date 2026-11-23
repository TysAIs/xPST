//! Output helpers shared by the implemented read-only commands.
//!
//! The Python engine prints machine-readable output as compact single-line
//! JSON (`json_output()` in `cli.py`), with secrets masked unless `--raw`
//! was passed. The Rust read paths keep that shape so a script can diff the
//! two engines without a formatter in between.

use std::io::Write;

use serde_json::Value;

/// An error object with the same shape as `_error_payload()` in `cli.py`.
pub fn error_payload(code: &str, message: &str) -> Value {
    serde_json::json!({
        "ok": false,
        "error": {
            "code": code,
            "message": message,
        }
    })
}

/// An error object plus command-specific context fields.
pub fn error_payload_with(code: &str, message: &str, extra: Value) -> Value {
    let mut payload = error_payload(code, message);
    if let (Some(object), Value::Object(extra)) = (payload.get_mut("error"), extra) {
        if let Some(object) = object.as_object_mut() {
            for (key, value) in extra {
                object.insert(key, value);
            }
        }
    }
    payload
}

/// Write a JSON document on one line, exactly as `json_output()` does.
pub fn write_json(out: &mut dyn Write, value: &Value) -> std::io::Result<()> {
    writeln!(out, "{value}")
}

/// Write a block of human-readable text, ensuring it ends with a newline.
pub fn write_text(out: &mut dyn Write, text: &str) -> std::io::Result<()> {
    if text.ends_with('\n') {
        write!(out, "{text}")
    } else {
        writeln!(out, "{text}")
    }
}

// ---------------------------------------------------------------------------
// Byte-exact JSON for the commands ported after P0
// ---------------------------------------------------------------------------
//
// `serde_json::json!` builds a `BTreeMap`, so it emits keys in alphabetical
// order with compact separators. Python's `json.dumps` emits insertion order
// with `", "` / `": "`. Both are valid JSON and the P0 parity fixture compares
// parsed payloads, so the difference was invisible — but it means `cmp` on the
// two engines' stdout fails, which is exactly the check a script (or a
// reviewer) reaches for first.
//
// The commands in this tranche therefore build their payload from
// [`xpst_config::Value`], whose `Mapping` preserves insertion order, and render
// it with [`xpst_config::python_style_json`] — the same pair `config show`
// already uses to be byte-identical.

/// An insertion-ordered JSON object.
pub fn object(entries: Vec<(&str, xpst_config::Value)>) -> xpst_config::Value {
    let mut map = xpst_config::Mapping::new();
    for (key, value) in entries {
        map.insert(xpst_config::Value::from(key), value);
    }
    xpst_config::Value::Mapping(map)
}

/// An insertion-ordered JSON array.
pub fn array(items: Vec<xpst_config::Value>) -> xpst_config::Value {
    xpst_config::Value::Sequence(items)
}

/// A JSON string.
pub fn string(text: impl Into<String>) -> xpst_config::Value {
    xpst_config::Value::String(text.into())
}

/// A JSON boolean.
pub fn boolean(flag: bool) -> xpst_config::Value {
    xpst_config::Value::Bool(flag)
}

/// A JSON integer.
pub fn integer(number: i64) -> xpst_config::Value {
    xpst_config::Value::Number(number.into())
}

/// A JSON `null`.
pub fn null() -> xpst_config::Value {
    xpst_config::Value::Null
}

/// Write a payload the way `json_output()` does: one line, Python separators,
/// insertion order, trailing newline.
pub fn write_python_json(out: &mut dyn Write, value: &xpst_config::Value) -> std::io::Result<()> {
    let text = xpst_config::python_style_json(value)
        .map_err(|err| std::io::Error::other(format!("cannot render JSON: {err}")))?;
    writeln!(out, "{text}")
}
