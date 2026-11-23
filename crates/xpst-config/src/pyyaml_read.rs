//! A PyYAML-compatible reader.
//!
//! The Python engine loads `config.yaml` with `yaml.safe_load`, so the Rust
//! engine has to read the same document into the same values — not merely into
//! *a* valid interpretation. That matters most on the write path (`config
//! export`, `config set`, `config import`, `config fix`), which reads the file
//! and writes it back: a reader that types `yes` as the string `"yes"` turns a
//! boolean into a string on every save, and one that types `.inf` as null
//! silently deletes the value.
//!
//! `serde-saphyr`'s deserializer is the wrong tool for that job. It resolves
//! scalars with its own YAML 1.1/1.2 switches, and the switches do not line up
//! with PyYAML: its YAML 1.1 boolean set includes a bare `y`/`n` (PyYAML's does
//! not), it reads `0o17` as 15 (PyYAML: a string), `08` as 8.0 (PyYAML: a
//! string), `1e3` as 1000.0 (PyYAML: a string), and it has no timestamp or
//! sexagesimal support at all. It also cannot tell a quoted `'yes'` from a
//! plain `yes` once the value reaches a visitor, which is exactly the
//! distinction the resolver needs.
//!
//! So this module walks the parser's event stream directly — the same
//! `granit-parser` that `serde-saphyr` uses, re-exported by it, so no new
//! dependency — and applies [`crate::schema`] to every plain scalar. Quoted and
//! block scalars stay strings, as they do in PyYAML.
//!
//! Deliberate divergences from PyYAML, all of them refusals rather than silent
//! reinterpretations:
//!
//! * a duplicate key in one mapping is an error (PyYAML keeps the last one);
//! * `!!binary`, `!!set` and unknown tags are errors (PyYAML constructs
//!   `bytes`/`set`, which the config surface has no representation for);
//! * an integer outside `i64` is an error (PyYAML is arbitrary precision);
//! * `1:` and `true:` in one mapping stay two keys (Python's `True == 1`
//!   collapses them into one, silently dropping a value).

use std::collections::HashMap;

use serde_saphyr::granit_parser::{Event, Parser, ScalarStyle, Tag};

use crate::schema::{self, Scalar};
use crate::yaml::{Error, Mapping, Number, Value};

