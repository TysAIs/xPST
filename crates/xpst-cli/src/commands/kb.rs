//! The knowledge-base read commands: `kb areas`, `kb course`, `kb doctor`,
//! `kb query` and the top-level `search`.
//!
//! Parity targets, in `src/xpst/knowledge/`:
//!
//! * `cli_kb.py::kb_areas` / `organize/difficulty.py::order_areas`
//! * `cli_kb.py::kb_course` / `course/assemble.py::assemble_course`
//! * `cli_kb.py::kb_doctor` / `doctor.py::diagnose`
//! * `cli_kb.py::kb_query` and `cli.py::search` / `query.py::query_nuggets`
//!
//! All five are local reads of the dependency-free JSON store (`nuggets.json`
//! and `areas.json` beside it): no embeddings, no LLM, no network. The store
//! reader lives in [`crate::commands::kb_store`] and reproduces Python's
//! quarantine-on-corruption contract.
//!
//! Two details are load-bearing and shared with `kb areas`:
//!
//! * The workspace root is `<XPST_HOME>/knowledge/<name>`, and `XPST_HOME`
//!   defaults to `~/.xpst` — **not** `XPST_CONFIG_DIR`. A sandboxed profile that
//!   sets only the config dir still reads the operator's real knowledge base in
//!   Python, so the same rule is reproduced here rather than "fixed".
//! * `Workspace.resolve` creates the directory by default, so a read of a
//!   never-used workspace leaves an empty one behind. That side effect is
//!   reproduced for `areas`, `course` and `doctor`; `query` and `search` pass
//!   `create=False` and must not create it.
//!
//! **Divergences, recorded in the ledger note:** text mode renders plain lines
//! instead of Rich markup (`--json` is the parity surface); an invalid
//! `--workspace` reproduces Python's uncaught `ValueError` contract — empty
//! stdout, exit 1 — with the exception line on stderr instead of the traceback
//! frames; `kb query`/`search` always take the substring path because the
//! embedding stack is not ported; and `kb doctor`'s optional-dependency probe
//! asks an interpreter out-of-process (`XPST_PYTHON`, else `python3`, else
//! `python`) because a Rust binary has no `sys.path` of its own.

use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_config::Value;

use crate::commands::config_view::{python_str, truthy};
use crate::commands::kb_store::{self, Area, Nugget};
use crate::output::{
    array, boolean, integer, null, object, python_pretty_json_cfg, string, write_python_json,
    write_text,
};
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

/// Why a workspace could not be resolved.
enum WorkspaceError {
    /// `Workspace.resolve()` raises an uncaught `ValueError`.
    Invalid(String),
    /// Neither `XPST_HOME` nor `HOME` is set.
    NoHome,
}

/// `Workspace.resolve(name, create=...)` — the root directory, not the name.
fn resolve_workspace(name: &str, create: bool) -> Result<PathBuf, WorkspaceError> {
    if !valid_workspace_name(name) {
        return Err(WorkspaceError::Invalid(format!(
            "Invalid workspace name: must contain only alphanumeric, dash, underscore, \
             colon, or dot characters (got: {})",
            crate::output::python_repr(name)
        )));
    }
    let Some(home) = xpst_home() else {
        return Err(WorkspaceError::NoHome);
    };
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

/// The `--json` decision: the global flag (which Click turns on for a piped
/// stdout) or the leaf's own flag.
fn json_flag(global: &GlobalOptions, leaf: Option<&ArgMatches>) -> bool {
    global.json || leaf.is_some_and(|matches| flag(matches, "opt:json"))
}

/// Report a workspace failure the way Python does and return the exit code.
fn workspace_failure(
    error: WorkspaceError,
    json: bool,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    match error {
        WorkspaceError::Invalid(message) => {
            // Python's `Workspace.resolve()` raises an uncaught `ValueError`:
            // the traceback goes to stderr, stdout stays empty and the process
            // exits 1. The frames cannot be reproduced (they name Python files
            // and line numbers), so the last line — the exception itself — is
            // what is printed here, and the exit/stdout contract is the same.
            let _ = writeln!(err, "ValueError: {message}");
            exit::GENERAL
        }
        WorkspaceError::NoHome => {
            let message = "cannot determine the xPST home: neither XPST_HOME nor HOME is set";
            if json {
                let payload = crate::output::error_object("CONFIG_ERROR", message, Vec::new());
                let _ = write_python_json(out, &payload);
            } else {
                let _ = writeln!(err, "{message}");
            }
            exit::CONFIG_ERROR
        }
    }
}

/// Report a store the Python loader raises out of, and return the exit code.
fn store_failure(line: &str, err: &mut dyn Write) -> i32 {
    let _ = writeln!(err, "{line}");
    exit::GENERAL
}

/// The two files `JsonKnowledgeStore.__init__` loads, in its order.
struct Store {
    nuggets: Vec<Nugget>,
    areas: Vec<Area>,
}

fn open_store(root: &Path) -> Result<Store, String> {
    let nuggets = kb_store::load_nuggets(&root.join("nuggets.json"))?;
    let areas = kb_store::load_areas(&root.join("areas.json"))?;
    Ok(Store { nuggets, areas })
}

// ---------------------------------------------------------------------------
// kb areas
// ---------------------------------------------------------------------------

/// Run `kb areas`.
pub fn areas(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_flag(global, leaf);
    let workspace = leaf_string(leaf, "opt:workspace").unwrap_or_else(|| "default".to_string());
    let root = match resolve_workspace(&workspace, true) {
        Ok(root) => root,
        Err(error) => return workspace_failure(error, json, out, err),
    };
    let store = match open_store(&root) {
        Ok(store) => store,
        Err(line) => return store_failure(&line, err),
    };
    let mut areas = store.areas;
    kb_store::order_areas(&mut areas);

    if json {
        let rows: Vec<Value> = areas
            .iter()
            .map(|area| {
                object(vec![
                    ("label", area.label.clone()),
                    ("order_index", area.order_index.clone()),
                    ("nuggets", integer(area.nugget_ids.len() as i64)),
                    // `getattr(area, "difficulty", None)` — `Area` has no such
                    // field, so the payload always carries null.
                    ("difficulty", null()),
                ])
            })
            .collect();
        let payload = object(vec![("areas", array(rows))]);
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
            area.order_index.as_i64().unwrap_or_default() + 1,
            python_str(&area.label),
            area.nugget_ids.len()
        ));
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

// ---------------------------------------------------------------------------
// kb course
// ---------------------------------------------------------------------------

/// `UNASSIGNED_AREA_ID` — the synthetic trailing module.
const UNASSIGNED_AREA_ID: &str = "__unassigned__";
/// `UNASSIGNED_AREA_LABEL`.
const UNASSIGNED_AREA_LABEL: &str = "Unassigned";

/// One assembled course module.
struct AssembledArea<'a> {
    label: Value,
    order: usize,
    nuggets: Vec<&'a Nugget>,
}

