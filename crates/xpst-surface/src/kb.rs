//! The knowledge-base query payload.
//!
//! Parity target: `src/xpst/knowledge/query.py::query_nuggets` as the MCP tools
//! `kb_query` and `xpst_search` serve it, and `xpst kb query` / `xpst search`
//! print it.
//!
//! Python tries the embedder first and falls back to substring matching when it
//! is unavailable *or* when the store holds no vectors. The Rust engine has no
//! embedding stack, so it always takes the fallback — which is the same answer
//! on a machine without the knowledge extra and on any store that has not been
//! embedded.

use std::path::{Path, PathBuf};

use xpst_config::output::{array, integer, null, number, object, python_repr, string};
use xpst_config::Value;

use crate::config_view::truthy;
use crate::kb_store::{self, Area, Nugget};

/// `_SAFE_NAME_RE`: alphanumerics, dash, underscore, colon, dot.
pub fn valid_workspace_name(name: &str) -> bool {
    !name.is_empty()
        && !name.contains("..")
        && !name.starts_with('~')
        && name.chars().all(|character| {
            character.is_alphanumeric() || matches!(character, '-' | '_' | ':' | '.')
        })
}

/// `_validate_name(name, field)` — the three refusals, with Python's messages.
///
/// The regex is ASCII-only (`^[a-zA-Z0-9:_\-.]+$`), which is stricter than
/// [`valid_workspace_name`]'s `is_alphanumeric()`: a name with a non-ASCII
/// letter passes the workspace check and fails this one. Both are reproduced
/// rather than unified, because each is what its own Python caller does.
pub fn validate_name(name: &str, field: &str) -> Result<(), String> {
    let ascii_safe = !name.is_empty()
        && name.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, ':' | '_' | '-' | '.')
        });
    if !ascii_safe {
        return Err(format!(
            "Invalid {field}: must contain only alphanumeric, dash, underscore, colon, or dot \
             characters (got: {})",
            python_repr(name)
        ));
    }
    if name.contains("..") {
        return Err(format!(
            "Invalid {field}: parent traversal (..) is not allowed"
        ));
    }
    if name.starts_with('~') {
        return Err(format!("Invalid {field}: leading tilde is not allowed"));
    }
    Ok(())
}

/// `_xpst_home()`: `XPST_HOME`, else `~/.xpst`.
pub fn xpst_home() -> Option<PathBuf> {
    if let Some(home) = std::env::var_os("XPST_HOME") {
        if !home.is_empty() {
            return Some(expanduser(&home.to_string_lossy()));
        }
    }
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".xpst"))
}

/// `Path.expanduser()` for the two forms the engine writes.
pub fn expanduser(path: &str) -> PathBuf {
    if path == "~" {
        return std::env::var_os("HOME").map_or_else(|| PathBuf::from(path), PathBuf::from);
    }
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(path)
}

/// Why a workspace could not be resolved.
#[derive(Debug)]
pub enum WorkspaceError {
    /// `Workspace.resolve()` raises an uncaught `ValueError`.
    Invalid(String),
    /// Neither `XPST_HOME` nor `HOME` is set.
    NoHome,
}

/// `Workspace.resolve(name, create=...)` — the root directory, not the name.
pub fn resolve_workspace(name: &str, create: bool) -> Result<PathBuf, WorkspaceError> {
    let home = xpst_home().ok_or(WorkspaceError::NoHome)?;
    resolve_workspace_in(&home, name, create)
}

/// [`resolve_workspace`] against an explicit home directory.
pub fn resolve_workspace_in(
    home: &Path,
    name: &str,
    create: bool,
) -> Result<PathBuf, WorkspaceError> {
    if !valid_workspace_name(name) {
        return Err(WorkspaceError::Invalid(format!(
            "Invalid workspace name: must contain only alphanumeric, dash, underscore, \
             colon, or dot characters (got: {})",
            python_repr(name)
        )));
    }
    let root = home.join("knowledge").join(name);
    if create {
        if let Err(error) = std::fs::create_dir_all(&root) {
            return Err(WorkspaceError::Invalid(format!(
                "cannot create {}: {error}",
                root.display()
            )));
        }
    }
    Ok(root)
}