/// Parse one YAML document into a [`Value`].
pub fn from_str(text: &str) -> Result<Value, Error> {
    // PyYAML reads a block scalar that ends without a line break as having no
    // trailing newline; the parser's default follows the YAML test suite
    // instead, so ask for PyYAML's behaviour explicitly.
    let options = serde_saphyr::granit_parser::options! {
        block_scalar_eof_without_line_feed: true,
    };
    let mut parser = Parser::new_from_str_with_options(text, options);

    let mut anchors: HashMap<usize, Value> = HashMap::new();
    let mut stack: Vec<Frame> = Vec::new();
    let mut root: Option<Value> = None;
    let mut documents = 0usize;

    while let Some(event) = parser.next_event() {
        let (event, span) = event.map_err(|err| Error(err.to_string()))?;
        let position = || {
            format!(
                " at line {}, column {}",
                span.start.line(),
                span.start.col() + 1
            )
        };
        match event {
            Event::StreamStart | Event::StreamEnd | Event::DocumentEnd | Event::Anchor(..) => {}
            Event::DocumentStart(..) => {
                documents += 1;
                if documents > 1 {
                    return Err(Error(format!(
                        "expected a single document, found a second one{}",
                        position()
                    )));
                }
            }
            Event::Scalar(text, style, anchor, tag) => {
                let (value, merge) = scalar_value(&text, style, tag.as_deref(), &position)?;
                if anchor != 0 {
                    anchors.insert(anchor, value.clone());
                }
                push(&mut stack, &mut root, value, merge, &position)?;
            }
            Event::Alias(id) => {
                let value = anchors.get(&id).cloned().ok_or_else(|| {
                    Error(format!("unknown anchor reference{}{}", id, position()))
                })?;
                push(&mut stack, &mut root, value, false, &position)?;
            }
            Event::SequenceStart(_, anchor, tag) => {
                check_collection_tag(tag.as_deref(), "seq", &position)?;
                stack.push(Frame::Sequence {
                    items: Vec::new(),
                    anchor,
                });
            }
            Event::SequenceEnd => {
                let Some(Frame::Sequence { items, anchor }) = stack.pop() else {
                    return Err(Error(format!("unbalanced sequence end{}", position())));
                };
                let value = Value::Sequence(items);
                if anchor != 0 {
                    anchors.insert(anchor, value.clone());
                }
                push(&mut stack, &mut root, value, false, &position)?;
            }
            Event::MappingStart(_, anchor, tag) => {
                check_collection_tag(tag.as_deref(), "map", &position)?;
                stack.push(Frame::Mapping {
                    merged: Vec::new(),
                    explicit: Vec::new(),
                    pending: None,
                    anchor,
                });
            }
            Event::MappingEnd => {
                let Some(Frame::Mapping {
                    merged,
                    explicit,
                    pending,
                    anchor,
                }) = stack.pop()
                else {
                    return Err(Error(format!("unbalanced mapping end{}", position())));
                };
                if pending.is_some() {
                    return Err(Error(format!("mapping key without a value{}", position())));
                }
                let value = Value::Mapping(finish_mapping(merged, explicit, &position)?);
                if anchor != 0 {
                    anchors.insert(anchor, value.clone());
                }
                push(&mut stack, &mut root, value, false, &position)?;
            }
            // Only emitted with `emit_unresolved_aliases`; the default stops at
            // the unknown anchor, so this is unreachable but must be handled.
            Event::UnresolvedAlias(name) => {
                return Err(Error(format!("unknown anchor `*{name}`{}", position())));
            }
            // `Event` is `#[non_exhaustive]`. A variant we do not know is a
            // parser feature we have not taught the reader about, so refuse
            // rather than silently drop a node.
            _ => {
                return Err(Error(format!("unsupported YAML event{}", position())));
            }
        }
    }

    if !stack.is_empty() {
        return Err(Error("unexpected end of document".to_string()));
    }
    Ok(root.unwrap_or(Value::Null))
}

/// A collection being built.
enum Frame {
    Sequence {
        items: Vec<Value>,
        anchor: usize,
    },
    Mapping {
        /// Pairs contributed by `<<` merge keys, in PyYAML's order.
        merged: Vec<(Value, Value)>,
        /// Pairs written in the document.
        explicit: Vec<(Value, Value)>,
        /// A key waiting for its value, and whether it was a merge key.
        pending: Option<(Value, bool)>,
        anchor: usize,
    },
}

fn push(
    stack: &mut [Frame],
    root: &mut Option<Value>,
    value: Value,
    merge: bool,
    position: &dyn Fn() -> String,
) -> Result<(), Error> {
    match stack.last_mut() {
        None => {
            if root.is_some() {
                return Err(Error(format!("more than one root node{}", position())));
            }
            *root = Some(value);
        }
        Some(Frame::Sequence { items, .. }) => items.push(value),
        Some(Frame::Mapping {
            merged,
            explicit,
            pending,
            ..
        }) => match pending.take() {
            None => *pending = Some((value, merge)),
            Some((key, key_is_merge)) => {
                if key_is_merge {
                    merge_into(merged, &value, position)?;
                } else {
                    explicit.push((key, value));
                }
            }
        },
    }
    Ok(())
}

/// Expand a `<<` value into the pairs it contributes.
///
/// PyYAML flattens merge keys *before* the document's own pairs, so an explicit
/// key overrides a merged one, and for a sequence of mappings the earlier
/// mapping wins.
fn merge_into(
    merged: &mut Vec<(Value, Value)>,
    value: &Value,
    position: &dyn Fn() -> String,
) -> Result<(), Error> {
    match value {
        Value::Mapping(map) => {
            merged.extend(map.iter().map(|(key, item)| (key.clone(), item.clone())));
            Ok(())
        }
        Value::Sequence(items) => {
            for item in items.iter().rev() {
                let Value::Mapping(map) = item else {
                    return Err(Error(format!(
                        "a merge sequence must contain mappings{}",
                        position()
                    )));
                };
                merged.extend(map.iter().map(|(key, item)| (key.clone(), item.clone())));
            }
            Ok(())
        }
        _ => Err(Error(format!(
            "a merge key must be a mapping or a sequence of mappings{}",
            position()
        ))),
    }
}