/// `assemble_course`: ordered areas, each with its nuggets in beginner→advanced
/// order, plus a trailing "Unassigned" module for nuggets no area claims.
fn assemble_course<'a>(
    nuggets: &'a [Nugget],
    mut areas: Vec<Area>,
    area_id: Option<&str>,
) -> Vec<AssembledArea<'a>> {
    // `{n.id: n for n in nuggets}` — a later duplicate wins.
    let mut by_id: HashMap<&str, &Nugget> = HashMap::new();
    for nugget in nuggets {
        by_id.insert(nugget.id_key.as_str(), nugget);
    }

    kb_store::order_areas(&mut areas);
    if let Some(wanted) = area_id {
        if wanted != UNASSIGNED_AREA_ID {
            areas.retain(|area| area.id.as_str() == Some(wanted));
        }
    }

    let mut assigned: HashSet<&str> = HashSet::new();
    let mut assembled: Vec<AssembledArea<'a>> = Vec::new();
    for (order, area) in areas.iter().enumerate() {
        let mut members: Vec<&Nugget> = Vec::new();
        for candidate in &area.nugget_ids {
            let Some(key) = kb_store::id_key(candidate) else {
                continue;
            };
            if let Some(nugget) = by_id.get(key.as_str()) {
                members.push(nugget);
            }
        }
        for member in &members {
            assigned.insert(member.id_key.as_str());
        }
        kb_store::order_nuggets(&mut members);
        assembled.push(AssembledArea {
            label: area.label.clone(),
            order,
            nuggets: members,
        });
    }

    let want_unassigned = area_id.is_none() || area_id == Some(UNASSIGNED_AREA_ID);
    if want_unassigned {
        let mut leftover: Vec<&Nugget> = nuggets
            .iter()
            .filter(|nugget| !assigned.contains(nugget.id_key.as_str()))
            .collect();
        if !leftover.is_empty() {
            kb_store::order_nuggets(&mut leftover);
            assembled.push(AssembledArea {
                label: Value::String(UNASSIGNED_AREA_LABEL.to_string()),
                order: assembled.len(),
                nuggets: leftover,
            });
        }
    }
    assembled
}

/// `nugget.source_url or nugget.source_video_id`.
fn citation(nugget: &Nugget) -> Value {
    if truthy(&nugget.source_url) {
        nugget.source_url.clone()
    } else {
        nugget.source_video_id.clone()
    }
}

/// `f"{value:.1f}"` for a timestamp.
fn one_decimal(value: &Value) -> String {
    format!("{:.1}", value.as_f64().unwrap_or_default())
}

