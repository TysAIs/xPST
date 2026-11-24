//! Drift guards against the Python MCP surface.
//!
//! The Python registry in `src/xpst/mcp/server.py` is the contract: this
//! server may not gain a tool Python does not have, may not drop one it does,
//! and may not reclassify a mutating tool as read-only. The Python source is
//! parsed here directly — no Python interpreter, no installed `xpst` package,
//! no network — so the gate runs anywhere `cargo test` runs.

use std::path::{Path, PathBuf};

use xpst_mcp::guard;
use xpst_mcp::registry::{registry, Consent, Implementation};

fn repo_root() -> PathBuf {
    let mut dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    loop {
        if dir.join("src/xpst/mcp/server.py").is_file() {
            return dir;
        }
        if !dir.pop() {
            panic!(
                "cannot find src/xpst/mcp/server.py above {}",
                env!("CARGO_MANIFEST_DIR")
            );
        }
    }
}

fn python_server_source() -> String {
    let path = repo_root().join("src/xpst/mcp/server.py");
    std::fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("cannot read {}: {err}", path.display()))
}

/// Tool names in the order the Python `TOOLS` list declares them.
///
/// The registry is formatted with one `Tool(` per literal followed by its
/// `name="..."` line (ruff-enforced), so a line scanner is enough and does not
/// pretend to be a Python parser.
fn python_tool_names(source: &str) -> Vec<String> {
    let mut names = Vec::new();
    let mut inside_tool = false;
    for line in source.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("Tool(") {
            inside_tool = true;
            continue;
        }
        if inside_tool {
            if let Some(rest) = trimmed.strip_prefix("name=\"") {
                if let Some(end) = rest.find('"') {
                    names.push(rest[..end].to_string());
                }
                inside_tool = false;
            }
        }
    }
    names
}

/// The quoted entries of a module-level Python set such as `_MUTATING_TOOLS`.
/// Comment text is stripped first: the Python literal carries prose with
/// commas and quoted names in it, and the set's members are the only strings on
/// their own lines.
fn python_string_set(source: &str, set_name: &str) -> Vec<String> {
    let marker = format!("{set_name} = {{");
    let start = source
        .find(&marker)
        .unwrap_or_else(|| panic!("cannot find {set_name} in the Python registry"));
    let body = &source[start + marker.len()..];
    let end = body
        .find("\n}")
        .unwrap_or_else(|| panic!("unterminated {set_name} literal"));

    let mut out = Vec::new();
    for line in body[..end].lines() {
        let code = line.split('#').next().unwrap_or_default();
        let mut rest = code;
        while let Some(open) = rest.find('"') {
            let after = &rest[open + 1..];
            let Some(close) = after.find('"') else { break };
            out.push(after[..close].to_string());
            rest = &after[close + 1..];
        }
    }
    out
}

fn captured_fixture() -> serde_json::Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/python_tool_registry.json");
    let text = std::fs::read_to_string(&path).expect("fixture is checked in");
    serde_json::from_str(&text).expect("fixture is valid JSON")
}

fn fixture_names(fixture: &serde_json::Value) -> Vec<String> {
    fixture
        .as_array()
        .expect("fixture is an array")
        .iter()
        .map(|tool| tool["name"].as_str().expect("name").to_string())
        .collect()
}

#[test]
fn the_python_registry_is_the_46_tool_contract() {
    let names = python_tool_names(&python_server_source());
    assert_eq!(
        names.len(),
        46,
        "the Python registry changed size: {names:?}"
    );
}

#[test]
fn the_captured_fixture_matches_the_python_source() {
    let source = python_tool_names(&python_server_source());
    let fixture = fixture_names(&captured_fixture());
    assert_eq!(
        fixture, source,
        "tests/fixtures/python_tool_registry.json is stale: \
         re-run scripts/capture_mcp_tool_registry.py"
    );
}

#[test]
fn rust_tool_names_match_python_in_order() {
    let source = python_tool_names(&python_server_source());
    let ours: Vec<String> = registry()
        .entries()
        .iter()
        .map(|entry| entry.name.to_string())
        .collect();
    assert_eq!(
        ours, source,
        "the Rust surface drifted from the Python registry"
    );
}

