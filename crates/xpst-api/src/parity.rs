//! What the parity test compares, as data.
//!
//! Three fixtures are checked in, all captured by
//! `xtask/scripts/capture_python_routes.py` from the real Python dashboard
//! (`cargo xtask routes` re-captures and diffs them):
//!
//! * [`route_parity.json`] — every `(method, path)` the Python app answers,
//! * [`payload_shapes.json`] — one sample payload per route this surface claims,
//! * [`pairing_refusals.json`] — every refusal the pairing exchange produces.
//!
//! Nothing here is hand-written except the divergence list, which exists
//! precisely so a deviation has to be declared to compile.

use std::collections::BTreeSet;

use serde_json::{Map, Value};

/// Deviations from the Python surface, each with the reason it exists.
///
/// The doc comment in `lib.rs` carries the same table for a reader; this one is
/// what the test can assert is non-empty and well-formed.
pub const DIVERGENCES: &[(&str, &str, &str)] = &[
    (
        "live auth probes",
        "collect_live_auth_status per platform",
        "none; readiness reports pending and no state field is guessed",
    ),
    (
        "provider state fields",
        "live/credential probe results",
        "null / unconfigured until something is probed",
    ),
    (
        "provider display names",
        "product copy (YouTube Shorts)",
        "xpst-core's content-contract name (YouTube)",
    ),
    (
        "rate-limit ledger",
        "rate_cooldowns.json",
        "rate_limits.json (xpst-limits)",
    ),
    (
        "schedule rows",
        "adds scheduled_time_local",
        "the persisted entry verbatim (store is UTC)",
    ),
    (
        "plan records",
        "plan_service preview + digest",
        "pipeline-native record with the same top-level keys",
    ),
    (
        "string index fallback",
        "the full Python landing page",
        "a compact equivalent page",
    ),
    (
        "video mode input",
        "derived from config or the CLI flag",
        "plan/preflight accept `mode`: no media probe runs on a request path, so the caller states it",
    ),
    (
        "/metrics",
        "Prometheus text",
        "typed 501 (no metrics registry)",
    ),
];

/// How strictly a route's payload shape is compared.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Shape {
    /// Every key path, at every depth, must match.
    Deep,
    /// Top-level keys must match; nested objects come from the Rust engine's own
    /// sources and the reason is stated.
    TopLevel(&'static str),
}

/// Per-route shape comparison depth, with the reason for anything less than
/// deep.
pub const SHAPE_LEVELS: &[(&str, Shape)] = &[
    ("health", Shape::Deep),
    ("api.schedules", Shape::Deep),
    ("api.devices", Shape::Deep),
    ("api.pair.missing_code", Shape::Deep),
    ("api.plans.create_error", Shape::Deep),
    ("api.plans.get_error", Shape::Deep),
    (
        "api.preflight",
        Shape::TopLevel(
            "the route rows and the nested plan block are built by xpst-core's planner, \
             which carries fewer per-destination fields than Python's route_plan",
        ),
    ),
    (
        "api.providers",
        Shape::TopLevel(
            "per-provider rows come from the Rust catalog; probe-derived fields are null \
             because nothing was probed",
        ),
    ),
    (
        "api.capabilities",
        Shape::TopLevel(
            "envelope rows are built from xpst-core's content contract and video envelope \
             registry",
        ),
    ),
    (
        "api.settings",
        Shape::TopLevel("sections are the masked YAML document, not Python dataclass field dumps"),
    ),
    (
        "api.health_status",
        Shape::TopLevel(
            "the readiness and canonical blocks report pending/empty because this engine has \
             no live prober",
        ),
    ),
];

const ROUTES_JSON: &str = include_str!("parity/route_parity.json");
const PAYLOADS_JSON: &str = include_str!("parity/payload_shapes.json");
const REFUSALS_JSON: &str = include_str!("parity/pairing_refusals.json");

/// One `(path, method)` the Python app answers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonRoute {
    pub path: String,
    pub method: String,
    pub name: String,
    pub kind: String,
}

/// Every route the Python dashboard app answers, from the checked-in capture.
///
/// # Panics
/// Panics when the checked-in fixture is malformed: a broken guard is worse
/// than a loud failure.
#[must_use]
pub fn python_routes() -> Vec<PythonRoute> {
    let document: Value =
        serde_json::from_str(ROUTES_JSON).expect("route_parity.json is valid JSON");
    let rows = document
        .get("routes")
        .and_then(Value::as_array)
        .expect("route_parity.json has a routes array");
    let mut routes = Vec::new();
    for row in rows {
        let path = row.get("path").and_then(Value::as_str).unwrap_or_default();
        let name = row.get("name").and_then(Value::as_str).unwrap_or_default();
        let kind = row.get("kind").and_then(Value::as_str).unwrap_or_default();
        for method in row
            .get("methods")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            routes.push(PythonRoute {
                path: path.to_string(),
                method: method.to_string(),
                name: name.to_string(),
                kind: kind.to_string(),
            });
        }
    }
    routes
}

