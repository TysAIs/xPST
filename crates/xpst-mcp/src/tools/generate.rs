//! `xpst_suggest_caption` and `xpst_generate_ideas` — the deterministic content
//! generators over MCP.
//!
//! Ports `_handle_suggest_caption()` and `_handle_generate_ideas()` over
//! `xpst.caption_gen.generate_caption` / `generate_ideas`.
//!
//! **Both are deterministic-only over MCP, and that is the shipped behaviour.**
//! `caption_gen.generate_caption` is `asyncio.run(generate_caption_llm(...))`
//! inside `except RuntimeError`. The MCP server dispatches handlers from inside
//! a running event loop, so `asyncio.run` always raises
//! `RuntimeError: asyncio.run() cannot be called from a running event loop` and
//! the handler always lands on the deterministic branch — even with
//! `XPST_KB_LLM_ENABLED` and a base URL configured. The LLM client is therefore
//! not a gap in this slice: it is unreachable on this surface. (The CLI is a
//! separate process with no loop, so there the LLM path *is* reachable and the
//! CLI's divergence note stands.)
//!
//! Two Python quirks the wire pins:
//!
//! * `xpst_suggest_caption` passes `video_path` as the **transcript text**. The
//!   handler never reads the file — `generate_caption(video_path, ...)` treats
//!   the path string as the transcript, so a short path falls through to the
//!   fixed fallback captions and a long one produces hashtags from the path's
//!   own words. Reproduced, not fixed.
//! * `count` is passed straight to `int()` inside
//!   `generate_ideas_deterministic`, so a `null` is a `TypeError` that escapes
//!   the handler as `Error: int() argument must be ...` rather than a clamp.

use serde_json::{json, Value};

use crate::error::ToolError;

use super::ToolContext;

/// `_handle_suggest_caption()`.
pub fn suggest_caption(_ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let video_path = arguments.get("video_path");
    let platform = arguments.get("platform");

    // `arguments.get("video_path", "")` then `if video_path else []` — the test
    // is Python truthiness, so an empty string is as falsy as an absent key.
    let transcript = match video_path {
        None => None,
        Some(Value::String(text)) if text.is_empty() => None,
        Some(Value::String(text)) => Some(text.as_str()),
        Some(Value::Null) => None,
        Some(other) => {
            // `re.split` on a non-string is a TypeError the handler does not
            // catch, so the body is `Error: expected string or bytes-like
            // object, got '<type>'`.
            return Err(ToolError::Handler {
                message: format!(
                    "expected string or bytes-like object, got '{}'",
                    python_type_name(other)
                ),
            });
        }
    };

    // `arguments.get("platform", "instagram")` — an explicit `null` stays null
    // in the payload and falls back to the 2200 limit, exactly as
    // `PLATFORM_CHAR_LIMITS.get(None, 2200)` does.
    let platform_value = platform.cloned().unwrap_or_else(|| json!("instagram"));
    let platform_name = platform.and_then(Value::as_str).unwrap_or_default();

    let suggestions: Vec<Value> = match transcript {
        Some(text) => xpst_surface::generate::caption_variants(text, platform_name)
            .into_iter()
            .map(|(caption, hashtags, style)| {
                let mut fields = serde_json::Map::new();
                fields.insert("caption".to_string(), json!(caption));
                fields.insert("hashtags".to_string(), json!(hashtags));
                if let Some(style) = style {
                    fields.insert("style".to_string(), json!(style));
                }
                Value::Object(fields)
            })
            .collect(),
        None => Vec::new(),
    };

    Ok(json!({
        "video_path": video_path.cloned().unwrap_or_else(|| json!("")),
        "platform": platform_value,
        "suggestions": suggestions,
    }))
}