/// Build the final mapping: merged pairs first, then the document's own, with
/// the last write winning — which is what a Python `dict` does.
fn finish_mapping(
    merged: Vec<(Value, Value)>,
    explicit: Vec<(Value, Value)>,
    position: &dyn Fn() -> String,
) -> Result<Mapping, Error> {
    let mut seen: Vec<&Value> = Vec::with_capacity(explicit.len());
    for (key, _) in &explicit {
        if seen.contains(&key) {
            return Err(Error(format!(
                "duplicate mapping key: {}{}",
                describe_key(key),
                position()
            )));
        }
        seen.push(key);
    }
    let mut mapping = Mapping::new();
    for (key, value) in merged.into_iter().chain(explicit) {
        mapping.insert(key, value);
    }
    Ok(mapping)
}

fn describe_key(key: &Value) -> String {
    match key {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        Value::Bool(flag) => flag.to_string(),
        Value::Null => "null".to_string(),
        Value::Timestamp(text) => text.clone(),
        Value::NonFinite(_) => "a non-finite float".to_string(),
        Value::Sequence(_) => "a sequence".to_string(),
        Value::Mapping(_) => "a mapping".to_string(),
    }
}

/// Type one scalar. The second element is true when it is a merge key.
fn scalar_value(
    text: &str,
    style: ScalarStyle,
    tag: Option<&Tag>,
    position: &dyn Fn() -> String,
) -> Result<(Value, bool), Error> {
    if let Some(tag) = tag {
        return tagged(text, tag, position);
    }
    // Only a plain scalar is subject to implicit typing; `'yes'` is a string.
    if !matches!(style, ScalarStyle::Plain) {
        return Ok((Value::String(text.to_string()), false));
    }
    let value = match schema::resolve_plain(text) {
        None => Value::String(text.to_string()),
        Some(Scalar::Null) => Value::Null,
        Some(Scalar::Bool(flag)) => Value::Bool(flag),
        Some(Scalar::Int(number)) => Value::Number(Number::from(number)),
        Some(Scalar::Float(number)) => Value::Number(
            Number::from_f64(number)
                .ok_or_else(|| Error(format!("float out of range: {text}{}", position())))?,
        ),
        Some(Scalar::NonFinite(kind)) => Value::NonFinite(kind),
        Some(Scalar::Timestamp(stamp)) => Value::Timestamp(stamp),
        Some(Scalar::InvalidTimestamp) => {
            return Err(Error(format!("invalid timestamp: {text}{}", position())));
        }
        Some(Scalar::Merge) => return Ok((Value::String("<<".to_string()), true)),
        Some(Scalar::Value) => Value::String("=".to_string()),
        Some(Scalar::Yaml) => Value::String(text.to_string()),
    };
    Ok((value, false))
}

/// An explicitly tagged scalar. PyYAML's `SafeConstructor` knows the core
/// schema's tags; anything else is a refusal rather than a guess.
fn tagged(text: &str, tag: &Tag, position: &dyn Fn() -> String) -> Result<(Value, bool), Error> {
    if tag.handle() != "tag:yaml.org,2002:" {
        return Err(Error(format!("unsupported tag `{tag}`{}", position())));
    }
    let value = match tag.suffix() {
        "str" => Value::String(text.to_string()),
        "null" => Value::Null,
        "bool" => match schema::resolve_plain(text) {
            Some(Scalar::Bool(flag)) => Value::Bool(flag),
            _ => return Err(Error(format!("invalid !!bool: {text}{}", position()))),
        },
        "int" => match schema::resolve_plain(text) {
            Some(Scalar::Int(number)) => Value::Number(Number::from(number)),
            _ => return Err(Error(format!("invalid !!int: {text}{}", position()))),
        },
        "float" => match schema::resolve_plain(text) {
            Some(Scalar::Float(number)) => Value::Number(
                Number::from_f64(number)
                    .ok_or_else(|| Error(format!("float out of range: {text}{}", position())))?,
            ),
            Some(Scalar::NonFinite(kind)) => Value::NonFinite(kind),
            _ => return Err(Error(format!("invalid !!float: {text}{}", position()))),
        },
        "timestamp" => match schema::resolve_plain(text) {
            Some(Scalar::Timestamp(stamp)) => Value::Timestamp(stamp),
            _ => return Err(Error(format!("invalid !!timestamp: {text}{}", position()))),
        },
        "merge" => return Ok((Value::String("<<".to_string()), true)),
        "value" => Value::String("=".to_string()),
        other => {
            return Err(Error(format!("unsupported tag `!!{other}`{}", position())));
        }
    };
    Ok((value, false))
}