/// The sample payloads captured from Python, keyed by sample id.
#[must_use]
pub fn payload_samples() -> Map<String, Value> {
    let document: Value =
        serde_json::from_str(PAYLOADS_JSON).expect("payload_shapes.json is valid JSON");
    document
        .get("samples")
        .and_then(Value::as_object)
        .cloned()
        .expect("payload_shapes.json has samples")
}

/// The pairing refusals captured from Python, keyed by refusal name.
#[must_use]
pub fn pairing_refusals() -> Map<String, Value> {
    let document: Value =
        serde_json::from_str(REFUSALS_JSON).expect("pairing_refusals.json is valid JSON");
    document
        .get("refusals")
        .and_then(Value::as_object)
        .cloned()
        .expect("pairing_refusals.json has refusals")
}

/// Every key path in a JSON document, at every depth.
///
/// Array elements collapse to one `[]` segment, so an array's elements are
/// compared once rather than once per index.
#[must_use]
pub fn key_paths(value: &Value) -> BTreeSet<String> {
    let mut paths = BTreeSet::new();
    collect_paths(value, "", &mut paths);
    paths
}

fn collect_paths(value: &Value, prefix: &str, out: &mut BTreeSet<String>) {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                let path = if prefix.is_empty() {
                    key.clone()
                } else {
                    format!("{prefix}.{key}")
                };
                out.insert(path.clone());
                collect_paths(child, &path, out);
            }
        }
        Value::Array(items) => {
            let path = format!("{prefix}[]");
            for item in items {
                collect_paths(item, &path, out);
            }
        }
        _ => {}
    }
}

/// Top-level keys of a document, ignoring depth.
#[must_use]
pub fn top_level_keys(value: &Value) -> BTreeSet<String> {
    value
        .as_object()
        .map(|map| map.keys().cloned().collect())
        .unwrap_or_default()
}

/// Describe the first difference between two key-path sets, or `None`.
#[must_use]
pub fn shape_difference(expected: &BTreeSet<String>, actual: &BTreeSet<String>) -> Option<String> {
    let missing: Vec<&String> = expected.difference(actual).collect();
    if !missing.is_empty() {
        return Some(format!("missing from the Rust payload: {missing:?}"));
    }
    let extra: Vec<&String> = actual.difference(expected).collect();
    if !extra.is_empty() {
        return Some(format!("not in the Python payload: {extra:?}"));
    }
    None
}

/// The shape level for a sample id, defaulting to deep.
#[must_use]
pub fn shape_level(sample_id: &str) -> Shape {
    SHAPE_LEVELS
        .iter()
        .find(|(id, _)| *id == sample_id)
        .map_or(Shape::Deep, |(_, level)| *level)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_captured_route_list_is_the_python_surface() {
        let routes = python_routes();
        assert_eq!(routes.len(), 57, "47 /api routes plus 10 floor routes");
        assert!(routes.iter().any(|route| route.path == "/api/pair"));
        assert!(routes.iter().any(|route| route.path == "/health"));
    }

    #[test]
    fn every_divergence_states_both_sides_and_a_reason() {
        for (surface, python, rust) in DIVERGENCES {
            assert!(!surface.is_empty());
            assert!(!python.is_empty());
            assert!(!rust.is_empty());
        }
    }

    #[test]
    fn every_shape_level_names_a_known_sample() {
        let samples = payload_samples();
        for (sample, _) in SHAPE_LEVELS {
            assert!(samples.contains_key(*sample), "{sample} has no capture");
        }
    }

    #[test]
    fn key_paths_ignore_array_indices() {
        let value = serde_json::json!({"a": [{"b": 1}, {"b": 2}], "c": null});
        let paths = key_paths(&value);
        assert!(paths.contains("a"));
        assert!(paths.contains("a[].b"));
        assert!(paths.contains("c"));
        assert_eq!(paths.len(), 3);
    }

    #[test]
    fn shape_comparison_reports_missing_and_extra_keys() {
        let expected: BTreeSet<String> = ["a".to_string(), "b".to_string()].into_iter().collect();
        let actual: BTreeSet<String> = ["a".to_string()].into_iter().collect();
        assert!(shape_difference(&expected, &actual)
            .unwrap()
            .contains("missing"));
        assert!(shape_difference(&actual, &expected)
            .unwrap()
            .contains("not in the Python payload"));
        assert!(shape_difference(&expected, &expected).is_none());
    }
}
