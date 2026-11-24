//! The knowledge-base JSON store, read the way the Python engine reads it.
//!
//! `JsonKnowledgeStore._load_file` is corruption-tolerant in a specific way: a
//! file that fails to decode, or whose records fail to construct, is renamed to
//! `<name>.corrupt` and the store starts empty. Two failure modes are *not*
//! caught and escape as a traceback — a document that is not an object
//! (`AttributeError`) and a value that cannot be coerced (`ValueError`). Both
//! are reproduced here, because a store the Python engine refuses to read must
//! not be silently accepted by the Rust one.
//!
//! The record decoders are strict on purpose. `Nugget.from_dict` and
//! `Area.from_dict` splat the mapping into the dataclass constructor, so an
//! unknown key is a `TypeError` and quarantines the whole file; a missing
//! required key does the same. Accepting either would make the Rust engine read
//! a store Python had already thrown away.

use std::cmp::Ordering;
use std::path::Path;

use xpst_config::{Mapping, Value};

use crate::output::from_json;

/// A store file Python raises out of the loader for: the exception line.
pub type LoadResult<T> = Result<T, String>;

/// `path.with_suffix(path.suffix + ".corrupt")`, then `path.replace(...)`.
///
/// The reason is carried by the caller for diagnostics; Python logs it at
/// WARNING and the Rust engine stays silent on stderr for parity.
fn quarantine(path: &Path, _reason: &str) -> Option<Mapping> {
    let mut name = path
        .file_name()
        .map(|name| name.to_os_string())
        .unwrap_or_default();
    name.push(".corrupt");
    let _ = std::fs::rename(path, path.with_file_name(name));
    None
}

/// Python's type name for a decoded JSON value, for the `AttributeError` line.
fn python_type(value: &serde_json::Value) -> &'static str {
    match value {
        serde_json::Value::Null => "NoneType",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(number) => {
            if number.is_f64() {
                "float"
            } else {
                "int"
            }
        }
        serde_json::Value::String(_) => "str",
        serde_json::Value::Array(_) => "list",
        serde_json::Value::Object(_) => "dict",
    }
}

/// `json.loads(path.read_text())` for a document that must be an object.
///
/// `Ok(None)` means "missing, or quarantined" — both leave the store empty.
/// `Err` is the exception line Python would print before exiting 1.
fn read_object(path: &Path) -> LoadResult<Option<Mapping>> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("OSError: {error}")),
    };
    let parsed: serde_json::Value = match serde_json::from_str(&text) {
        Ok(value) => value,
        Err(error) => return Ok(quarantine(path, &error.to_string())),
    };
    match parsed {
        serde_json::Value::Object(_) => {
            let converted = from_json(&parsed);
            Ok(Some(converted.as_mapping().cloned().unwrap_or_default()))
        }
        other => Err(format!(
            "AttributeError: '{}' object has no attribute 'items'",
            python_type(&other)
        )),
    }
}

/// A record the Python dataclass would refuse to construct.
enum RecordError {
    /// `TypeError`/`KeyError`: `_load_file` catches it and quarantines the file.
    Quarantine(String),
    /// `AttributeError`/`ValueError`: it escapes the loader.
    Uncaught(String),
}

/// A hashable key for a Python dict key, or `None` where Python raises
/// `TypeError: unhashable type`.
pub fn id_key(value: &Value) -> Option<String> {
    match value {
        Value::String(text) | Value::Timestamp(text) => Some(format!("s:{text}")),
        Value::Number(number) => Some(format!("n:{number}")),
        Value::Bool(flag) => Some(format!("b:{flag}")),
        Value::Null => Some("z:".to_string()),
        Value::NonFinite(kind) => Some(format!("f:{}", kind.as_json())),
        Value::Sequence(_) | Value::Mapping(_) => None,
    }
}

