//! `xpst kb areas` — the discovered knowledge areas in course order.
//!
//! Parity target: `src/xpst/knowledge/cli_kb.py::kb_areas` with
//! `src/xpst/knowledge/organize/difficulty.py::order_areas`. The store is the
//! dependency-free JSON one (`areas.json` beside `nuggets.json`), so this is a
//! local read: no embeddings, no LLM, no network.
//!
//! Two details are load-bearing:
//!
//! * The workspace root is `<XPST_HOME>/knowledge/<name>`, and `XPST_HOME`
//!   defaults to `~/.xpst` — **not** `XPST_CONFIG_DIR`. A sandboxed profile that
//!   sets only the config dir still reads the operator's real knowledge base in
//!   Python, so the same rule is reproduced here rather than "fixed".
//! * `Workspace.resolve` creates the directory by default, so a read of a
//!   never-used workspace leaves an empty one behind. That side effect is
//!   reproduced: a later `kb add` expects the directory to exist.
//!
//! **Divergence, recorded in the ledger note:** text mode renders plain lines
//! instead of Rich markup (`--json` is the parity surface).

use std::io::Write;
use std::path::PathBuf;

use clap::ArgMatches;
use xpst_config::Value;

use crate::output::{integer, null, object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `_SAFE_NAME_RE`: alphanumerics, dash, underscore, colon, dot.
fn valid_workspace_name(name: &str) -> bool {
    !name.is_empty()
        && !name.contains("..")
        && !name.starts_with('~')
        && name.chars().all(|character| {
            character.is_alphanumeric() || matches!(character, '-' | '_' | ':' | '.')
        })
}

/// `_xpst_home()`: `XPST_HOME`, else `~/.xpst`.
fn xpst_home() -> Option<PathBuf> {
    if let Some(home) = std::env::var_os("XPST_HOME") {
        if !home.is_empty() {
            return Some(crate::commands::state::expanduser(&home.to_string_lossy()));
        }
    }
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".xpst"))
}

/// One `Area` row, as `kb areas` reports it.
struct Area {
    id: String,
    label: String,
    order_index: i64,
    nuggets: usize,
}

/// `JsonKnowledgeStore._load_file` for `areas.json`: a corrupt file is
/// quarantined to `<name>.corrupt` and the store starts empty.
fn load_areas(path: &std::path::Path) -> Vec<Area> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    let parsed = xpst_config::yaml::from_str(&text);
    let Ok(document) = parsed else {
        let _ = std::fs::rename(path, path.with_extension("json.corrupt"));
        return Vec::new();
    };
    let Some(map) = document.as_mapping() else {
        let _ = std::fs::rename(path, path.with_extension("json.corrupt"));
        return Vec::new();
    };
    let mut areas = Vec::new();
    for (key, value) in map.iter() {
        let Some(id) = key.as_str() else {
            continue;
        };
        let Some(record) = value.as_mapping() else {
            continue;
        };
        let label = record
            .get("label")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let order_index = record
            .get("order_index")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        let nuggets = record
            .get("nugget_ids")
            .and_then(Value::as_sequence)
            .map(<[Value]>::len)
            .unwrap_or_default();
        areas.push(Area {
            id: id.to_string(),
            label,
            order_index,
            nuggets,
        });
    }
    areas
}

/// Run `kb areas`.
pub fn areas(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let workspace = leaf_string(leaf, "opt:workspace").unwrap_or_else(|| "default".to_string());
    if !valid_workspace_name(&workspace) {
        let message = format!(
            "Invalid workspace name: must contain only alphanumeric, dash, underscore, \
             colon, or dot characters (got: {workspace:?})"
        );
        if json {
            let payload = crate::output::error_object("INVALID_WORKSPACE", &message, Vec::new());
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    }
    let Some(home) = xpst_home() else {
        let message = "cannot determine the xPST home: neither XPST_HOME nor HOME is set";
        if json {
            let payload = crate::output::error_object("CONFIG_ERROR", message, Vec::new());
            let _ = write_python_json(out, &payload);
        } else {
            let _ = writeln!(err, "{message}");
        }
        return exit::CONFIG_ERROR;
    };

    let root = home.join("knowledge").join(&workspace);
    // `Workspace.resolve(create=True)` — a read of a never-used workspace leaves
    // an empty one behind, exactly as Python does.
    if let Err(error) = std::fs::create_dir_all(&root) {
        let _ = writeln!(
            err,
            "xpst kb areas: cannot create {}: {error}",
            root.display()
        );
        return exit::GENERAL;
    }

    let mut areas = load_areas(&root.join("areas.json"));
    // `order_areas`: stable sort by (order_index, label, id).
    areas.sort_by(|left, right| {
        (left.order_index, &left.label, &left.id).cmp(&(right.order_index, &right.label, &right.id))
    });

    if json {
        let rows: Vec<Value> = areas
            .iter()
            .map(|area| {
                object(vec![
                    ("label", string(area.label.clone())),
                    ("order_index", integer(area.order_index)),
                    ("nuggets", integer(area.nuggets as i64)),
                    // `getattr(area, "difficulty", None)` — `Area` has no such
                    // field, so the payload always carries null.
                    ("difficulty", null()),
                ])
            })
            .collect();
        let payload = object(vec![("areas", crate::output::array(rows))]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    if areas.is_empty() {
        let _ = write_text(out, "No areas yet. Run 'xpst kb organize'.");
        return exit::SUCCESS;
    }
    let mut text = String::new();
    for area in &areas {
        text.push_str(&format!(
            "{}. {} ({} nuggets)\n",
            area.order_index + 1,
            area.label,
            area.nuggets
        ));
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workspace_names_are_validated_like_python() {
        assert!(valid_workspace_name("default"));
        assert!(valid_workspace_name("my-ws_1"));
        assert!(!valid_workspace_name(""));
        assert!(!valid_workspace_name("../escape"));
        assert!(!valid_workspace_name("~home"));
        assert!(!valid_workspace_name("a/b"));
    }

    #[test]
    fn areas_are_ordered_by_index_then_label() {
        let dir = std::env::temp_dir().join(format!("xpst-kb-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("areas.json"),
            r#"{"b": {"label": "Beta", "order_index": 1, "nugget_ids": ["n1"]},
                "a": {"label": "Alpha", "order_index": 0, "nugget_ids": ["n1", "n2"]}}"#,
        )
        .unwrap();
        let mut areas = load_areas(&dir.join("areas.json"));
        areas.sort_by(|left, right| {
            (left.order_index, &left.label, &left.id).cmp(&(
                right.order_index,
                &right.label,
                &right.id,
            ))
        });
        assert_eq!(areas[0].label, "Alpha");
        assert_eq!(areas[0].nuggets, 2);
        assert_eq!(areas[1].label, "Beta");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_corrupt_store_is_quarantined() {
        let dir = std::env::temp_dir().join(format!("xpst-kb-corrupt-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("areas.json");
        std::fs::write(&path, "{not json").unwrap();
        assert!(load_areas(&path).is_empty());
        assert!(dir.join("areas.json.corrupt").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