/// Run `kb course`.
pub fn course(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_flag(global, leaf);
    let workspace = leaf_string(leaf, "opt:workspace").unwrap_or_else(|| "default".to_string());
    let area_id = leaf_string(leaf, "opt:area");
    let root = match resolve_workspace(&workspace, true) {
        Ok(root) => root,
        Err(error) => return workspace_failure(error, json, out, err),
    };
    let store = match open_store(&root) {
        Ok(store) => store,
        Err(line) => return store_failure(&line, err),
    };
    let assembled = assemble_course(&store.nuggets, store.areas, area_id.as_deref());

    if json {
        let rows: Vec<Value> = assembled
            .iter()
            .map(|area| {
                let nuggets: Vec<Value> = area
                    .nuggets
                    .iter()
                    .map(|nugget| {
                        object(vec![
                            ("point", Value::String(nugget.point.clone())),
                            ("difficulty", nugget.difficulty.clone()),
                            ("citation", citation(nugget)),
                            ("timestamp_start", nugget.timestamp_start.clone()),
                            ("timestamp_end", nugget.timestamp_end.clone()),
                        ])
                    })
                    .collect();
                object(vec![
                    ("label", area.label.clone()),
                    ("order", integer(area.order as i64)),
                    ("nuggets", array(nuggets)),
                ])
            })
            .collect();
        let payload = object(vec![
            ("workspace", string(workspace.clone())),
            ("areas", array(rows)),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    if assembled.is_empty() {
        let _ = write_text(
            out,
            "Nothing to assemble. Ingest with 'xpst kb add' and organize with 'xpst kb organize'.",
        );
        return exit::SUCCESS;
    }
    let mut text = String::new();
    for area in &assembled {
        text.push_str(&format!(
            "{}. {} ({} nuggets)\n",
            area.order + 1,
            python_str(&area.label),
            area.nuggets.len()
        ));
        for nugget in &area.nuggets {
            text.push_str(&format!(
                "  - [{}] {}\n    ({} @ {}-{}s)\n",
                python_str(&nugget.difficulty),
                nugget.point,
                python_str(&citation(nugget)),
                one_decimal(&nugget.timestamp_start),
                one_decimal(&nugget.timestamp_end)
            ));
        }
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

// ---------------------------------------------------------------------------
// kb doctor
// ---------------------------------------------------------------------------

const SEVERITY_OK: &str = "ok";
const SEVERITY_WARNING: &str = "warning";
const SEVERITY_ERROR: &str = "error";

/// Heavy optional-extra deps probed (not imported) so the cold path stays intact.
const OPTIONAL_DEPS: [&str; 4] = ["faster_whisper", "fastembed", "lancedb", "sqlite_vec"];

/// `importlib.util.find_spec` for each optional dep, run in the interpreter the
/// Python engine would use.
///
/// A Rust binary has no `sys.path`, so the only truthful answer to "is the
/// knowledge extra installed" is to ask an interpreter. `XPST_PYTHON` is the
/// repo's name for that interpreter; `python3`/`python` are the fallbacks. With
/// no interpreter at all the extra is reported absent, which is also true of
/// this engine.
const DEPENDENCY_PROBE: &str = "\
import importlib.util as u, json
out = []
for m in ('faster_whisper', 'fastembed', 'lancedb', 'sqlite_vec'):
    try:
        ok = u.find_spec(m) is not None
    except (ImportError, ValueError):
        ok = False
    out.append([m, ok])
print(json.dumps(out))
";

fn probe_dependencies() -> Vec<&'static str> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(interpreter) = std::env::var_os("XPST_PYTHON") {
        if !interpreter.is_empty() {
            candidates.push(PathBuf::from(interpreter));
        }
    }
    candidates.push(PathBuf::from("python3"));
    candidates.push(PathBuf::from("python"));
    for interpreter in candidates {
        let Ok(output) = std::process::Command::new(&interpreter)
            .arg("-c")
            .arg(DEPENDENCY_PROBE)
            .output()
        else {
            continue;
        };
        if !output.status.success() {
            continue;
        }
        let Ok(parsed) = serde_json::from_slice::<serde_json::Value>(&output.stdout) else {
            continue;
        };
        let Some(rows) = parsed.as_array() else {
            continue;
        };
        let mut present = Vec::new();
        for row in rows {
            let name = row.get(0).and_then(|value| value.as_str());
            let installed = row.get(1).and_then(|value| value.as_bool());
            if installed == Some(true) {
                if let Some(known) =
                    name.and_then(|name| OPTIONAL_DEPS.iter().find(|dep| **dep == name))
                {
                    present.push(*known);
                }
            }
        }
        return present;
    }
    Vec::new()
}

/// One `kb doctor` finding.
struct Finding {
    check: &'static str,
    severity: &'static str,
    message: String,
}

fn check_dependencies() -> Finding {
    let present = probe_dependencies();
    let missing: Vec<&str> = OPTIONAL_DEPS
        .iter()
        .copied()
        .filter(|dep| !present.contains(dep))
        .collect();
    if missing.is_empty() {
        Finding {
            check: "dependencies",
            severity: SEVERITY_OK,
            message: "knowledge extra fully installed".to_string(),
        }
    } else {
        Finding {
            check: "dependencies",
            severity: SEVERITY_WARNING,
            message: format!(
                "knowledge extra not fully installed; missing {}. Ingestion needs \
                 'pip install xpst[knowledge]'. Querying organized data works without it.",
                missing.join(", ")
            ),
        }
    }
}

fn check_store(nuggets: &[Nugget], areas: &[Area]) -> Finding {
    if nuggets.is_empty() {
        Finding {
            check: "store",
            severity: SEVERITY_WARNING,
            message: "store is empty (no nuggets ingested yet)".to_string(),
        }
    } else {
        Finding {
            check: "store",
            severity: SEVERITY_OK,
            message: format!("{} nuggets, {} areas loaded", nuggets.len(), areas.len()),
        }
    }
}

fn check_embeddings(nuggets: &[Nugget]) -> Option<Finding> {
    let mut widths: Vec<usize> = nuggets
        .iter()
        .filter(|nugget| nugget.embedding_len > 0)
        .map(|nugget| nugget.embedding_len)
        .collect();
    widths.sort_unstable();
    widths.dedup();
    let embedded = nuggets
        .iter()
        .filter(|nugget| nugget.embedding_len > 0)
        .count();
    if widths.len() > 1 {
        let rendered: Vec<String> = widths.iter().map(usize::to_string).collect();
        return Some(Finding {
            check: "embeddings",
            severity: SEVERITY_ERROR,
            message: format!(
                "mixed embedding widths [{}] -- the embedding model changed without a \
                 re-embed; centroids/routing are corrupt. Re-embed the workspace.",
                rendered.join(", ")
            ),
        });
    }
    if !nuggets.is_empty() && embedded < nuggets.len() {
        return Some(Finding {
            check: "embeddings",
            severity: SEVERITY_WARNING,
            message: format!(
                "{} of {} nuggets have no embedding and cannot be routed/recalled",
                nuggets.len() - embedded,
                nuggets.len()
            ),
        });
    }
    if !nuggets.is_empty() {
        let width = widths.first().copied().unwrap_or(0);
        return Some(Finding {
            check: "embeddings",
            severity: SEVERITY_OK,
            message: format!("all {embedded} nuggets embedded at width {width}"),
        });
    }
    None
}

fn check_orphans(nuggets: &[Nugget], areas: &[Area]) -> Vec<Finding> {
    let mut findings = Vec::new();
    let nugget_ids: HashSet<&str> = nuggets
        .iter()
        .map(|nugget| nugget.id_key.as_str())
        .collect();
    let area_ids: HashSet<&str> = areas.iter().map(|area| area.id_key.as_str()).collect();

    let empty_areas: Vec<&Area> = areas
        .iter()
        .filter(|area| area.nugget_ids.is_empty())
        .collect();
    if !empty_areas.is_empty() {
        findings.push(Finding {
            check: "orphaned_areas",
            severity: SEVERITY_WARNING,
            message: format!(
                "{} area(s) have no members ({})",
                empty_areas.len(),
                empty_areas
                    .iter()
                    .map(|area| python_str(&area.label))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        });
    }

    let dangling = areas
        .iter()
        .flat_map(|area| area.nugget_ids.iter())
        .filter(|candidate| {
            kb_store::id_key(candidate)
                .map(|key| !nugget_ids.contains(key.as_str()))
                .unwrap_or(true)
        })
        .count();
    if dangling > 0 {
        findings.push(Finding {
            check: "orphaned_areas",
            severity: SEVERITY_ERROR,
            message: format!(
                "{dangling} area membership reference(s) point at nuggets that no longer \
                 exist; re-run 'kb organize'"
            ),
        });
    }

    let orphan_nuggets = nuggets
        .iter()
        .filter(|nugget| {
            !nugget.area_id.is_null()
                && kb_store::id_key(&nugget.area_id)
                    .map(|key| !area_ids.contains(key.as_str()))
                    .unwrap_or(true)
        })
        .count();
    if orphan_nuggets > 0 {
        findings.push(Finding {
            check: "orphaned_nuggets",
            severity: SEVERITY_ERROR,
            message: format!(
                "{orphan_nuggets} nugget(s) reference a non-existent area; re-run \
                 'kb organize' to reconcile"
            ),
        });
    }

    if empty_areas.is_empty() && dangling == 0 && orphan_nuggets == 0 {
        findings.push(Finding {
            check: "orphans",
            severity: SEVERITY_OK,
            message: "no orphaned areas or nuggets".to_string(),
        });
    }
    findings
}

/// One `QueueItem`, as `kb doctor` reads it.
#[derive(Debug)]
struct QueueItem {
    source: Value,
    status: String,
    reason: Value,
    enqueued_at: Value,
    seq: Value,
    id: String,
}

const QUEUE_FIELDS: &[&str] = &[
    "id",
    "source",
    "status",
    "workspace",
    "attempts",
    "reason",
    "enqueued_at",
    "updated_at",
    "seq",
    "source_platform",
    "source_post_id",
];

const QUEUE_STATES: [&str; 4] = ["pending", "in_progress", "done", "failed"];

/// One field of a decoded queue item, in the insertion-ordered value type.
fn json_field(record: &serde_json::Map<String, serde_json::Value>, name: &str) -> Value {
    record
        .get(name)
        .map(crate::output::from_json)
        .unwrap_or(Value::Null)
}

/// `IngestionQueue(path, readonly=True)`.
///
/// A corrupt or unreadable queue file starts empty (Python catches
/// `JSONDecodeError`/`OSError`/`UnicodeDecodeError`); an item the dataclass
/// refuses to construct escapes as a `TypeError`, which is reproduced.
fn load_queue(path: &Path) -> Result<Vec<QueueItem>, String> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(_) => return Ok(Vec::new()),
    };
    let parsed: serde_json::Value = match serde_json::from_str(&text) {
        Ok(value) => value,
        Err(_) => return Ok(Vec::new()),
    };
    let Some(document) = parsed.as_object() else {
        return Ok(Vec::new());
    };
    let Some(items) = document.get("items").and_then(|value| value.as_array()) else {
        return Ok(Vec::new());
    };
    let mut queue = Vec::new();
    for raw in items {
        let Some(record) = raw.as_object() else {
            continue;
        };
        for key in record.keys() {
            if !QUEUE_FIELDS.contains(&key.as_str()) {
                return Err(format!(
                    "TypeError: QueueItem.__init__() got an unexpected keyword argument '{key}'"
                ));
            }
        }
        for required in ["id", "source"] {
            if !record.contains_key(required) {
                return Err(format!(
                    "TypeError: QueueItem.__init__() missing required argument: '{required}'"
                ));
            }
        }
        let id = record
            .get("id")
            .and_then(|value| value.as_str())
            .unwrap_or_default()
            .to_string();
        let status = record
            .get("status")
            .and_then(|value| value.as_str())
            .filter(|status| QUEUE_STATES.contains(status))
            .unwrap_or("pending")
            .to_string();
        queue.push(QueueItem {
            source: json_field(record, "source"),
            status,
            reason: json_field(record, "reason"),
            enqueued_at: json_field(record, "enqueued_at"),
            seq: json_field(record, "seq"),
            id,
        });
    }
    // `_ordered()`: (enqueued_at, seq, id).
    queue.sort_by(|left, right| {
        left.enqueued_at
            .as_f64()
            .unwrap_or_default()
            .total_cmp(&right.enqueued_at.as_f64().unwrap_or_default())
            .then_with(|| {
                left.seq
                    .as_f64()
                    .unwrap_or_default()
                    .total_cmp(&right.seq.as_f64().unwrap_or_default())
            })
            .then_with(|| left.id.cmp(&right.id))
    });
    Ok(queue)
}

fn check_queue(queue: &[QueueItem]) -> Vec<Finding> {
    let mut findings = Vec::new();
    let failed: Vec<&QueueItem> = queue
        .iter()
        .filter(|item| item.status == "failed")
        .collect();
    if !failed.is_empty() {
        let sample = failed
            .iter()
            .take(3)
            .map(|item| {
                format!(
                    "{} ({})",
                    python_str(&item.source),
                    python_str(&item.reason)
                )
            })
            .collect::<Vec<_>>()
            .join("; ");
        findings.push(Finding {
            check: "queue",
            severity: SEVERITY_WARNING,
            message: format!("{} failed queue item(s): {sample}", failed.len()),
        });
    }
    let count = |status: &str| queue.iter().filter(|item| item.status == status).count();
    findings.push(Finding {
        check: "queue",
        severity: SEVERITY_OK,
        message: format!(
            "pending={} in_progress={} done={} failed={}",
            count("pending"),
            count("in_progress"),
            count("done"),
            count("failed")
        ),
    });
    findings
}

/// `Manifest.source_count()`: `len(data["sources"])`.
///
/// A corrupt or non-object manifest recovers to empty (Python catches the
/// decode errors); a `sources` value with no length raises `TypeError`, which
/// escapes.
fn manifest_source_count(path: &Path) -> Result<i64, String> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(_) => return Ok(0),
    };
    let parsed: serde_json::Value = match serde_json::from_str(&text) {
        Ok(value) => value,
        Err(_) => return Ok(0),
    };
    let Some(document) = parsed.as_object() else {
        return Ok(0);
    };
    let Some(sources) = document.get("sources") else {
        return Ok(0);
    };
    match sources {
        serde_json::Value::Object(map) => Ok(map.len() as i64),
        serde_json::Value::Array(items) => Ok(items.len() as i64),
        serde_json::Value::String(text) => Ok(text.chars().count() as i64),
        other => Err(format!(
            "TypeError: object of type '{}' has no len()",
            match other {
                serde_json::Value::Null => "NoneType",
                serde_json::Value::Bool(_) => "bool",
                serde_json::Value::Number(number) if number.is_f64() => "float",
                serde_json::Value::Number(_) => "int",
                _ => "object",
            }
        )),
    }
}

