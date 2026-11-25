//! Output helpers shared by the implemented read-only commands.
//!
//! The Python engine prints machine-readable output as single-line JSON
//! (`json_output()` in `cli.py` calls `json.dumps(data, default=str,
//! ensure_ascii=False)`), with secrets masked unless `--raw` was passed. That
//! means Python's default separators (`", "` / `": "`), insertion order and raw
//! UTF-8 — not serde's compact form. [`write_python_json`] is the renderer that
//! reproduces it, and every `--json` command in this crate uses it so a script
//! can `cmp` the two engines' stdout.

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
// [`crate::Value`], whose `Mapping` preserves insertion order, and render
// it with [`crate::python_style_json`] — the same pair `config show`
// already uses to be byte-identical.

/// An insertion-ordered JSON object.
pub fn object(entries: Vec<(&str, crate::Value)>) -> crate::Value {
    let mut map = crate::Mapping::new();
    for (key, value) in entries {
        map.insert(crate::Value::from(key), value);
    }
    crate::Value::Mapping(map)
}

/// An insertion-ordered JSON array.
pub fn array(items: Vec<crate::Value>) -> crate::Value {
    crate::Value::Sequence(items)
}

/// `_error_payload()` in Python's key order: `{ok: false, error: {code, message, ...}}`.
///
/// [`error_payload`] builds the same shape through `serde_json`; this one exists
/// for the call sites that already build an ordered payload and want the error
/// object to slot into it.
pub fn error_object(code: &str, message: &str, extra: Vec<(&str, crate::Value)>) -> crate::Value {
    let mut error = vec![("code", string(code)), ("message", string(message))];
    error.extend(extra);
    object(vec![("ok", boolean(false)), ("error", object(error))])
}

/// A JSON string.
pub fn string(text: impl Into<String>) -> crate::Value {
    crate::Value::String(text.into())
}

/// A JSON boolean.
pub fn boolean(flag: bool) -> crate::Value {
    crate::Value::Bool(flag)
}

/// A JSON integer.
pub fn integer(number: i64) -> crate::Value {
    crate::Value::Number(number.into())
}

/// A JSON float (Python renders it with `repr`, so `1767225600.0` keeps its
/// `.0`).
pub fn number(value: f64) -> crate::Value {
    serde_json::Number::from_f64(value).map_or(crate::Value::Null, crate::Value::Number)
}

/// A JSON `null`.
pub fn null() -> crate::Value {
    crate::Value::Null
}