/// `_handle_generate_ideas()`.
pub fn generate_ideas(_ctx: &ToolContext, arguments: &Value) -> Result<Value, ToolError> {
    let topic = arguments.get("topic");
    let count = arguments.get("count");

    // `arguments.get("topic", "")` then `if topic else []` — Python truthiness,
    // so an empty string is as falsy as an absent key.
    let topic_text = match topic {
        None => None,
        Some(Value::String(text)) if text.is_empty() => None,
        Some(Value::String(text)) => Some(text.as_str()),
        Some(Value::Null) => None,
        Some(other) => {
            // `(topic or "").strip()` on a truthy non-string is an
            // AttributeError the handler does not catch.
            return Err(ToolError::Handler {
                message: format!(
                    "'{}' object has no attribute 'strip'",
                    python_type_name(other)
                ),
            });
        }
    };

    let ideas: Vec<Value> = match topic_text {
        Some(text) => {
            // `arguments.get("count", 5)` — absent means 5, and the value goes
            // straight into `int()`.
            let count = match count {
                None => 5,
                Some(value) => {
                    python_int(value).map_err(|message| ToolError::Handler { message })?
                }
            };
            let count = usize::try_from(count.max(0)).unwrap_or(0);
            xpst_surface::generate::idea_variants(text, count)
                .into_iter()
                .map(|(idea, hook)| json!({"idea": idea, "hook": hook, "source": "deterministic"}))
                .collect()
        }
        None => Vec::new(),
    };

    Ok(json!({
        "topic": topic.cloned().unwrap_or_else(|| json!("")),
        "count": ideas.len(),
        "ideas": ideas,
    }))
}

/// Python's `int(value)`, with the exception sentence it raises.
fn python_int(value: &Value) -> Result<i64, String> {
    match value {
        Value::Bool(flag) => Ok(i64::from(*flag)),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                return Ok(integer);
            }
            // `int(2.7)` truncates toward zero; `int(nan)`/`int(inf)` raise.
            match number.as_f64() {
                Some(float) if float.is_finite() => Ok(float as i64),
                _ => Err("cannot convert float NaN to integer".to_string()),
            }
        }
        Value::String(text) => parse_python_int(text),
        other => Err(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            python_type_name(other)
        )),
    }
}

/// `int("<text>")` — surrounding whitespace, an optional sign, and single
/// underscores between digits are all accepted.
fn parse_python_int(text: &str) -> Result<i64, String> {
    let trimmed = text.trim();
    let (sign, digits) = match trimmed.strip_prefix('-') {
        Some(rest) => (-1i64, rest),
        None => (1i64, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    let invalid = || format!("invalid literal for int() with base 10: '{text}'");
    if digits.is_empty() {
        return Err(invalid());
    }
    let mut value: i64 = 0;
    let mut previous_underscore = true; // a leading underscore is invalid
    for character in digits.chars() {
        if character == '_' {
            if previous_underscore {
                return Err(invalid());
            }
            previous_underscore = true;
            continue;
        }
        let digit = character.to_digit(10).ok_or_else(invalid)?;
        value = value
            .checked_mul(10)
            .and_then(|value| value.checked_add(i64::from(digit)))
            .ok_or_else(invalid)?;
        previous_underscore = false;
    }
    if previous_underscore {
        return Err(invalid());
    }
    Ok(sign * value)
}

/// The `type(value).__name__` Python would print in an exception sentence.
fn python_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(number) if number.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_matches_the_shipped_conversions() {
        assert_eq!(python_int(&json!(3)), Ok(3));
        assert_eq!(python_int(&json!(2.7)), Ok(2));
        assert_eq!(python_int(&json!(-2.7)), Ok(-2));
        assert_eq!(python_int(&json!(true)), Ok(1));
        assert_eq!(python_int(&json!(" 3 ")), Ok(3));
        assert_eq!(python_int(&json!("+3")), Ok(3));
        assert_eq!(python_int(&json!("1_0")), Ok(10));
        assert_eq!(
            python_int(&json!("3.5")),
            Err("invalid literal for int() with base 10: '3.5'".to_string())
        );
        assert_eq!(
            python_int(&json!(null)),
            Err(
                "int() argument must be a string, a bytes-like object or a real number, not \
                 'NoneType'"
                    .to_string()
            )
        );
    }
}