#[test]
fn every_implemented_tool_schema_forbids_unknown_arguments() {
    let reg = registry();
    let mut problems = Vec::new();
    for entry in reg.entries().iter().filter(|entry| entry.is_ported()) {
        let schema = &entry.spec().input_schema;
        if schema.get("type") != Some(&serde_json::json!("object")) {
            problems.push(format!("{}: schema is not an object schema", entry.name));
        }
        if schema.get("additionalProperties") != Some(&serde_json::json!(false)) {
            problems.push(format!("{}: additionalProperties is not false", entry.name));
        }
    }
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn every_captured_schema_forbids_unknown_arguments() {
    let fixture = captured_fixture();
    let mut bad = Vec::new();
    for tool in fixture.as_array().expect("array") {
        if tool["input_schema"]["additionalProperties"] != serde_json::json!(false) {
            bad.push(tool["name"].as_str().unwrap_or("<unnamed>").to_string());
        }
    }
    assert!(
        bad.is_empty(),
        "Python schemas without additionalProperties:false: {bad:?}"
    );
}

#[test]
fn mutating_classification_matches_python() {
    let source = python_string_set(&python_server_source(), "_MUTATING_TOOLS");
    let mut python: Vec<String> = source;
    let mut rust: Vec<String> = guard::MUTATING_TOOLS
        .iter()
        .map(|s| s.to_string())
        .collect();
    python.sort();
    rust.sort();
    assert_eq!(rust, python, "consent tiers drifted from _MUTATING_TOOLS");
}

#[test]
fn registry_rows_carry_the_python_consent_tier() {
    let configured = guard::mutating_names();
    let mut problems = Vec::new();
    for entry in registry().entries() {
        let expected = if configured.contains(entry.name) {
            Consent::Mutating
        } else {
            Consent::ReadOnly
        };
        if entry.consent != expected {
            problems.push(format!(
                "{}: registry says {:?}, _MUTATING_TOOLS says {:?}",
                entry.name, entry.consent, expected
            ));
        }
    }
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn names_are_unique_across_the_registry() {
    let names: Vec<&str> = registry()
        .entries()
        .iter()
        .map(|entry| entry.name)
        .collect();
    let mut sorted = names.clone();
    sorted.sort_unstable();
    sorted.dedup();
    assert_eq!(
        sorted.len(),
        names.len(),
        "duplicate tool names in the registry"
    );
}

#[test]
fn tools_list_renders_every_registry_row() {
    let tools = registry().tools();
    let rendered: Vec<String> = tools.iter().map(|tool| tool.name.to_string()).collect();
    let ours: Vec<String> = registry()
        .entries()
        .iter()
        .map(|entry| entry.name.to_string())
        .collect();
    assert_eq!(rendered, ours);
    assert_eq!(tools.len(), 46);
}

#[test]
fn every_stub_answers_with_a_typed_not_implemented_error() {
    let reg = registry();
    let ctx = xpst_mcp::ToolContext::new(xpst_mcp::StorePaths::at("/nonexistent-xpst-profile"));
    let mut stubs = 0;
    for entry in reg.entries() {
        if !matches!(entry.implementation, Implementation::Stub { .. }) {
            continue;
        }
        stubs += 1;
        let err = reg
            .dispatch(&ctx, entry.name, &serde_json::json!({}))
            .expect_err(&format!("{} must not fake a success", entry.name));
        assert_eq!(
            err.code(),
            "not_implemented",
            "{} returned {:?}",
            entry.name,
            err
        );
        let payload = err.to_payload();
        assert_eq!(payload["tool"], serde_json::json!(entry.name));
        assert_eq!(payload["ok"], serde_json::json!(false));
    }
    assert_eq!(stubs, reg.stub_count());
    assert_eq!(reg.ported_count() + stubs, reg.len());
}

#[test]
fn unknown_tools_are_reported_as_unknown() {
    let reg = registry();
    let ctx = xpst_mcp::ToolContext::new(xpst_mcp::StorePaths::at("/nonexistent-xpst-profile"));
    let err = reg
        .dispatch(&ctx, "xpst_not_a_tool", &serde_json::json!({}))
        .expect_err("unknown tool");
    assert_eq!(err.code(), "unknown_tool");
}

#[test]
fn the_ported_surface_is_exactly_the_read_only_network_free_set() {
    // This is the claim the README makes; if a handler is added or removed the
    // documentation has to change with it.
    let mut ported: Vec<&str> = registry()
        .entries()
        .iter()
        .filter(|entry| entry.is_ported())
        .map(|entry| entry.name)
        .collect();
    ported.sort_unstable();
    assert_eq!(
        ported,
        vec![
            "kb_areas",
            "kb_query",
            "xpst_activity",
            "xpst_auth_start",
            "xpst_best_time",
            "xpst_bio_get",
            "xpst_byo_app",
            "xpst_capabilities",
            "xpst_chat",
            "xpst_config_show",
            "xpst_cross_post_analytics",
            "xpst_followers",
            "xpst_generate_ideas",
            "xpst_preflight",
            "xpst_providers",
            "xpst_rate_limit_status",
            "xpst_readiness",
            "xpst_schedule_add",
            "xpst_schedule_cancel",
            "xpst_schedule_list",
            "xpst_schedule_pause",
            "xpst_schedule_resume",
            "xpst_search",
            "xpst_security_audit",
            "xpst_setup_reset",
            "xpst_setup_resume",
            "xpst_setup_start",
            "xpst_setup_status",
            "xpst_status",
            "xpst_stream",
            "xpst_suggest_caption",
            "xpst_transcript",
        ]
    );
}

/// Tool names listed in a README table that starts with the given heading.
fn readme_table_names(heading: &str) -> Vec<String> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("README.md");
    let text = std::fs::read_to_string(&path).expect("crate README is checked in");
    let mut names = Vec::new();
    let mut inside = false;
    for line in text.lines() {
        if line.starts_with("## ") {
            inside = line.trim_end() == heading;
            continue;
        }
        if inside {
            if let Some(rest) = line.trim_start().strip_prefix("| `") {
                if let Some(end) = rest.find('`') {
                    names.push(rest[..end].to_string());
                }
            }
        }
    }
    names.sort();
    names
}

#[test]
fn the_readme_ported_and_stub_tables_match_the_registry() {
    let reg = registry();
    let mut ported: Vec<String> = reg
        .entries()
        .iter()
        .filter(|entry| entry.is_ported())
        .map(|entry| entry.name.to_string())
        .collect();
    let mut stubbed: Vec<String> = reg
        .entries()
        .iter()
        .filter(|entry| !entry.is_ported())
        .map(|entry| entry.name.to_string())
        .collect();
    ported.sort();
    stubbed.sort();

    assert_eq!(
        readme_table_names("## Ported tools (local, no network)"),
        ported,
        "crates/xpst-mcp/README.md no longer matches the ported set"
    );
    assert_eq!(
        readme_table_names("## Stubbed tools"),
        stubbed,
        "crates/xpst-mcp/README.md no longer matches the stubbed set"
    );
}
