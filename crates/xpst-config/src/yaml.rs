//! The YAML data model `xpst-config` reads config documents into.
//!
//! This replaces `serde_yaml::{Value, Mapping}`. `serde_yaml` is deprecated and
//! reaches the unmaintained `unsafe-libyaml` (a c2rust translation of libyaml),
//! which is a RustSec advisory no version can clear — only a different parser
//! can. The parser here is [`serde_saphyr`], safe Rust with no C dependency.
//!
//! The *model* stays ours, on purpose. Three behaviours that the config surface
//! depends on are not things a generic value type gives you for free, and a
//! migration that silently changed any of them would be a behaviour change
//! disguised as a dependency swap:
//!
//! * **Order.** A mapping keeps the document's key order, because
//!   `xpst config show` renders the document back out and the Python engine
//!   dumps it with `sort_keys=False`.
//! * **Duplicate keys are an error.** The previous parser refused them and so
//!   does this one (the deserializer is configured with `DuplicateKeyPolicy::
//!   Error`). A config with two `enabled:` keys must not silently pick one.
//! * **Non-string keys stay typed.** `443: https` keeps a numeric key; the JSON
//!   renderer then applies serde_json's own rule for non-string keys exactly as
//!   it did before, so the JSON output is unchanged.
//!
//! The scalar *inference* rules are pinned to the previous parser's YAML 1.2
//! behaviour rather than to the new parser's defaults, which resolve `on`,
//! `yes`, `off` and `no` as booleans and read `007` and `1_000` as numbers.
//! Those spellings are strings in a config file that a YAML 1.1 reader (PyYAML)
//! also writes, so the strict options below are the compatibility switch, not a
//! preference. `crates/xpst-config/tests/yaml_compat.rs` holds the corpus that
//! proves each of these rows.

use std::fmt;

pub use serde_json::Number;

/// A YAML mapping that preserves document order.
///
/// Deliberately a `Vec` rather than a hash map: config documents are small
/// (tens of keys), order is part of the output contract, and lookup by key is
/// one string comparison per entry.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Mapping(Vec<(Value, Value)>);

impl Mapping {
    /// An empty mapping.
    #[must_use]
    pub fn new() -> Self {
        Self(Vec::new())
    }

    /// Number of entries.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// True when there are no entries.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The value stored under a string key.
    ///
    /// Only string keys are looked up by name, which is the whole config use
    /// case: `version`, `accounts`, `enabled`. A numeric key is reachable
    /// through [`Mapping::iter`].
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.0
            .iter()
            .find(|(candidate, _)| candidate.as_str() == Some(key))
            .map(|(_, value)| value)
    }

    /// True when the mapping has an entry under `key`.
    #[must_use]
    pub fn contains_key(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    /// Insert an entry, replacing any existing entry with an equal key.
    ///
    /// Replacement preserves the original position, so a re-inserted key does
    /// not move to the end of the document.
    pub fn insert(&mut self, key: Value, value: Value) -> Option<Value> {
        if let Some((_, existing)) = self.0.iter_mut().find(|(candidate, _)| *candidate == key) {
            return Some(std::mem::replace(existing, value));
        }
        self.0.push((key, value));
        None
    }

    /// Entries in document order.
    pub fn iter(&self) -> impl Iterator<Item = (&Value, &Value)> {
        self.0.iter().map(|(key, value)| (key, value))
    }

    /// Keys in document order.
    pub fn keys(&self) -> impl Iterator<Item = &Value> {
        self.0.iter().map(|(key, _)| key)
    }

    /// Values in document order.
    pub fn values(&self) -> impl Iterator<Item = &Value> {
        self.0.iter().map(|(_, value)| value)
    }
}

impl<'a> IntoIterator for &'a Mapping {
    type Item = (&'a Value, &'a Value);
    type IntoIter = std::iter::Map<
        std::slice::Iter<'a, (Value, Value)>,
        fn(&'a (Value, Value)) -> (&'a Value, &'a Value),
    >;

    fn into_iter(self) -> Self::IntoIter {
        self.0.iter().map(|(key, value)| (key, value))
    }
}

impl IntoIterator for Mapping {
    type Item = (Value, Value);
    type IntoIter = std::vec::IntoIter<(Value, Value)>;

