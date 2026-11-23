//! The YAML reader must mean what PyYAML means.
//!
//! `serde_yaml` (deprecated, and the only route to the unmaintained
//! `unsafe-libyaml`) was replaced by `serde_saphyr`, and the reader now walks
//! that parser's event stream directly so it can apply PyYAML's resolver to
//! every plain scalar. That is a change in the one place where a silent
//! difference is worst: the user's config file, which the four write commands
//! (`config export`, `set`, `import`, `fix`) read and write back.
//!
//! So the corpus below was captured from the **real PyYAML** — every row's
//! `json_raw` / `json_masked` is byte-for-byte what `json.dumps(...,
//! default=str, ensure_ascii=False)` produced for `yaml.safe_load` of that
//! document, recorded by `xtask/scripts/capture_yaml_compat.py` — and this test
//! holds the Rust reader to it. Rows whose `outcome` is `error` must still be
//! refused. Nothing here is a hand-written expectation.
//!
//! Two refusals are the Rust reader's own, and the capture script records them
//! as `error` with a `note`: a document that is not a mapping, and a duplicate
//! key (PyYAML keeps the last value).
//!
//! What is deliberately *not* pinned: the error text (a different parser names
//! the same problem differently). The rendered YAML bytes are pinned instead by
//! `tests/pyyaml_parity.rs`, and the round-trip property by
//! `yaml_emission_round_trips_through_the_parser` below.

use xpst_config::{ConfigDocument, Value};

/// The corpus. `include_str!` so the fixture is compiled in: a test that
/// silently skipped a missing file would be worse than no test.
const CORPUS: &str = include_str!("fixtures/yaml_compat.json");

#[derive(Debug, Clone)]
struct Row {
    id: String,
    yaml: String,
    json_raw: Option<String>,
    json_masked: Option<String>,
    outcome: String,
}

/// A deliberately tiny reader for the fixture: `serde_json` would need the
/// derive feature, and this is a build-time constant, not user input.
fn rows() -> Vec<Row> {
    let value: Value = xpst_config::yaml::from_str(CORPUS).expect("fixture parses as YAML");
    let items = value
        .as_sequence()
        .expect("the fixture is a YAML sequence of cases");
    items
        .iter()
        .map(|item| {
            let text = |key: &str| {
                item.get(key)
                    .and_then(Value::as_str)
                    .unwrap_or_else(|| panic!("fixture row has no {key}"))
                    .to_string()
            };
            let optional = |key: &str| item.get(key).and_then(Value::as_str).map(str::to_string);
            Row {
                id: text("id"),
                yaml: text("yaml"),
                json_raw: optional("json_raw"),
                json_masked: optional("json_masked"),
                outcome: text("outcome"),
            }
        })
        .collect()
}

#[test]
fn the_corpus_is_not_empty_and_every_row_is_usable() {
    let rows = rows();
    assert_eq!(
        rows.len(),
        28,
        "the compatibility corpus changed size: {:?}",
        rows.iter().map(|r| &r.id).collect::<Vec<_>>()
    );
    for row in &rows {
        assert!(!row.id.is_empty(), "a fixture row has no id");
        assert!(
            row.outcome == "ok" || row.outcome == "error",
            "{}: unknown outcome {:?}",
            row.id,
            row.outcome
        );
        if row.outcome == "ok" {
            assert!(
                row.json_raw.is_some() && row.json_masked.is_some(),
                "{}: an accepted document must carry the JSON the old parser produced",
                row.id
            );
        }
    }
}

#[test]
fn every_document_still_means_what_it_meant() {
    for row in rows() {
        let parsed = ConfigDocument::from_yaml_str(&row.yaml, "config.yaml");
        match row.outcome.as_str() {
            "error" => {
                assert!(
                    parsed.is_err(),
                    "{}: the previous parser refused this document and the new one accepts it",
                    row.id
                );
            }
            "ok" => {
                let document = parsed.unwrap_or_else(|err| {
                    panic!(
                        "{}: the previous parser accepted this document: {err}",
                        row.id
                    )
                });
                assert_eq!(
                    document.to_json_string(false).expect("json"),
                    row.json_raw.clone().expect("fixture json"),
                    "{}: the unmasked JSON rendering changed",
                    row.id
                );
                assert_eq!(
                    document.to_json_string(true).expect("json"),
                    row.json_masked.clone().expect("fixture json"),
                    "{}: the masked JSON rendering changed",
                    row.id
                );
            }
            other => panic!("{}: unknown outcome {other}", row.id),
        }
    }
}

