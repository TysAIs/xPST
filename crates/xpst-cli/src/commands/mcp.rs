//! `xpst mcp list` — the MCP tool surface.
//!
//! Parity target: `src/xpst/cli.py::mcp_list`, which prints
//! `json.dumps([{"name", "description"}], indent=2)` over `xpst.mcp.server.TOOLS`
//! in Python's order. The list is served from the registry fixture the MCP crate
//! already captures from the shipped Python
//! (`crates/xpst-mcp/tests/fixtures/python_tool_registry.json`), so the two
//! engines cannot drift: a tool added to Python without re-capturing fails the
//! MCP crate's own parity test.
//!
//! **Divergences, recorded in the ledger note:** text mode renders a plain list
//! instead of Rich's box table (`--json` is the parity surface), and the
//! `MCP package not installed` branch cannot happen — the Rust binary *is* the
//! MCP server, so the registry is always available.

use std::io::Write;

use clap::ArgMatches;
use serde_json::Value;

use crate::output::{null, object, python_pretty_json_cfg, string, write_text};
use crate::{exit, flag, GlobalOptions};

/// The captured Python tool registry, in Python's order.
const PYTHON_REGISTRY: &str =
    include_str!("../../../xpst-mcp/tests/fixtures/python_tool_registry.json");

/// One captured tool definition.
struct Tool {
    name: String,
    description: Option<String>,
}

fn tools() -> Result<Vec<Tool>, String> {
    let captured: Vec<Value> =
        serde_json::from_str(PYTHON_REGISTRY).map_err(|error| error.to_string())?;
    let tools: Vec<Tool> = captured
        .iter()
        .map(|entry| Tool {
            name: entry
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            description: entry
                .get("description")
                .and_then(Value::as_str)
                .map(str::to_string),
        })
        .collect();
    if tools.is_empty() {
        return Err("the captured MCP registry is empty".to_string());
    }
    Ok(tools)
}

/// Run `mcp list`.
pub fn list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let tools = match tools() {
        Ok(tools) => tools,
        Err(error) => {
            let _ = writeln!(err, "xpst mcp list: {error}");
            return exit::GENERAL;
        }
    };

    if json {
        let payload = xpst_config::Value::Sequence(
            tools
                .iter()
                .map(|tool| {
                    object(vec![
                        ("name", string(tool.name.clone())),
                        (
                            "description",
                            tool.description.clone().map_or_else(null, string),
                        ),
                    ])
                })
                .collect(),
        );
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let mut text = format!("\nMCP Tools ({})\n\n", tools.len());
    for (index, tool) in tools.iter().enumerate() {
        let description = tool
            .description
            .as_deref()
            .unwrap_or("")
            .lines()
            .next()
            .unwrap_or("");
        let description: String = description.chars().take(80).collect();
        text.push_str(&format!(
            "{:>3}  {}  {}\n",
            index + 1,
            tool.name,
            description
        ));
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_registry_carries_every_python_tool_in_order() {
        let tools = tools().expect("registry parses");
        assert_eq!(tools.len(), 46, "the Python surface has 46 tools");
        assert_eq!(tools[0].name, "xpst_run");
        assert_eq!(tools[45].name, "xpst_chat");
        assert!(tools.iter().all(|tool| tool.description.is_some()));
    }
}