    fn into_iter(self) -> Self::IntoIter {
        self.0.into_iter()
    }
}

/// A YAML value.
///
/// Same variant set the previous parser exposed, minus `Tagged`: no xPST config
/// written by the engine carries a YAML tag, and the new parser resolves tags
/// to their underlying scalar rather than surfacing them.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    /// `null`, `~`, or an empty node.
    Null,
    /// `true` / `false`.
    Bool(bool),
    /// An integer or float.
    Number(Number),
    /// A string scalar.
    String(String),
    /// A sequence.
    Sequence(Vec<Value>),
    /// A mapping.
    Mapping(Mapping),
}

impl Value {
    /// The value under a string key, when this is a mapping.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Mapping(map) => map.get(key),
            _ => None,
        }
    }

    /// The mapping behind this value.
    #[must_use]
    pub fn as_mapping(&self) -> Option<&Mapping> {
        match self {
            Value::Mapping(map) => Some(map),
            _ => None,
        }
    }

    /// The sequence behind this value.
    #[must_use]
    pub fn as_sequence(&self) -> Option<&[Value]> {
        match self {
            Value::Sequence(items) => Some(items),
            _ => None,
        }
    }

    /// The string behind this value.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(text) => Some(text),
            _ => None,
        }
    }

    /// The boolean behind this value.
    #[must_use]
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(flag) => Some(*flag),
            _ => None,
        }
    }

    /// The integer behind this value, when it is not negative.
    #[must_use]
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Value::Number(number) => number.as_u64(),
            _ => None,
        }
    }

    /// The integer behind this value.
    #[must_use]
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Number(number) => number.as_i64(),
            _ => None,
        }
    }

    /// The float behind this value.
    #[must_use]
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Number(number) => number.as_f64(),
            _ => None,
        }
    }

    /// True for [`Value::Null`].
    #[must_use]
    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }
}

impl From<&str> for Value {
    fn from(text: &str) -> Self {
        Value::String(text.to_string())
    }
}

impl From<String> for Value {
    fn from(text: String) -> Self {
        Value::String(text)
    }
}

impl From<bool> for Value {
    fn from(flag: bool) -> Self {
        Value::Bool(flag)
    }
}

impl From<i64> for Value {
    fn from(number: i64) -> Self {
        Value::Number(Number::from(number))
    }
}

impl From<u64> for Value {
    fn from(number: u64) -> Self {
        Value::Number(Number::from(number))
    }
}

/// A parse or render failure, as a one-line message fit for the
/// `Invalid YAML in {path}: {message}` surface.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error(String);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

/// The scalar-inference and mapping rules, pinned to the previous parser.
///
/// Every field below is a compatibility switch, not a preference — see the
/// module documentation and `tests/yaml_compat.rs`, which fails if one of them
/// stops being applied.
fn parse_options() -> serde_saphyr::Options {
    serde_saphyr::options! {
        // Two `enabled:` keys in one mapping is a broken document, not a
        // last-write-wins.
        duplicate_keys: serde_saphyr::DuplicateKeyPolicy::Error,
        // `<<: *defaults` stays a literal `<<` entry, as before.
        merge_keys: serde_saphyr::MergeKeyPolicy::AsOrdinary,
        // `on`/`yes`/`off`/`no` are strings unless quoted.
        strict_booleans: true,
        // `key:` stays an empty string when read as text, `null` otherwise.
        null_as_string: true,
        // `007` and `1_000` are strings; `0x1f` and `0o17` stay numbers.
        leading_zero_numbers_as_strings: true,
        underscored_numbers_as_strings: true,
        // `.inf`/`.nan` reach the visitor instead of failing the parse.
        non_finite_float_policy: serde_saphyr::NonFiniteFloatPolicy::PassThrough,
        // Errors are embedded in a one-line CLI message; a source snippet would
        // break that surface.
        with_snippet: false,
    }
}