fn check_collection_tag(
    tag: Option<&Tag>,
    expected: &str,
    position: &dyn Fn() -> String,
) -> Result<(), Error> {
    let Some(tag) = tag else { return Ok(()) };
    if tag.handle() == "tag:yaml.org,2002:" && tag.suffix() == expected {
        return Ok(());
    }
    Err(Error(format!("unsupported tag `{tag}`{}", position())))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(text: &str) -> Value {
        from_str(text).unwrap_or_else(|err| panic!("{text:?} should parse: {err}"))
    }

    fn json(text: &str) -> String {
        crate::python_style_json(&parse(text)).expect("json")
    }

    #[test]
    fn a_duplicate_key_is_refused() {
        let error = from_str("a: 1\na: 2\n").expect_err("duplicate key");
        assert!(
            error.to_string().contains("duplicate mapping key: a"),
            "{error}"
        );
    }

    #[test]
    fn a_second_document_is_refused() {
        let error = from_str("a: 1\n---\nb: 2\n").expect_err("two documents");
        assert!(error.to_string().contains("single document"), "{error}");
    }

    #[test]
    fn an_unknown_tag_is_refused() {
        let error = from_str("a: !custom 1\n").expect_err("unknown tag");
        assert!(error.to_string().contains("unsupported tag"), "{error}");
    }

    #[test]
    fn an_empty_document_is_null() {
        assert_eq!(parse(""), Value::Null);
        assert_eq!(parse("# only a comment\n"), Value::Null);
    }

    #[test]
    fn merge_keys_put_the_merged_pairs_first_and_let_explicit_ones_win() {
        // PyYAML flattens `<<` before the document's own pairs, so the output
        // order is `enabled, retries, name` and `retries` keeps the explicit 5.
        assert_eq!(
            json("defaults: &d\n  enabled: true\n  retries: 3\nservice:\n  <<: *d\n  retries: 5\n  name: svc\n"),
            r#"{"defaults": {"enabled": true, "retries": 3}, "service": {"enabled": true, "retries": 5, "name": "svc"}}"#
        );
    }

    #[test]
    fn a_sequence_of_merge_sources_lets_the_first_win() {
        assert_eq!(
            json("a: &a\n  k: 1\nb: &b\n  k: 2\nc:\n  <<: [*a, *b]\n"),
            r#"{"a": {"k": 1}, "b": {"k": 2}, "c": {"k": 1}}"#
        );
    }

    #[test]
    fn an_alias_repeats_the_anchored_value() {
        assert_eq!(
            json("base: &b\n  x: 1\ncopy: *b\n"),
            r#"{"base": {"x": 1}, "copy": {"x": 1}}"#
        );
    }

    #[test]
    fn quoted_scalars_are_never_typed() {
        assert_eq!(
            json("a: 'yes'\nb: '1_000'\nc: '2026-01-01'\nd: '.inf'\n"),
            r#"{"a": "yes", "b": "1_000", "c": "2026-01-01", "d": ".inf"}"#
        );
    }

    #[test]
    fn a_block_scalar_ending_without_a_newline_matches_pyyaml() {
        // PyYAML reads `|` with no trailing line break as having no newline.
        assert_eq!(json("a: |\n  text"), r#"{"a": "text"}"#);
    }

    #[test]
    fn a_byte_order_mark_is_skipped() {
        assert_eq!(json("\u{feff}version: 4\n"), r#"{"version": 4}"#);
    }

    #[test]
    fn an_out_of_range_timestamp_is_refused_like_pyyaml() {
        let error = from_str("when: 2026-13-01\n").expect_err("invalid date");
        assert!(error.to_string().contains("invalid timestamp"), "{error}");
    }
}
