//! JSON helpers shared by the engine's file writers.
//!
//! `serde_json` is built with `preserve_order` (workspace `Cargo.toml`), so a
//! [`serde_json::Map`] keeps insertion order. Python's
//! `json.dumps(..., sort_keys=True)` is the opposite contract, and the
//! documents that use it (`devices.json`, `ban_risk_acknowledgements.json`, the
//! MCP tool text) are byte-compared with the shipped engine, so the sort has to
//! be explicit.
//!
//! This lives in `xpst-core` rather than in `xpst-config` (where the
//! Python-compatible *renderer* lives) because the writers that need it are
//! spread across the workspace: `xpst-config` dev-depends on `xpst-authflow`
//! for its credential-store test, so putting the helper in `xpst-config` and
//! depending on it from `xpst-authflow` would make the two crates mutually
//! dependent. `xpst-core` is the base crate both can depend on.

use serde_json::Value;

/// Sort every object's keys, recursively — `json.dumps(..., sort_keys=True)`.
///
/// Python sorts at every level, not just the top one, so a nested record's
/// fields are alphabetical too. Arrays keep their order (only objects are
/// sorted), which is what `sort_keys=True` does.
pub fn sort_keys_recursive(value: &mut Value) {
    match value {
        Value::Object(map) => {
            map.sort_keys();
            for child in map.values_mut() {
                sort_keys_recursive(child);
            }
        }
        Value::Array(items) => items.iter_mut().for_each(sort_keys_recursive),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::sort_keys_recursive;
    use serde_json::json;

    #[test]
    fn sorts_every_level_and_leaves_arrays_in_order() {
        let mut value = json!({
            "version": 1,
            "acknowledgements": {
                "x": {"platform": "x", "auth_mode": "cookies"},
                "instagram": {"surface": "s", "ban_risk": "b"},
            },
            "list": [{"b": 1, "a": 2}, {"d": 3, "c": 4}],
        });
        sort_keys_recursive(&mut value);
        let rendered = serde_json::to_string(&value).expect("json");
        assert_eq!(
            rendered,
            r#"{"acknowledgements":{"instagram":{"ban_risk":"b","surface":"s"},"x":{"auth_mode":"cookies","platform":"x"}},"list":[{"a":2,"b":1},{"c":4,"d":3}],"version":1}"#
        );
    }

    #[test]
    fn scalars_and_empty_containers_are_untouched() {
        for mut value in [
            json!(null),
            json!(true),
            json!(7),
            json!("text"),
            json!({}),
            json!([]),
        ] {
            let before = serde_json::to_string(&value).expect("json");
            sort_keys_recursive(&mut value);
            assert_eq!(serde_json::to_string(&value).expect("json"), before);
        }
    }
}