/// Greedy word wrap at `width` columns, the way Rich wraps a plain message.
///
/// Only used where the Python command prints through `console.print` and the
/// console is not a TTY (so Rich uses its default 80-column width). A message
/// that fits stays on one line; a longer one breaks at a space and keeps the
/// trailing space, which is what Rich emits.
pub fn wrap_at(text: &str, width: usize) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut current = String::new();
    for word in text.split(' ') {
        let candidate_len = if current.is_empty() {
            word.chars().count()
        } else {
            current.chars().count() + 1 + word.chars().count()
        };
        if candidate_len > width && !current.is_empty() {
            current.push(' ');
            lines.push(std::mem::take(&mut current));
            current.push_str(word);
        } else {
            if !current.is_empty() {
                current.push(' ');
            }
            current.push_str(word);
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines.join("\n")
}

/// Write a payload the way `json_output()` does: one line, Python separators,
/// insertion order, trailing newline.
pub fn write_python_json(out: &mut dyn Write, value: &crate::Value) -> std::io::Result<()> {
    let text = crate::python_style_json(value)
        .map_err(|err| std::io::Error::other(format!("cannot render JSON: {err}")))?;
    writeln!(out, "{text}")
}

/// [`write_python_json`] for a `serde_json` document.
///
/// `serde_json` is built with `preserve_order` (workspace `Cargo.toml`), so a
/// document that came from a parsed file keeps the file's key order and a
/// `json!` literal keeps the order it was written in — which is how the payloads
/// below are written, in Python's dict-literal order. This is the one renderer
/// every `--json` command uses.
pub fn write_python_json_value(
    out: &mut dyn Write,
    value: &serde_json::Value,
) -> std::io::Result<()> {
    write_python_json(out, &from_json(value))
}

/// Convert a `serde_json` document into the insertion-ordered value type.
/// `serde_json` is built with `preserve_order`, so the document's key order is
/// already the order Python would print; this conversion keeps it. It exists so
/// a `serde_json` payload can be rendered with
/// [`crate::python_style_json`] — Python's `", "` / `": "` separators —
/// instead of serde's compact form.
pub fn from_json(value: &serde_json::Value) -> crate::Value {
    use crate::Value as Out;
    use serde_json::Value as Json;
    match value {
        Json::Null => Out::Null,
        Json::Bool(flag) => Out::Bool(*flag),
        Json::Number(number) => Out::Number(number.clone()),
        Json::String(text) => Out::String(text.clone()),
        Json::Array(items) => Out::Sequence(items.iter().map(from_json).collect()),
        Json::Object(map) => {
            let mut out = crate::Mapping::new();
            for (key, item) in map {
                out.insert(Out::String(key.clone()), from_json(item));
            }
            Out::Mapping(out)
        }
    }
}

/// Convert an insertion-ordered value into a `serde_json` document.
///
/// The inverse of [`from_json`], for the MCP handlers: the payload builders in
/// `xpst-surface` return [`crate::Value`] (insertion order is the contract), and
/// the MCP server's handler type is `serde_json::Value`. `serde_json` is built
/// with `preserve_order`, so the key order survives the conversion.
pub fn to_json(value: &crate::Value) -> serde_json::Value {
    use crate::Value as In;
    use serde_json::Value as Json;
    match value {
        In::Null => Json::Null,
        In::Bool(flag) => Json::Bool(*flag),
        In::Number(number) => Json::Number(number.clone()),
        // `json.dumps` writes these as bare `NaN`/`Infinity`; serde_json has no
        // such literal, so the token is carried as its string form.
        In::NonFinite(kind) => Json::String(kind.as_json().to_string()),
        In::Timestamp(text) | In::String(text) => Json::String(text.clone()),
        In::Sequence(items) => Json::Array(items.iter().map(to_json).collect()),
        In::Mapping(map) => {
            let mut out = serde_json::Map::new();
            for (key, item) in map {
                let key = match key {
                    In::String(text) | In::Timestamp(text) => text.clone(),
                    other => crate::python_style_json(other).unwrap_or_default(),
                };
                out.insert(key, to_json(item));
            }
            Json::Object(out)
        }
    }
}

/// Render a `serde_json` document the way `json.dumps(value, indent=2)` does.
///
/// Python's default `ensure_ascii=True` escapes every character outside
/// printable ASCII as `\uXXXX` (a surrogate pair above the BMP), which is why
/// `security-audit` and `mcp list` carry `\u2014` rather than an em dash. Items
/// are separated by `","` with no trailing space, a key is followed by `": "`,
/// and the indent is two spaces — exactly `json.dumps(indent=2)`.
pub fn python_pretty_json(value: &serde_json::Value) -> String {
    // One renderer for the whole workspace: `xpst-config` owns the byte-exact
    // `json.dumps` emulation, so the CLI and the MCP server cannot drift.
    crate::python_style_json_pretty(value).unwrap_or_default()
}

/// The same rendering with `ensure_ascii=False`: raw UTF-8, as
/// `json.dump(..., ensure_ascii=False)` writes `schedule.json`.
pub fn python_pretty_json_utf8(value: &serde_json::Value) -> String {
    crate::python_style_json_pretty_utf8(value).unwrap_or_default()
}

/// `json.dumps(value, indent=2)` for the insertion-ordered value type.
///
/// The ordered twin of [`python_pretty_json`]: a payload built with
/// [`object`]/[`array`] keeps Python's key order explicitly, independent of how
/// the `serde_json` map is configured.
pub fn python_pretty_json_cfg(value: &crate::Value) -> String {
    let mut out = String::new();
    write_pretty_cfg(value, 0, true, &mut out);
    out
}

/// [`python_pretty_json_cfg`] with `ensure_ascii=False` (raw UTF-8).
pub fn python_pretty_json_cfg_utf8(value: &crate::Value) -> String {
    let mut out = String::new();
    write_pretty_cfg(value, 0, false, &mut out);
    out
}

fn write_pretty_cfg(value: &crate::Value, depth: usize, ascii_only: bool, out: &mut String) {
    use crate::Value as Cfg;
    match value {
        Cfg::Null => out.push_str("null"),
        Cfg::Bool(true) => out.push_str("true"),
        Cfg::Bool(false) => out.push_str("false"),
        Cfg::Number(number) => {
            if let Some(integer) = number.as_i64() {
                out.push_str(&integer.to_string());
            } else if let Some(integer) = number.as_u64() {
                out.push_str(&integer.to_string());
            } else {
                out.push_str(&crate::pyyaml::python_json_float(
                    number.as_f64().unwrap_or_default(),
                ));
            }
        }
        Cfg::NonFinite(kind) => out.push_str(kind.as_json()),
        Cfg::Timestamp(text) | Cfg::String(text) => write_pretty_string(text, ascii_only, out),
        Cfg::Sequence(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push_str("[\n");
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(",\n");
                }
                indent(depth + 1, out);
                write_pretty_cfg(item, depth + 1, ascii_only, out);
            }
            out.push('\n');
            indent(depth, out);
            out.push(']');
        }
        Cfg::Mapping(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push_str("{\n");
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push_str(",\n");
                }
                indent(depth + 1, out);
                match key {
                    Cfg::String(text) | Cfg::Timestamp(text) => {
                        write_pretty_string(text, ascii_only, out)
                    }
                    other => {
                        let rendered = crate::python_style_json(other).unwrap_or_default();
                        write_pretty_string(&rendered, ascii_only, out);
                    }
                }
                out.push_str(": ");
                write_pretty_cfg(item, depth + 1, ascii_only, out);
            }
            out.push('\n');
            indent(depth, out);
            out.push('}');
        }
    }
}

