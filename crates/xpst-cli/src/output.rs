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
pub fn write_json<W: Write>(out: &mut W, value: &Value) -> std::io::Result<()> {
    writeln!(out, "{value}")
}

/// Write a block of human-readable text, ensuring it ends with a newline.
pub fn write_text<W: Write>(out: &mut W, text: &str) -> std::io::Result<()> {
    if text.ends_with('\n') {
        write!(out, "{text}")
    } else {
        writeln!(out, "{text}")
    }
}