/// The two files `JsonKnowledgeStore.__init__` loads, in its order.
pub struct Store {
    pub nuggets: Vec<Nugget>,
    pub areas: Vec<Area>,
}

/// Open a workspace store the way the Python loader does.
pub fn open_store(root: &Path) -> Result<Store, String> {
    let nuggets = kb_store::load_nuggets(&root.join("nuggets.json"))?;
    let areas = kb_store::load_areas(&root.join("areas.json"))?;
    Ok(Store { nuggets, areas })
}

/// `kb_areas()` — the discovered areas in course order.
///
/// Ports `xpst.knowledge.mcp.tools.kb_areas`: `Workspace.resolve(workspace)`
/// (create=True, so a fresh workspace is materialized), the JSON store's areas,
/// `order_areas`, and `{workspace, count, areas: [{order, label, nugget_count}]}`.
/// `order` is `order_index + 1`, so the payload is 1-based.
pub fn areas_payload(workspace: &str) -> Result<Value, String> {
    let home = xpst_home().ok_or_else(|| {
        "cannot determine the xPST home: neither XPST_HOME nor HOME is set".to_string()
    })?;
    areas_payload_in(&home, workspace)
}

/// [`areas_payload`] against an explicit home directory.
pub fn areas_payload_in(home: &Path, workspace: &str) -> Result<Value, String> {
    let root = resolve_workspace_in(home, workspace, true).map_err(|error| match error {
        WorkspaceError::Invalid(message) => message,
        WorkspaceError::NoHome => {
            "cannot determine the xPST home: neither XPST_HOME nor HOME is set".to_string()
        }
    })?;
    let store = open_store(&root)?;
    let mut areas = store.areas;
    kb_store::order_areas(&mut areas);

    let rows: Vec<Value> = areas
        .iter()
        .map(|area| {
            object(vec![
                ("order", order_plus_one(&area.order_index)),
                ("label", area.label.clone()),
                ("nugget_count", integer(area.nugget_ids.len() as i64)),
            ])
        })
        .collect();
    Ok(object(vec![
        ("workspace", string(workspace)),
        ("count", integer(rows.len() as i64)),
        ("areas", array(rows)),
    ]))
}

/// `area.order_index + 1`, keeping the numeric type Python keeps.
fn order_plus_one(value: &Value) -> Value {
    match value {
        Value::Number(value) => match value.as_i64() {
            Some(whole) => integer(whole + 1),
            None => number(value.as_f64().unwrap_or_default() + 1.0),
        },
        // `None + 1` raises `TypeError` in Python; the store's `order_index`
        // defaults to 0 when the key is absent, so this is the same value.
        Value::Null => integer(1),
        other => other.clone(),
    }
}

/// `nugget.source_url or nugget.source_video_id`.
pub fn citation(nugget: &Nugget) -> Value {
    if truthy(&nugget.source_url) {
        nugget.source_url.clone()
    } else {
        nugget.source_video_id.clone()
    }
}

/// Why a query could not run.
///
/// Two renderings, because the two surfaces print different things: the CLI
/// prints Python's exception *line* (`ValueError: ...`), while the MCP server
/// catches the exception and serves `Error: {str(e)}` — the message without the
/// class name.
#[derive(Debug)]
pub enum QueryError {
    /// `Workspace.resolve()` raises an uncaught `ValueError`.
    InvalidWorkspace(String),
    /// The store loader raised; the string is the exception line.
    Store(String),
}

impl QueryError {
    /// The line the CLI prints.
    pub fn cli_line(&self) -> String {
        match self {
            QueryError::InvalidWorkspace(message) => format!("ValueError: {message}"),
            QueryError::Store(line) => line.clone(),
        }
    }

    /// Python's `str(e)`: the message without the exception class name.
    pub fn message(&self) -> String {
        match self {
            QueryError::InvalidWorkspace(message) => message.clone(),
            QueryError::Store(line) => {
                for prefix in [
                    "OSError: ",
                    "AttributeError: ",
                    "ValueError: ",
                    "TypeError: ",
                    "KeyError: ",
                ] {
                    if let Some(rest) = line.strip_prefix(prefix) {
                        return rest.to_string();
                    }
                }
                line.clone()
            }
        }
    }
}