/// Run `kb doctor`.
pub fn doctor(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_flag(global, leaf);
    let workspace = leaf_string(leaf, "opt:workspace").unwrap_or_else(|| "default".to_string());
    let root = match resolve_workspace(&workspace, true) {
        Ok(root) => root,
        Err(error) => return workspace_failure(error, json, out, err),
    };
    let store = match open_store(&root) {
        Ok(store) => store,
        Err(line) => return store_failure(&line, err),
    };

    let mut findings = vec![check_dependencies()];
    findings.push(check_store(&store.nuggets, &store.areas));
    if let Some(finding) = check_embeddings(&store.nuggets) {
        findings.push(finding);
    }
    findings.extend(check_orphans(&store.nuggets, &store.areas));

    let queue = match load_queue(&root.join("queue.json")) {
        Ok(queue) => queue,
        Err(line) => return store_failure(&line, err),
    };
    findings.extend(check_queue(&queue));

    let sources = match manifest_source_count(&root.join("manifest.json")) {
        Ok(count) => count,
        Err(line) => return store_failure(&line, err),
    };
    findings.push(Finding {
        check: "manifest",
        severity: SEVERITY_OK,
        message: format!("{sources} source(s) recorded"),
    });

    let errors = findings
        .iter()
        .filter(|finding| finding.severity == SEVERITY_ERROR)
        .count();
    let warnings = findings
        .iter()
        .filter(|finding| finding.severity == SEVERITY_WARNING)
        .count();
    let ok = errors == 0;

    if json {
        let rows: Vec<Value> = findings
            .iter()
            .map(|finding| {
                object(vec![
                    ("severity", string(finding.severity)),
                    ("check", string(finding.check)),
                    ("message", string(finding.message.clone())),
                ])
            })
            .collect();
        let payload = object(vec![
            ("ok", boolean(ok)),
            ("error_count", integer(errors as i64)),
            ("warning_count", integer(warnings as i64)),
            ("findings", array(rows)),
        ]);
        let _ = write_python_json(out, &payload);
        return if ok { exit::SUCCESS } else { exit::GENERAL };
    }

    let mut text = String::new();
    for finding in &findings {
        text.push_str(&format!(
            "{:<7} {}: {}\n",
            finding.severity.to_uppercase(),
            finding.check,
            finding.message
        ));
    }
    text.push_str(&format!(
        "{} ({errors} errors, {warnings} warnings)\n",
        if ok { "OK" } else { "PROBLEMS FOUND" }
    ));
    let _ = write_text(out, &text);
    if ok {
        exit::SUCCESS
    } else {
        exit::GENERAL
    }
}