fn indent(depth: usize, out: &mut String) {
    for _ in 0..depth {
        out.push_str("  ");
    }
}

/// `json.dumps` string escaping, with `ensure_ascii` as a switch.
fn write_pretty_string(text: &str, ascii_only: bool, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (' '..='~').contains(&c) => out.push(c),
            // Python's `py_encode_basestring` escapes `[\x00-\x1f]` whether or
            // not `ensure_ascii` is set — only the *non-ASCII* range depends on
            // the flag. Emitting a raw control byte here produced a
            // `schedule.json` that neither engine could read back.
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if !ascii_only => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// Python's `repr()` for a short string: single quotes unless the value itself
/// contains one (and no double quote).
///
/// Used where the Python engine interpolates `{value!r}` into a message — the
/// quoting is part of the message, so it has to match.
pub fn python_repr(text: &str) -> String {
    if text.contains('\'') && !text.contains('"') {
        format!("\"{text}\"")
    } else {
        format!("'{text}'")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pretty_json_matches_pythons_indent_two() {
        let value = serde_json::json!({"a": [1, 2], "b": {"c": "d"}, "e": [], "f": {}});
        assert_eq!(
            python_pretty_json(&value),
            "{\n  \"a\": [\n    1,\n    2\n  ],\n  \"b\": {\n    \"c\": \"d\"\n  },\n  \"e\": [],\n  \"f\": {}\n}"
        );
    }

    #[test]
    fn non_ascii_is_escaped_like_ensure_ascii() {
        assert_eq!(
            python_pretty_json(&serde_json::json!("a—b")),
            "\"a\\u2014b\""
        );
        assert_eq!(
            python_pretty_json(&serde_json::json!("😀")),
            "\"\\ud83d\\ude00\""
        );
        assert_eq!(
            python_pretty_json(&serde_json::json!("\u{7f}")),
            "\"\\u007f\""
        );
    }

    #[test]
    fn floats_render_like_python_repr() {
        assert_eq!(
            python_pretty_json(&serde_json::json!(1767225600.0)),
            "1767225600.0"
        );
        assert_eq!(python_pretty_json(&serde_json::json!(1.5)), "1.5");
    }

    #[test]
    fn control_characters_are_escaped_with_ensure_ascii_false() {
        // Python's `py_encode_basestring` escapes `[\x00-\x1f]` in both modes;
        // only the non-ASCII range depends on `ensure_ascii`. A raw 0x01 here
        // made `schedule remove` write a `schedule.json` that neither engine
        // could read back.
        assert_eq!(
            python_pretty_json_utf8(&serde_json::json!("a\u{1}b")),
            "\"a\\u0001b\""
        );
        assert_eq!(
            python_pretty_json_utf8(&serde_json::json!("\u{0}\u{1f}")),
            "\"\\u0000\\u001f\""
        );
        // The named escapes keep their short forms, and 0x7f stays raw in the
        // `ensure_ascii=False` path (Python's ESCAPE regex stops at 0x1f).
        assert_eq!(
            python_pretty_json_utf8(&serde_json::json!("\n\r\t\u{8}\u{c}")),
            "\"\\n\\r\\t\\b\\f\""
        );
        assert_eq!(
            python_pretty_json_utf8(&serde_json::json!("\u{7f}")),
            "\"\u{7f}\""
        );
        // Non-ASCII is still raw in this mode.
        assert_eq!(
            python_pretty_json_utf8(&serde_json::json!("a—b")),
            "\"a—b\""
        );
        // The ASCII-escaped mode is unchanged.
        assert_eq!(
            python_pretty_json(&serde_json::json!("a\u{1}b")),
            "\"a\\u0001b\""
        );
    }
}