/// `tuple(value)`'s length, or the `TypeError` Python raises for a non-iterable.
fn tuple_len(value: &Value) -> Result<usize, RecordError> {
    match value {
        Value::Sequence(items) => Ok(items.len()),
        Value::String(text) | Value::Timestamp(text) => Ok(text.chars().count()),
        Value::Mapping(map) => Ok(map.len()),
        other => Err(RecordError::Quarantine(format!(
            "TypeError: '{}' object is not iterable",
            value_type_name(other)
        ))),
    }
}

fn value_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "int",
        Value::NonFinite(_) => "float",
        Value::Timestamp(_) | Value::String(_) => "str",
        Value::Sequence(_) => "list",
        Value::Mapping(_) => "dict",
    }
}

/// `float(x)` for the values a JSON document can hold.
fn python_float(value: &Value) -> Result<f64, RecordError> {
    match value {
        Value::Number(number) => Ok(number.as_f64().unwrap_or_default()),
        Value::Bool(flag) => Ok(if *flag { 1.0 } else { 0.0 }),
        Value::NonFinite(kind) => Ok(match kind {
            xpst_config::NonFinite::Nan => f64::NAN,
            xpst_config::NonFinite::Infinity => f64::INFINITY,
            xpst_config::NonFinite::NegInfinity => f64::NEG_INFINITY,
        }),
        Value::String(text) | Value::Timestamp(text) => {
            let trimmed = text.trim();
            let cleaned = trimmed.replace('_', "");
            let lowered = cleaned.to_ascii_lowercase();
            match lowered.as_str() {
                "inf" | "infinity" | "+inf" | "+infinity" => Ok(f64::INFINITY),
                "-inf" | "-infinity" => Ok(f64::NEG_INFINITY),
                "nan" | "+nan" | "-nan" => Ok(f64::NAN),
                _ => cleaned.parse::<f64>().map_err(|_| {
                    RecordError::Uncaught(format!(
                        "ValueError: could not convert string to float: {}",
                        crate::output::python_repr(text)
                    ))
                }),
            }
        }
        other => Err(RecordError::Quarantine(format!(
            "TypeError: float() argument must be a string or a real number, not '{}'",
            value_type_name(other)
        ))),
    }
}

/// One `Nugget`, as the read paths need it.
#[derive(Debug)]
pub struct Nugget {
    pub id: Value,
    pub id_key: String,
    pub point: String,
    pub source_video_id: Value,
    pub source_url: Value,
    pub timestamp_start: Value,
    pub timestamp_end: Value,
    pub area_id: Value,
    pub difficulty: Value,
    pub embedding_len: usize,
}

const NUGGET_FIELDS: &[&str] = &[
    "id",
    "point",
    "source_video_id",
    "timestamp_start",
    "timestamp_end",
    "source_url",
    "source_platform",
    "source_post_id",
    "area_id",
    "difficulty",
    "prerequisites",
    "embedding",
    "created_at",
    "title",
    "topic",
    "tags",
];

const NUGGET_REQUIRED: &[&str] = &[
    "id",
    "point",
    "source_video_id",
    "timestamp_start",
    "timestamp_end",
];

fn unexpected_keyword(record: &Mapping, fields: &[&str], class: &str) -> Option<RecordError> {
    for key in record.keys() {
        let Some(name) = key.as_str() else {
            continue;
        };
        if !fields.contains(&name) {
            return Some(RecordError::Quarantine(format!(
                "TypeError: {class}.__init__() got an unexpected keyword argument '{name}'"
            )));
        }
    }
    None
}

fn missing_argument(record: &Mapping, required: &[&str], class: &str) -> Option<RecordError> {
    for name in required {
        if record.get(name).is_none() {
            return Some(RecordError::Quarantine(format!(
                "TypeError: {class}.__init__() missing required argument: '{name}'"
            )));
        }
    }
    None
}