/// `query_nuggets(text, workspace, k)` — the substring path.
///
/// The home directory comes from the environment (`XPST_HOME`, else `~/.xpst`),
/// exactly as `Workspace.resolve` does.
pub fn query_payload(workspace: &str, text: &str, k: i64) -> Result<Value, QueryError> {
    let home = xpst_home().ok_or_else(|| {
        QueryError::Store(
            "cannot determine the xPST home: neither XPST_HOME nor HOME is set".to_string(),
        )
    })?;
    query_payload_in(&home, workspace, text, k)
}

/// [`query_payload`] against an explicit home directory.
///
/// The seam exists so a test can drive a seeded store without mutating the
/// process environment (which would race every other test in the binary).
pub fn query_payload_in(
    home: &Path,
    workspace: &str,
    text: &str,
    k: i64,
) -> Result<Value, QueryError> {
    let root = match resolve_workspace_in(home, workspace, false) {
        Ok(root) => root,
        Err(WorkspaceError::Invalid(message)) => return Err(QueryError::InvalidWorkspace(message)),
        Err(WorkspaceError::NoHome) => {
            return Err(QueryError::Store(
                "cannot determine the xPST home: neither XPST_HOME nor HOME is set".to_string(),
            ))
        }
    };
    let store = open_store(&root).map_err(QueryError::Store)?;
    let limit = k.max(1) as usize;
    let needle = text.to_lowercase();
    let hits: Vec<&Nugget> = store
        .nuggets
        .iter()
        .filter(|nugget| nugget.point.to_lowercase().contains(&needle))
        .take(limit)
        .collect();

    let rows: Vec<Value> = hits
        .iter()
        .map(|nugget| {
            object(vec![
                ("point", Value::String(nugget.point.clone())),
                ("citation", citation(nugget)),
                ("source_url", nugget.source_url.clone()),
                ("timestamp_start", nugget.timestamp_start.clone()),
                ("timestamp_end", nugget.timestamp_end.clone()),
                ("score", null()),
                ("area_id", nugget.area_id.clone()),
            ])
        })
        .collect();
    Ok(object(vec![
        ("workspace", string(workspace)),
        ("query", string(text)),
        ("mode", string("substring")),
        ("count", integer(rows.len() as i64)),
        ("nuggets", array(rows)),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seeded_home(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("xpst-kb-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let workspace = dir.join("knowledge").join("default");
        std::fs::create_dir_all(&workspace).expect("workspace");
        std::fs::write(
            workspace.join("nuggets.json"),
            r#"{"n1": {"id": "n1", "point": "Intro to routing", "source_video_id": "vid-a",
                       "timestamp_start": 0.0, "timestamp_end": 12.5,
                       "source_url": "https://example.invalid/a", "difficulty": "beginner",
                       "area_id": "a1"},
                "n2": {"id": "n2", "point": "Advanced concurrency tuning", "source_video_id": "vid-a",
                       "timestamp_start": 30.0, "timestamp_end": 45.0, "source_url": null,
                       "difficulty": "advanced", "area_id": "a1"}}"#,
        )
        .expect("nuggets");
        std::fs::write(workspace.join("areas.json"), "{}").expect("areas");
        dir
    }

    #[test]
    fn a_query_returns_the_python_document() {
        let home = seeded_home("hits");
        let payload = query_payload_in(&home, "default", "ing", 8).expect("query");
        assert_eq!(
            payload.get("workspace").and_then(Value::as_str),
            Some("default")
        );
        assert_eq!(
            payload.get("mode").and_then(Value::as_str),
            Some("substring")
        );
        assert_eq!(payload.get("count").and_then(Value::as_i64), Some(2));
        let nuggets = payload
            .get("nuggets")
            .and_then(Value::as_sequence)
            .expect("nuggets");
        // `citation` is `source_url or source_video_id`.
        assert_eq!(
            nuggets[0].get("citation").and_then(Value::as_str),
            Some("https://example.invalid/a")
        );
        assert_eq!(
            nuggets[1].get("citation").and_then(Value::as_str),
            Some("vid-a")
        );
        // A metric no snapshot reported stays null, never zero.
        assert!(nuggets[0].get("score").is_some_and(Value::is_null));
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn the_limit_is_applied_and_never_zero() {
        let home = seeded_home("limit");
        let one = query_payload_in(&home, "default", "ing", 1).expect("query");
        assert_eq!(one.get("count").and_then(Value::as_i64), Some(1));
        // `max(1, k)`: a zero or negative limit still returns one row.
        let zero = query_payload_in(&home, "default", "ing", 0).expect("query");
        assert_eq!(zero.get("count").and_then(Value::as_i64), Some(1));
        let _ = std::fs::remove_dir_all(&home);
    }

    /// `kb_areas()`: areas in course order, `order` 1-based, with nugget counts.
    #[test]
    fn areas_are_ordered_and_one_based() {
        let home = seeded_home("areas");
        let workspace = home.join("knowledge").join("default");
        std::fs::write(
            workspace.join("areas.json"),
            r#"{
              "a2": {"id": "a2", "label": "Advanced", "centroid": [], "nugget_ids": ["n2"], "order_index": 1},
              "a1": {"id": "a1", "label": "Basics", "centroid": [], "nugget_ids": ["n1", "n3"], "order_index": 0}
            }"#,
        )
        .expect("areas");

        let payload = areas_payload_in(&home, "default").expect("areas payload");
        assert_eq!(
            payload.get("workspace").and_then(Value::as_str),
            Some("default")
        );
        assert_eq!(payload.get("count").and_then(Value::as_i64), Some(2));
        let rows = payload
            .get("areas")
            .and_then(Value::as_sequence)
            .expect("areas");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].get("order").and_then(Value::as_i64), Some(1));
        assert_eq!(rows[0].get("label").and_then(Value::as_str), Some("Basics"));
        assert_eq!(rows[0].get("nugget_count").and_then(Value::as_i64), Some(2));
        assert_eq!(rows[1].get("order").and_then(Value::as_i64), Some(2));
        let _ = std::fs::remove_dir_all(&home);
    }

    /// `Workspace.resolve(workspace)` defaults to `create=True`, so a workspace
    /// that does not exist yet is materialized and answers an empty list.
    #[test]
    fn areas_materialize_a_missing_workspace() {
        let home = std::env::temp_dir().join(format!("xpst-kb-areas-new-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&home);
        std::fs::create_dir_all(&home).expect("home");

        let payload = areas_payload_in(&home, "fresh").expect("areas payload");
        assert_eq!(payload.get("count").and_then(Value::as_i64), Some(0));
        assert!(home.join("knowledge").join("fresh").is_dir());
        let _ = std::fs::remove_dir_all(&home);
    }

    /// `_validate_name`'s three refusals, with Python's sentences.
    #[test]
    fn validate_name_reproduces_pythons_refusals() {
        assert_eq!(
            validate_name("../escape", "video_id").expect_err("traversal"),
            "Invalid video_id: must contain only alphanumeric, dash, underscore, colon, or dot \
             characters (got: '../escape')"
        );
        assert_eq!(
            validate_name("a..b", "video_id").expect_err("parent traversal"),
            "Invalid video_id: parent traversal (..) is not allowed"
        );
        // `~` is not in the safe regex, so a leading tilde fails the FIRST
        // check — Python's `startswith("~")` branch is unreachable for the same
        // reason. Reproduced rather than tidied.
        assert_eq!(
            validate_name("~home", "video_id").expect_err("tilde"),
            "Invalid video_id: must contain only alphanumeric, dash, underscore, colon, or dot \
             characters (got: '~home')"
        );
        assert!(validate_name("content:abc-1.2", "video_id").is_ok());
        // The regex is ASCII-only, so a non-ASCII letter is refused here even
        // though `valid_workspace_name` accepts it.
        assert!(validate_name("ünïcode", "video_id").is_err());
        assert!(valid_workspace_name("ünïcode"));
    }

    #[test]
    fn an_invalid_workspace_is_a_value_error() {
        let home = seeded_home("invalid");
        let error = query_payload_in(&home, "../escape", "x", 8).expect_err("invalid workspace");
        assert!(matches!(error, QueryError::InvalidWorkspace(_)));
        // The CLI prints the exception line; the MCP server serves `str(e)`.
        assert!(error
            .cli_line()
            .starts_with("ValueError: Invalid workspace name"));
        assert!(error.message().starts_with("Invalid workspace name"));
        let _ = std::fs::remove_dir_all(&home);
    }
}