/// Parse one YAML document.
///
/// A multi-document stream is an error, as it was before: a config file is one
/// document, and silently reading the first of several would hide a corrupted
/// file.
pub fn from_str(text: &str) -> Result<Value, Error> {
    serde_saphyr::from_str_with_options(text, parse_options())
        .map_err(|err| Error(err.to_string().trim().to_string()))
}

/// Render a value as block-style YAML.
///
/// The emitted style is not byte-identical to the deprecated parser's: this one
/// double-quotes scalars a YAML 1.1 reader could mistype and folds long lines.
/// Both are still valid YAML that re-parses to the same value, and the quoting
/// is the safer direction — `yes` stays a string for PyYAML as well.
pub fn to_string(value: &Value) -> Result<String, Error> {
    serde_saphyr::to_string(value).map_err(|err| Error(err.to_string().trim().to_string()))
}

// ---------------------------------------------------------------------------
// serde plumbing
// ---------------------------------------------------------------------------

impl serde::Serialize for Value {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::{SerializeMap, SerializeSeq};

        match self {
            Value::Null => serializer.serialize_unit(),
            Value::Bool(flag) => serializer.serialize_bool(*flag),
            Value::Number(number) => number.serialize(serializer),
            Value::String(text) => serializer.serialize_str(text),
            Value::Sequence(items) => {
                let mut seq = serializer.serialize_seq(Some(items.len()))?;
                for item in items {
                    seq.serialize_element(item)?;
                }
                seq.end()
            }
            Value::Mapping(map) => {
                let mut entries = serializer.serialize_map(Some(map.len()))?;
                for (key, value) in map {
                    entries.serialize_entry(key, value)?;
                }
                entries.end()
            }
        }
    }
}

impl<'de> serde::Deserialize<'de> for Value {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        use serde::de::{self, MapAccess, SeqAccess, Visitor};

        struct ValueVisitor;

        impl<'de> Visitor<'de> for ValueVisitor {
            type Value = Value;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("any YAML value")
            }

            fn visit_unit<E: de::Error>(self) -> Result<Value, E> {
                Ok(Value::Null)
            }

            fn visit_none<E: de::Error>(self) -> Result<Value, E> {
                Ok(Value::Null)
            }

            fn visit_some<D: serde::Deserializer<'de>>(self, d: D) -> Result<Value, D::Error> {
                serde::Deserialize::deserialize(d)
            }

            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Value, E> {
                Ok(Value::Bool(value))
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Value, E> {
                Ok(Value::Number(Number::from(value)))
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Value, E> {
                Ok(Value::Number(Number::from(value)))
            }

            fn visit_f64<E: de::Error>(self, value: f64) -> Result<Value, E> {
                // A non-finite float has no JSON representation; the previous
                // parser kept `Number(inf)` and failed only when that document
                // was rendered as JSON. `null` is the concrete-JSON shape both
                // JSON and the config surface can carry.
                Ok(match Number::from_f64(value) {
                    Some(number) => Value::Number(number),
                    None => Value::Null,
                })
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Value, E> {
                Ok(Value::String(value.to_string()))
            }

            fn visit_string<E: de::Error>(self, value: String) -> Result<Value, E> {
                Ok(Value::String(value))
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
                let mut items = Vec::with_capacity(seq.size_hint().unwrap_or(0));
                while let Some(item) = seq.next_element()? {
                    items.push(item);
                }
                Ok(Value::Sequence(items))
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
                let mut entries = Mapping::new();
                while let Some((key, value)) = map.next_entry::<Value, Value>()? {
                    // The parser already rejects duplicates under
                    // `DuplicateKeyPolicy::Error`; `insert` replaces in place so
                    // that a future policy change cannot silently rotate a key
                    // to the end of the document.
                    entries.insert(key, value);
                }
                Ok(Value::Mapping(entries))
            }
        }

        deserializer.deserialize_any(ValueVisitor)
    }
}