fn nugget_from(value: &Value) -> Result<Nugget, RecordError> {
    let Some(record) = value.as_mapping() else {
        return Err(RecordError::Quarantine(format!(
            "TypeError: '{}' object is not iterable",
            value_type_name(value)
        )));
    };
    if let Some(error) = unexpected_keyword(record, NUGGET_FIELDS, "Nugget") {
        return Err(error);
    }
    if let Some(error) = missing_argument(record, NUGGET_REQUIRED, "Nugget") {
        return Err(error);
    }
    let id = record.get("id").cloned().unwrap_or(Value::Null);
    let Some(id_key) = id_key(&id) else {
        return Err(RecordError::Uncaught(format!(
            "TypeError: unhashable type: '{}'",
            value_type_name(&id)
        )));
    };
    let point = match record.get("point") {
        Some(Value::String(text)) | Some(Value::Timestamp(text)) => text.clone(),
        Some(other) => {
            return Err(RecordError::Uncaught(format!(
                "AttributeError: '{}' object has no attribute 'lower'",
                value_type_name(other)
            )))
        }
        None => String::new(),
    };
    // `tuple(d.get("embedding", ()))` — a non-iterable is a TypeError, which
    // `_load_file` catches and quarantines.
    let embedding_len = match record.get("embedding") {
        Some(value) => tuple_len(value)?,
        None => 0,
    };
    for optional in ["prerequisites", "tags"] {
        if let Some(value) = record.get(optional) {
            tuple_len(value)?;
        }
    }
    Ok(Nugget {
        id,
        id_key,
        point,
        source_video_id: record.get("source_video_id").cloned().unwrap_or(Value::Null),
        source_url: record.get("source_url").cloned().unwrap_or(Value::Null),
        timestamp_start: record
            .get("timestamp_start")
            .cloned()
            .unwrap_or(Value::Null),
        timestamp_end: record.get("timestamp_end").cloned().unwrap_or(Value::Null),
        area_id: record.get("area_id").cloned().unwrap_or(Value::Null),
        difficulty: record
            .get("difficulty")
            .cloned()
            .unwrap_or_else(|| Value::String("beginner".to_string())),
        embedding_len,
    })
}

/// One `Area`, as the read paths need it.
#[derive(Debug)]
pub struct Area {
    pub id: Value,
    pub id_key: String,
    pub label: Value,
    pub order_index: Value,
    pub nugget_ids: Vec<Value>,
}

const AREA_FIELDS: &[&str] = &["id", "label", "centroid", "nugget_ids", "order_index"];
const AREA_REQUIRED: &[&str] = &["id", "label"];

fn area_from(value: &Value) -> Result<Area, RecordError> {
    let Some(record) = value.as_mapping() else {
        return Err(RecordError::Quarantine(format!(
            "TypeError: '{}' object is not iterable",
            value_type_name(value)
        )));
    };
    if let Some(error) = unexpected_keyword(record, AREA_FIELDS, "Area") {
        return Err(error);
    }
    if let Some(error) = missing_argument(record, AREA_REQUIRED, "Area") {
        return Err(error);
    }
    let id = record.get("id").cloned().unwrap_or(Value::Null);
    let Some(id_key) = id_key(&id) else {
        return Err(RecordError::Uncaught(format!(
            "TypeError: unhashable type: '{}'",
            value_type_name(&id)
        )));
    };
    // `tuple(float(x) for x in d.get("centroid", ()))` — a non-numeric item is
    // a `ValueError`, which escapes `_load_file`.
    if let Some(centroid) = record.get("centroid") {
        match centroid {
            Value::Sequence(items) => {
                for item in items {
                    python_float(item)?;
                }
            }
            other => {
                tuple_len(other)?;
            }
        }
    }
    let nugget_ids = match record.get("nugget_ids") {
        Some(Value::Sequence(items)) => items.clone(),
        Some(Value::String(text)) | Some(Value::Timestamp(text)) => {
            text.chars().map(|ch| Value::String(ch.to_string())).collect()
        }
        Some(Value::Mapping(map)) => map.keys().cloned().collect(),
        Some(other) => {
            return Err(RecordError::Quarantine(format!(
                "TypeError: '{}' object is not iterable",
                value_type_name(other)
            )))
        }
        None => Vec::new(),
    };
    Ok(Area {
        id,
        id_key,
        label: record.get("label").cloned().unwrap_or(Value::Null),
        order_index: record.get("order_index").cloned().unwrap_or(Value::Null),
        nugget_ids,
    })
}