#[test]
fn yaml_emission_round_trips_through_the_parser() {
    // The emitted bytes differ from the old emitter's — that is allowed. What
    // is not allowed is an emitted document that reads back as something else.
    for row in rows().into_iter().filter(|row| row.outcome == "ok") {
        let document = ConfigDocument::from_yaml_str(&row.yaml, "config.yaml").expect("parse");
        let rendered = document.to_yaml_string(false).expect("render");
        let reread = ConfigDocument::from_yaml_str(&rendered, "round-trip.yaml")
            .unwrap_or_else(|err| panic!("{}: emitted YAML does not re-parse: {err}", row.id));
        assert_eq!(
            reread.to_json_string(false).expect("json"),
            row.json_raw.clone().expect("fixture json"),
            "{}: the emitted YAML reads back as a different document",
            row.id
        );
        // And emitting twice is stable — a round trip that drifts each time is
        // not a round trip.
        assert_eq!(
            reread.to_yaml_string(false).expect("render"),
            rendered,
            "{}: rendering is not idempotent",
            row.id
        );
    }
}

#[test]
fn key_order_is_the_documents_order() {
    // `xpst config show` writes the document back out; sorting it would be a
    // visible change on every invocation.
    let document = ConfigDocument::from_yaml_str(
        "zebra: 1\napple: 2\nmango: 3\nBanana: 4\n_under: 5\n",
        "order.yaml",
    )
    .expect("parse");
    let rendered = document.to_yaml_string(false).expect("render");
    let order: Vec<&str> = rendered
        .lines()
        .filter_map(|line| line.split_once(':'))
        .map(|(key, _)| key)
        .collect();
    assert_eq!(order, vec!["zebra", "apple", "mango", "Banana", "_under"]);
}

#[test]
fn a_duplicate_key_is_still_a_refusal() {
    // Last-write-wins would silently change which account is enabled.
    let error = ConfigDocument::from_yaml_str(
        "accounts:\n  youtube:\n    enabled: true\n    enabled: false\n",
        "config.yaml",
    )
    .expect_err("a duplicate key must not be accepted");
    let message = error.to_string();
    assert!(
        message.contains("Invalid YAML in config.yaml"),
        "the error must keep the documented shape: {message}"
    );
    assert!(
        message.to_ascii_lowercase().contains("duplicate"),
        "the error must name the duplicate: {message}"
    );
}

#[test]
fn non_string_keys_stay_typed_and_still_render_as_json() {
    // serde_json stringifies integer and boolean keys; anything else is an
    // error there rather than a silently mangled key.
    let document =
        ConfigDocument::from_yaml_str("ports:\n  1: one\n  true: yes_key\n", "keys.yaml")
            .expect("parse");
    assert_eq!(
        document.to_json_string(false).expect("json"),
        r#"{"ports": {"1": "one", "true": "yes_key"}}"#
    );
    let ports = document.raw().get("ports").and_then(Value::as_mapping);
    let ports = ports.expect("mapping");
    assert!(
        ports.get("1").is_none(),
        "lookup by name must not match a numeric key"
    );
    assert_eq!(ports.len(), 2);
}

#[test]
fn a_byte_order_mark_is_accepted() {
    // Deviation, recorded on purpose: the previous parser treated a BOM as the
    // start of a second document and refused the file. Python's `yaml` reads
    // such a file (Windows editors write it), so accepting it is a fix, not a
    // regression — and this test is what stops that claim going stale.
    let document = ConfigDocument::from_yaml_str("\u{feff}version: 4\naccounts: {}\n", "bom.yaml")
        .expect("a BOM-prefixed config parses");
    assert_eq!(document.version(), 4);
}

#[test]
fn scalar_inference_is_yaml11_like_pyyaml() {
    // The riskiest knob, and the one the write path depends on: PyYAML reads
    // `on`/`yes`/`off`/`no` as booleans, `007` as octal 7 and `1_000` as 1000.
    // A reader that keeps them as strings turns `enabled: yes` into the string
    // `"yes"` on every `config set`/`export`/`import`/`fix`.
    let document = ConfigDocument::from_yaml_str(
        "mode_on: on\nmode_yes: yes\nmode_off: off\nmode_no: no\nleading: 007\nunderscored: 1_000\nsingle_y: y\n",
        "scalars.yaml",
    )
    .expect("parse");
    assert_eq!(
        document.to_json_string(false).expect("json"),
        r#"{"mode_on": true, "mode_yes": true, "mode_off": false, "mode_no": false, "leading": 7, "underscored": 1000, "single_y": "y"}"#
    );
}

#[test]
fn a_yaml11_scalar_survives_the_write_path() {
    // The review's reproducer, end to end: read the document, write it back
    // with the PyYAML emitter, and check the values — not just the bytes.
    let source = "accounts:\n  youtube:\n    enabled: yes\nvideo:\n  crf: 1_000\n  ratio: .inf\n";
    let document = ConfigDocument::from_yaml_str(source, "config.yaml").expect("parse");
    assert_eq!(
        document.to_json_string(false).expect("json"),
        r#"{"accounts": {"youtube": {"enabled": true}}, "video": {"crf": 1000, "ratio": Infinity}}"#
    );
    assert_eq!(
        document.to_yaml_string(false).expect("render"),
        "accounts:\n  youtube:\n    enabled: true\nvideo:\n  crf: 1000\n  ratio: .inf\n"
    );
}
