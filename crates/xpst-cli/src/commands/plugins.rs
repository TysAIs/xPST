//! `xpst plugins list` / `xpst plugins docs` — the installed plugin inventory.
//!
//! Parity target: `src/xpst/cli.py::plugins_list` / `plugins_docs` →
//! `xpst.plugins.PluginManager`.
//!
//! **A plugin is Python code.** `PluginManager.discover()` imports every
//! `~/.xpst/plugins/*.py` and calls its `register()`, which is arbitrary code by
//! design (the module's own docstring says so). A Rust binary cannot execute it,
//! and a static parse of the `register()` return literal would silently misreport
//! any plugin that computes its dict — worse than not answering.
//!
//! So the inventory is asked of an interpreter, exactly as `kb doctor` asks one
//! whether the knowledge extra is installed: `XPST_PYTHON`, else `python3`, else
//! `python`, running a small bootstrap that imports the shipped `PluginManager`
//! and prints the rows as JSON. This module owns the directory resolution, the
//! payload and the markdown; the interpreter owns the loading.
//!
//! **Divergences, recorded in the ledger:** with no interpreter that can import
//! `xpst`, the inventory is empty (Python would list the plugins); and text mode
//! prints plain lines instead of Rich's table.

use std::io::Write;
use std::path::PathBuf;

use clap::ArgMatches;
use serde_json::Value;

use crate::output::{boolean, integer, object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// The bootstrap the interpreter runs.
///
/// `sys.argv[1]` is the plugin directory, so the engine decides which directory
/// is read and the interpreter only loads it. `default=str` keeps a plugin whose
/// `config_options` carries a class or a callable from breaking the probe — the
/// Python renderer would print it with `str()` anyway.
const PLUGIN_PROBE: &str = "\
import json, sys
from xpst.plugins import PluginManager
manager = PluginManager(sys.argv[1])
manager.discover()
rows = []
for summary in manager.list_plugins():
    detail = manager.get(summary['name']) or {}
    row = dict(summary)
    row['config_options'] = detail.get('config_options', [])
    rows.append(row)
print(json.dumps(rows, default=str))
";

/// `~/.xpst/plugins` — `DEFAULT_PLUGIN_DIR` in `xpst/plugins/__init__.py`.
///
/// Python resolves it from `HOME` (`Path("~/.xpst/plugins").expanduser()`), not
/// from `XPST_CONFIG_DIR`, so a profile override does not move the plugin
/// directory. Reproduced: a user who points `XPST_CONFIG_DIR` elsewhere still
/// gets the plugins they installed under their home directory.
pub fn plugin_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(PathBuf::from(home).join(".xpst").join("plugins"))
}

/// One installed plugin, as the interpreter reported it.
struct PluginRow {
    name: String,
    version: String,
    description: String,
    has_uploader: bool,
    has_source: bool,
    config_options: Vec<Value>,
}

impl PluginRow {
    fn from_json(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        let name = object.get("name")?.as_str()?.to_string();
        Some(Self {
            name,
            version: object
                .get("version")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            description: object
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            has_uploader: object
                .get("has_uploader")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            has_source: object
                .get("has_source")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            config_options: object
                .get("config_options")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
        })
    }
}

/// Interpreters to try, in the order the repo uses elsewhere.
fn interpreters() -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(interpreter) = std::env::var_os("XPST_PYTHON") {
        if !interpreter.is_empty() {
            candidates.push(PathBuf::from(interpreter));
        }
    }
    candidates.push(PathBuf::from("python3"));
    candidates.push(PathBuf::from("python"));
    candidates
}

/// Ask an interpreter for the plugin inventory.
///
/// Returns `None` when no interpreter could answer, which the callers report as
/// an empty inventory (the honest answer for an engine that cannot load Python
/// plugins).
fn discover(directory: &std::path::Path) -> Option<Vec<PluginRow>> {
    for interpreter in interpreters() {
        let Ok(output) = std::process::Command::new(&interpreter)
            .arg("-c")
            .arg(PLUGIN_PROBE)
            .arg(directory)
            .output()
        else {
            continue;
        };
        if !output.status.success() {
            continue;
        }
        let Ok(parsed) = serde_json::from_slice::<Value>(&output.stdout) else {
            continue;
        };
        let Some(rows) = parsed.as_array() else {
            continue;
        };
        return Some(rows.iter().filter_map(PluginRow::from_json).collect());
    }
    None
}

/// The inventory, creating the plugin directory the way `discover()` does.
fn inventory() -> Vec<PluginRow> {
    let Some(directory) = plugin_dir() else {
        return Vec::new();
    };
    if !directory.exists() {
        // `PluginManager.discover()` creates the directory on a fresh profile.
        let _ = std::fs::create_dir_all(&directory);
        return Vec::new();
    }
    discover(&directory).unwrap_or_default()
}