/// `JsonKnowledgeStore._load_file` for `nuggets.json`.
pub fn load_nuggets(path: &Path) -> LoadResult<Vec<Nugget>> {
    let Some(map) = read_object(path)? else {
        return Ok(Vec::new());
    };
    let mut nuggets = Vec::new();
    for (_key, value) in map.iter() {
        match nugget_from(value) {
            Ok(nugget) => nuggets.push(nugget),
            Err(RecordError::Quarantine(reason)) => {
                quarantine(path, &reason);
                return Ok(Vec::new());
            }
            Err(RecordError::Uncaught(line)) => return Err(line),
        }
    }
    Ok(nuggets)
}

/// `JsonKnowledgeStore._load_file` for `areas.json`.
pub fn load_areas(path: &Path) -> LoadResult<Vec<Area>> {
    let Some(map) = read_object(path)? else {
        return Ok(Vec::new());
    };
    let mut areas = Vec::new();
    for (_key, value) in map.iter() {
        match area_from(value) {
            Ok(area) => areas.push(area),
            Err(RecordError::Quarantine(reason)) => {
                quarantine(path, &reason);
                return Ok(Vec::new());
            }
            Err(RecordError::Uncaught(line)) => return Err(line),
        }
    }
    Ok(areas)
}

/// Python's ordering for two scalars: numbers numerically, strings
/// lexicographically, numbers before strings. Python raises `TypeError` on a
/// mixed comparison; a total order is used instead so a hand-corrupted store
/// still renders (recorded as a bounded deviation).
fn compare_scalar(left: &Value, right: &Value) -> Ordering {
    fn rank(value: &Value) -> u8 {
        match value {
            Value::Number(_) | Value::Bool(_) | Value::NonFinite(_) => 0,
            Value::String(_) | Value::Timestamp(_) => 1,
            Value::Null => 2,
            Value::Sequence(_) | Value::Mapping(_) => 3,
        }
    }
    fn as_number(value: &Value) -> f64 {
        match value {
            Value::Number(number) => number.as_f64().unwrap_or_default(),
            Value::Bool(flag) => {
                if *flag {
                    1.0
                } else {
                    0.0
                }
            }
            Value::NonFinite(kind) => match kind {
                xpst_config::NonFinite::Nan => f64::NAN,
                xpst_config::NonFinite::Infinity => f64::INFINITY,
                xpst_config::NonFinite::NegInfinity => f64::NEG_INFINITY,
            },
            _ => 0.0,
        }
    }
    fn as_text(value: &Value) -> String {
        match value {
            Value::String(text) | Value::Timestamp(text) => text.clone(),
            Value::Number(number) => number.to_string(),
            Value::Bool(flag) => flag.to_string(),
            Value::Null => "None".to_string(),
            _ => String::new(),
        }
    }
    match rank(left).cmp(&rank(right)) {
        Ordering::Equal => match rank(left) {
            0 => as_number(left).total_cmp(&as_number(right)),
            1 => as_text(left).cmp(&as_text(right)),
            _ => Ordering::Equal,
        },
        other => other,
    }
}

/// `order_areas`: stable sort by `(order_index, label, id)`.
pub fn order_areas(areas: &mut [Area]) {
    areas.sort_by(|left, right| {
        compare_scalar(&left.order_index, &right.order_index)
            .then_with(|| compare_scalar(&left.label, &right.label))
            .then_with(|| compare_scalar(&left.id, &right.id))
    });
}

