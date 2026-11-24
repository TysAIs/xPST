//! The content contract's capability document — what xPST can publish, per
//! destination.
//!
//! Parity target: `src/xpst/content.py::capability_document()`, which the CLI
//! (`xpst capabilities`), the MCP tool (`xpst_capabilities`) and the HTTP API
//! (`GET /api/capabilities`) all serve verbatim, so the list a human reads and
//! the list an agent plans against cannot drift.
//!
//! **The document is captured, not recomputed.** It is derived from the content
//! contract's tables (`DESTINATION_CONTENT_PROFILES`, `CONTENT_TYPES`,
//! `media.specs.PLATFORM_SPECS`), which is a large data port with no behaviour
//! of its own; transcribing it into Rust would make a typo invisible. Instead
//! `xtask/scripts/capture_capabilities.py` runs the shipped Python and writes
//! `resources/capabilities.json` as the exact bytes `json_output()` prints, and
//! `xtask/scripts/capture_local_surface.py --check-with` diffs the Rust payload
//! against a live `python -m xpst capabilities --json` on every CI run — so a
//! Python-side change fails the build until the resource is refreshed.
//!
//! Two renderers read the same bytes: the CLI writes [`DOCUMENT`] verbatim
//! (compact, Python's `json.dumps` separators, trailing newline), and the MCP
//! tool parses it into [`document`] and re-renders it nested inside its own
//! `indent=2` payload.

use serde_json::Value;

/// The captured document, byte-identical to `json_output(capability_document())`.
pub const DOCUMENT: &str = include_str!("resources/capabilities.json");

/// The document as a value, for a surface that nests it in a larger payload.
///
/// `serde_json` is built with `preserve_order`, so the key order the capture
/// recorded survives the round trip — which is what makes the nested render
/// byte-identical to Python's.
pub fn document() -> Result<Value, String> {
    serde_json::from_str(DOCUMENT).map_err(|error| format!("embedded capability document: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_embedded_document_parses_and_keeps_its_key_order() {
        let document = document().expect("the embedded document is valid JSON");
        let keys: Vec<&str> = document
            .as_object()
            .expect("the document is an object")
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            vec![
                "contract_version",
                "content_types",
                "publish_destinations",
                "platforms",
                "format_routes",
                "envelopes",
                "publish_routes",
                "declared_but_unimplemented",
            ],
            "the captured document's key order is the parity surface"
        );
        assert_eq!(document["contract_version"], 2);
        assert!(
            document["platforms"]
                .as_object()
                .is_some_and(|platforms| platforms.len() >= 8),
            "the document must list every publish destination"
        );
    }

    /// A re-render at `indent=2` must be the same document, so the MCP tool can
    /// nest it without changing a value.
    #[test]
    fn the_document_round_trips_through_pretty_json() {
        let document = document().expect("valid JSON");
        let pretty = serde_json::to_string_pretty(&document).expect("render");
        let reparsed: Value = serde_json::from_str(&pretty).expect("reparse");
        assert_eq!(reparsed, document);
    }
}