/// Run `plugins list`.
pub fn list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    _err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let plugins = inventory();

    if json_mode {
        let rows = plugins
            .iter()
            .map(|plugin| {
                object(vec![
                    ("name", string(plugin.name.clone())),
                    ("version", string(plugin.version.clone())),
                    ("description", string(plugin.description.clone())),
                    ("has_uploader", boolean(plugin.has_uploader)),
                    ("has_source", boolean(plugin.has_source)),
                ])
            })
            .collect();
        let payload = object(vec![
            ("plugins", xpst_config::Value::Sequence(rows)),
            ("count", integer(plugins.len() as i64)),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    if plugins.is_empty() {
        let _ = write_text(out, "No plugins installed.");
        return exit::SUCCESS;
    }

    // Rich renders a bordered table; the facts are the contract (see the
    // deviations), so this is a plain listing with the same columns.
    let mut text = String::from("Installed Plugins\n");
    for plugin in &plugins {
        text.push_str(&format!(
            "  {} | {} | {} | {} | {}\n",
            plugin.name,
            plugin.version,
            plugin.description,
            if plugin.has_uploader { "✓" } else { "✗" },
            if plugin.has_source { "✓" } else { "✗" },
        ));
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

/// Run `plugins docs`.
pub fn docs(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let output = leaf_string(leaf, "opt:output").filter(|value| !value.is_empty());
    let plugins = inventory();

    if plugins.is_empty() {
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(true)),
                ("plugins", integer(0)),
                ("message", string("No plugins installed")),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(
                out,
                "No plugins installed. Place .py files in ~/.xpst/plugins/",
            );
        }
        return exit::SUCCESS;
    }

    let markdown = render_markdown(&plugins);

    if let Some(path) = output {
        let path = PathBuf::from(path);
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                let _ = std::fs::create_dir_all(parent);
            }
        }
        if let Err(error) = std::fs::write(&path, &markdown) {
            let _ = writeln!(err, "xpst plugins docs: cannot write {path:?}: {error}");
            return exit::GENERAL;
        }
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(true)),
                ("output", string(path.display().to_string())),
                ("plugins", integer(plugins.len() as i64)),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, &format!("✓ Plugin docs written to {}", path.display()));
        }
        return exit::SUCCESS;
    }

    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("content", string(markdown.clone())),
            ("plugins", integer(plugins.len() as i64)),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(out, &markdown);
    }
    exit::SUCCESS
}

/// The markdown document, byte for byte as `plugins_docs` builds it.
fn render_markdown(plugins: &[PluginRow]) -> String {
    let mut lines: Vec<String> = Vec::new();
    lines.push("# xPST Plugin Documentation\n".to_string());
    lines.push(format!(
        "Auto-generated on {}\n",
        jiff::Timestamp::now()
            .to_zoned_datetime(jiff::tz::TimeZone::system())
            .strftime("%Y-%m-%d %H:%M")
    ));
    lines.push(format!("**{} plugin(s) installed**\n", plugins.len()));
    lines.push("---\n".to_string());

    for plugin in plugins {
        lines.push(format!("## {}\n", plugin.name));
        lines.push(format!("- **Version:** {}", plugin.version));
        lines.push(format!("- **Description:** {}", plugin.description));
        let mut capabilities: Vec<&str> = Vec::new();
        if plugin.has_uploader {
            capabilities.push("Uploader");
        }
        if plugin.has_source {
            capabilities.push("Source");
        }
        lines.push(format!(
            "- **Capabilities:** {}",
            if capabilities.is_empty() {
                "None".to_string()
            } else {
                capabilities.join(", ")
            }
        ));
        if !plugin.config_options.is_empty() {
            lines.push("- **Config Options:**".to_string());
            for option in &plugin.config_options {
                match option {
                    Value::Object(map) => {
                        let name = map
                            .get("name")
                            .map(render_scalar)
                            .unwrap_or_else(|| "?".to_string());
                        let description = map
                            .get("description")
                            .map(render_scalar)
                            .unwrap_or_default();
                        let default = map.get("default").map(render_scalar).unwrap_or_default();
                        lines.push(format!(
                            "  - `{name}`: {description} (default: `{default}`)"
                        ));
                    }
                    other => lines.push(format!("  - `{}`", render_scalar(other))),
                }
            }
        }
        lines.push(String::new());
    }

    lines.join("\n")
}

/// `str()` for a JSON scalar, which is what Python's f-string interpolation does.
fn render_scalar(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(name: &str, uploader: bool, source: bool) -> PluginRow {
        PluginRow {
            name: name.to_string(),
            version: "1.0.0".to_string(),
            description: "does a thing".to_string(),
            has_uploader: uploader,
            has_source: source,
            config_options: Vec::new(),
        }
    }

    #[test]
    fn the_markdown_matches_the_python_layout() {
        let markdown = render_markdown(&[row("alpha", true, false)]);
        let body: Vec<&str> = markdown.lines().collect();
        assert_eq!(body[0], "# xPST Plugin Documentation");
        assert!(body[1].is_empty());
        assert!(body[2].starts_with("Auto-generated on "));
        assert_eq!(body[4], "**1 plugin(s) installed**");
        assert_eq!(body[6], "---");
        assert_eq!(body[8], "## alpha");
        assert_eq!(body[10], "- **Version:** 1.0.0");
        assert_eq!(body[11], "- **Description:** does a thing");
        assert_eq!(body[12], "- **Capabilities:** Uploader");
        assert!(markdown.ends_with('\n'));
    }

    #[test]
    fn capabilities_join_in_python_order() {
        let markdown = render_markdown(&[row("both", true, true), row("none", false, false)]);
        assert!(markdown.contains("- **Capabilities:** Uploader, Source"));
        assert!(markdown.contains("- **Capabilities:** None"));
    }

    #[test]
    fn a_config_option_renders_its_default() {
        let mut plugin = row("opts", true, false);
        plugin.config_options = vec![serde_json::json!({
            "name": "api_key",
            "description": "the key",
            "default": "none"
        })];
        let markdown = render_markdown(&[plugin]);
        assert!(markdown.contains("  - `api_key`: the key (default: `none`)"));
    }
}