/// `DIFFICULTY_RANK.get(nugget.difficulty, 0)`.
fn difficulty_rank(value: &Value) -> i64 {
    match value {
        Value::String(text) | Value::Timestamp(text) => match text.as_str() {
            "beginner" => 0,
            "intermediate" => 1,
            "advanced" => 2,
            _ => 0,
        },
        _ => 0,
    }
}

/// `order_nuggets`: stable sort by `(difficulty rank, timestamp_start, id)`.
pub fn order_nuggets(nuggets: &mut [&Nugget]) {
    nuggets.sort_by(|left, right| {
        difficulty_rank(&left.difficulty)
            .cmp(&difficulty_rank(&right.difficulty))
            .then_with(|| compare_scalar(&left.timestamp_start, &right.timestamp_start))
            .then_with(|| compare_scalar(&left.id, &right.id))
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("xpst-kb-store-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_valid_store_loads_in_file_order() {
        let dir = scratch("valid");
        let path = dir.join("nuggets.json");
        std::fs::write(
            &path,
            r#"{"b": {"id": "b", "point": "Second", "source_video_id": "v",
                     "timestamp_start": 2.0, "timestamp_end": 3.0},
                "a": {"id": "a", "point": "First", "source_video_id": "v",
                     "timestamp_start": 1, "timestamp_end": 2}}"#,
        )
        .unwrap();
        let nuggets = load_nuggets(&path).unwrap();
        assert_eq!(nuggets.len(), 2);
        assert_eq!(nuggets[0].point, "Second");
        assert_eq!(nuggets[1].point, "First");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unknown_key_quarantines_the_file() {
        let dir = scratch("unknown");
        let path = dir.join("areas.json");
        std::fs::write(
            &path,
            r#"{"a": {"id": "a", "label": "Alpha", "surprise": 1}}"#,
        )
        .unwrap();
        assert!(load_areas(&path).unwrap().is_empty());
        assert!(dir.join("areas.json.corrupt").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_required_key_quarantines_the_file() {
        let dir = scratch("missing");
        let path = dir.join("nuggets.json");
        std::fs::write(&path, r#"{"a": {"id": "a", "point": "p"}}"#).unwrap();
        assert!(load_nuggets(&path).unwrap().is_empty());
        assert!(dir.join("nuggets.json.corrupt").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_non_object_document_raises_like_python() {
        let dir = scratch("list");
        let path = dir.join("nuggets.json");
        std::fs::write(&path, "[1, 2]").unwrap();
        let error = load_nuggets(&path).unwrap_err();
        assert!(error.starts_with("AttributeError: 'list' object"), "{error}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_non_numeric_centroid_raises_like_python() {
        let dir = scratch("centroid");
        let path = dir.join("areas.json");
        std::fs::write(
            &path,
            r#"{"a": {"id": "a", "label": "Alpha", "centroid": ["abc"]}}"#,
        )
        .unwrap();
        let error = load_areas(&path).unwrap_err();
        assert!(error.starts_with("ValueError: could not convert"), "{error}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn areas_order_by_index_then_label_then_id() {
        let dir = scratch("order");
        let path = dir.join("areas.json");
        std::fs::write(
            &path,
            r#"{"b": {"id": "b", "label": "Beta", "order_index": 1, "nugget_ids": ["n1"]},
                "a": {"id": "a", "label": "Alpha", "order_index": 0, "nugget_ids": ["n1", "n2"]}}"#,
        )
        .unwrap();
        let mut areas = load_areas(&path).unwrap();
        order_areas(&mut areas);
        assert_eq!(areas[0].id_key, "s:a");
        assert_eq!(areas[0].nugget_ids.len(), 2);
        assert_eq!(areas[1].id_key, "s:b");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