// ---------------------------------------------------------------------------
// kb query / search
// ---------------------------------------------------------------------------

/// `query_nuggets(text, workspace, k)` — the substring path.
///
/// Python tries the embedder first and falls back to substring matching when it
/// is unavailable *or* when the store holds no vectors. The Rust engine has no
/// embedding stack, so it always takes the fallback — which is the same answer
/// on a machine without the knowledge extra and on any store that has not been
/// embedded.
fn query_payload(workspace: &str, text: &str, k: i64) -> Result<Value, String> {
    let root = match resolve_workspace(workspace, false) {
        Ok(root) => root,
        Err(WorkspaceError::Invalid(message)) => return Err(format!("ValueError: {message}")),
        Err(WorkspaceError::NoHome) => {
            return Err(
                "cannot determine the xPST home: neither XPST_HOME nor HOME is set".to_string(),
            )
        }
    };
    let store = open_store(&root)?;
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

/// The `--limit` value, defaulting the way Click does.
fn limit_option(leaf: Option<&ArgMatches>, default: i64) -> i64 {
    leaf_string(leaf, "opt:limit")
        .and_then(|text| crate::pythonnum::int(&text))
        .unwrap_or(default)
}

/// Run `kb query`.
pub fn query(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_flag(global, leaf);
    let workspace = leaf_string(leaf, "opt:workspace").unwrap_or_else(|| "default".to_string());
    let text = leaf_string(leaf, "arg:text").unwrap_or_default();
    let limit = limit_option(leaf, 8);
    let payload = match query_payload(&workspace, &text, limit) {
        Ok(payload) => payload,
        Err(line) => return store_failure(&line, err),
    };

    if json {
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let Some(rows) = payload.get("nuggets").and_then(Value::as_sequence) else {
        return exit::SUCCESS;
    };
    if rows.is_empty() {
        let _ = write_text(out, "No matching nuggets.");
        return exit::SUCCESS;
    }
    let mut text_out = String::from("mode: substring\n");
    for row in rows {
        let point = row.get("point").map(python_str).unwrap_or_default();
        let citation = row.get("citation").map(python_str).unwrap_or_default();
        let start = row
            .get("timestamp_start")
            .map(one_decimal)
            .unwrap_or_default();
        let end = row
            .get("timestamp_end")
            .map(one_decimal)
            .unwrap_or_default();
        text_out.push_str(&format!("{point}\n  ({citation} @ {start}-{end}s)\n"));
    }
    let _ = write_text(out, &text_out);
    exit::SUCCESS
}

/// Run `search` — the top-level alias for `kb query` on the `default`
/// workspace, with `json.dumps(indent=2)` output.
pub fn search(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    // `search` reads `as_json` directly, not the group-level auto-JSON, so a
    // piped stdout does not switch it to JSON in the Python engine either.
    let _ = global;
    let json = leaf.is_some_and(|matches| flag(matches, "opt:json"));
    let text = leaf_string(leaf, "arg:query").unwrap_or_default();
    let limit = limit_option(leaf, 10);
    let payload = match query_payload("default", &text, limit) {
        Ok(payload) => payload,
        Err(line) => return store_failure(&line, err),
    };

    if json {
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    // Python's text mode reads `result["hits"]`, a key `query_nuggets` never
    // returns, so it always prints the header and "No results found." even when
    // the payload carries matches. Reproduced rather than fixed: `--json` is the
    // contract, and a "better" text mode would be a second behaviour.
    let mut text_out = format!("Search: '{text}' (substring)\n\n");
    text_out.push_str("No results found.\n");
    let _ = write_text(out, &text_out);
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    fn nugget(id: &str, point: &str, difficulty: &str, start: f64) -> Nugget {
        Nugget {
            id: Value::String(id.to_string()),
            id_key: format!("s:{id}"),
            point: point.to_string(),
            source_video_id: Value::String("video".to_string()),
            source_url: Value::Null,
            timestamp_start: Value::Number(serde_json::Number::from_f64(start).unwrap()),
            timestamp_end: Value::Number(serde_json::Number::from_f64(start + 1.0).unwrap()),
            area_id: Value::Null,
            difficulty: Value::String(difficulty.to_string()),
            embedding_len: 0,
        }
    }

    fn area(id: &str, label: &str, order: i64, members: &[&str]) -> Area {
        Area {
            id: Value::String(id.to_string()),
            id_key: format!("s:{id}"),
            label: Value::String(label.to_string()),
            order_index: Value::Number(serde_json::Number::from(order)),
            nugget_ids: members
                .iter()
                .map(|member| Value::String((*member).to_string()))
                .collect(),
        }
    }

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
    fn course_orders_areas_and_nuggets_and_collects_the_unassigned() {
        let nuggets = vec![
            nugget("n1", "Advanced topic", "advanced", 5.0),
            nugget("n2", "Intro topic", "beginner", 9.0),
            nugget("n3", "Loose topic", "beginner", 1.0),
        ];
        let areas = vec![area("a1", "Alpha", 0, &["n1", "n2"])];
        let assembled = assemble_course(&nuggets, areas, None);
        assert_eq!(assembled.len(), 2);
        assert_eq!(assembled[0].order, 0);
        // beginner before advanced inside the area
        assert_eq!(assembled[0].nuggets[0].point, "Intro topic");
        assert_eq!(assembled[0].nuggets[1].point, "Advanced topic");
        // the unclaimed nugget lands in the trailing module
        assert_eq!(python_str(&assembled[1].label), "Unassigned");
        assert_eq!(assembled[1].order, 1);
        assert_eq!(assembled[1].nuggets.len(), 1);
        assert_eq!(assembled[1].nuggets[0].point, "Loose topic");
    }

    #[test]
    fn a_specific_area_skips_the_unassigned_module() {
        let nuggets = vec![
            nugget("n1", "One", "beginner", 1.0),
            nugget("n2", "Two", "beginner", 2.0),
        ];
        let areas = vec![area("a1", "Alpha", 0, &["n1"])];
        let assembled = assemble_course(&nuggets, areas, Some("a1"));
        assert_eq!(assembled.len(), 1);
        assert_eq!(assembled[0].nuggets.len(), 1);
    }

    #[test]
    fn an_unknown_area_id_assembles_nothing() {
        let nuggets = vec![nugget("n1", "One", "beginner", 1.0)];
        let areas = vec![area("a1", "Alpha", 0, &["n1"])];
        assert!(assemble_course(&nuggets, areas, Some("nope")).is_empty());
    }

    #[test]
    fn the_unassigned_pseudo_area_can_be_requested_by_id() {
        let nuggets = vec![nugget("n1", "One", "beginner", 1.0)];
        let assembled = assemble_course(&nuggets, Vec::new(), Some(UNASSIGNED_AREA_ID));
        assert_eq!(assembled.len(), 1);
        assert_eq!(python_str(&assembled[0].label), "Unassigned");
    }

    #[test]
    fn embeddings_report_mixed_widths_as_an_error() {
        let mut first = nugget("n1", "One", "beginner", 1.0);
        first.embedding_len = 3;
        let mut second = nugget("n2", "Two", "beginner", 2.0);
        second.embedding_len = 4;
        let finding = check_embeddings(&[first, second]).expect("a finding");
        assert_eq!(finding.severity, SEVERITY_ERROR);
        assert!(finding.message.contains("[3, 4]"), "{}", finding.message);
    }

    #[test]
    fn embeddings_report_partial_coverage_as_a_warning() {
        let mut first = nugget("n1", "One", "beginner", 1.0);
        first.embedding_len = 3;
        let second = nugget("n2", "Two", "beginner", 2.0);
        let finding = check_embeddings(&[first, second]).expect("a finding");
        assert_eq!(finding.severity, SEVERITY_WARNING);
        assert!(
            finding.message.starts_with("1 of 2 nuggets"),
            "{}",
            finding.message
        );
    }

    #[test]
    fn an_empty_store_has_no_embeddings_finding() {
        assert!(check_embeddings(&[]).is_none());
    }

    #[test]
    fn orphans_are_reported_for_dangling_members_and_stale_back_pointers() {
        let mut stale = nugget("n1", "One", "beginner", 1.0);
        stale.area_id = Value::String("gone".to_string());
        let areas = vec![area("a1", "Alpha", 0, &["n1", "missing"])];
        let findings = check_orphans(&[stale], &areas);
        assert!(findings
            .iter()
            .any(|f| f.check == "orphaned_areas" && f.severity == SEVERITY_ERROR));
        assert!(findings
            .iter()
            .any(|f| f.check == "orphaned_nuggets" && f.severity == SEVERITY_ERROR));
    }

    #[test]
    fn a_clean_store_reports_no_orphans() {
        let nuggets = vec![nugget("n1", "One", "beginner", 1.0)];
        let areas = vec![area("a1", "Alpha", 0, &["n1"])];
        let findings = check_orphans(&nuggets, &areas);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].check, "orphans");
        assert_eq!(findings[0].severity, SEVERITY_OK);
    }

    #[test]
    fn the_queue_reports_counts_and_a_failed_sample() {
        let queue = vec![
            QueueItem {
                source: Value::String("a".to_string()),
                status: "failed".to_string(),
                reason: Value::String("boom".to_string()),
                enqueued_at: Value::Null,
                seq: Value::Null,
                id: "1".to_string(),
            },
            QueueItem {
                source: Value::String("b".to_string()),
                status: "done".to_string(),
                reason: Value::Null,
                enqueued_at: Value::Null,
                seq: Value::Null,
                id: "2".to_string(),
            },
        ];
        let findings = check_queue(&queue);
        assert_eq!(findings.len(), 2);
        assert_eq!(findings[0].severity, SEVERITY_WARNING);
        assert!(
            findings[0].message.contains("a (boom)"),
            "{}",
            findings[0].message
        );
        assert_eq!(
            findings[1].message,
            "pending=0 in_progress=0 done=1 failed=1"
        );
    }

    #[test]
    fn a_manifest_counts_a_mapping_a_list_and_a_string() {
        let dir = std::env::temp_dir().join(format!("xpst-kb-manifest-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("manifest.json");
        std::fs::write(&path, r#"{"sources": {"a": {}, "b": {}}}"#).unwrap();
        assert_eq!(manifest_source_count(&path).unwrap(), 2);
        std::fs::write(&path, r#"{"sources": [1, 2, 3]}"#).unwrap();
        assert_eq!(manifest_source_count(&path).unwrap(), 3);
        std::fs::write(&path, r#"{"sources": "abc"}"#).unwrap();
        assert_eq!(manifest_source_count(&path).unwrap(), 3);
        std::fs::write(&path, r#"{"sources": 5}"#).unwrap();
        assert!(manifest_source_count(&path)
            .unwrap_err()
            .starts_with("TypeError"));
        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(manifest_source_count(&path).unwrap(), 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_queue_item_with_an_unknown_key_raises_like_python() {
        let dir = std::env::temp_dir().join(format!("xpst-kb-queue-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("queue.json");
        std::fs::write(
            &path,
            r#"{"items": [{"id": "1", "source": "s", "surprise": true}]}"#,
        )
        .unwrap();
        let error = load_queue(&path).unwrap_err();
        assert!(
            error.contains("unexpected keyword argument 'surprise'"),
            "{error}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unknown_queue_status_normalizes_to_pending() {
        let dir = std::env::temp_dir().join(format!("xpst-kb-queue-status-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("queue.json");
        std::fs::write(
            &path,
            r#"{"items": [{"id": "1", "source": "s", "status": "weird"}]}"#,
        )
        .unwrap();
        let queue = load_queue(&path).unwrap();
        assert_eq!(queue[0].status, "pending");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
