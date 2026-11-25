//! `xpst wizard` — the compatibility view of the canonical setup transaction.
//!
//! Parity target: `wizard()` in `src/xpst/cli.py`, `_legacy_wizard_checklist()`,
//! `_setup_pending_payload()`, `_render_setup_transaction()` and
//! `xpst.wizard.export_markdown()`.
//!
//! `wizard` is a deprecated alias of `setup`: durable progress belongs to the
//! setup transaction, which `xpst_surface::setup_transaction` already ports (the
//! four `xpst_setup_*` MCP tools serve it). This module is the Click surface
//! plus the two things only the alias has — the legacy checklist and the
//! markdown guide export.
//!
//! **The guide table is captured, not transcribed.** `PLATFORM_GUIDES` is eight
//! platforms of prose; `xtask/scripts/capture_wizard_guides.py` writes it to
//! `resources/wizard_guides.json` and the exported markdown to
//! `tests/fixtures/wizard_export.md`, so the Rust renderer is held to the
//! shipped bytes instead of to a reading of the renderer.
//!
//! **Divergence, recorded in the ledger note:** the interactive path
//! (`run_wizard()`, which opens a browser and runs live health checks) is not
//! ported. `--json` and a non-TTY stdin — the agent contract, and the only path
//! the harness exercises — are identical; an interactive terminal gets the
//! transaction state and a line naming the Python command that performs the
//! human actions.

use std::io::{IsTerminal, Write};
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::{json, Map, Value};
use xpst_surface::setup_transaction::{ResumeRequest, SetupTransactionService};

use crate::output::{write_python_json_value, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `resources/wizard_guides.json`, captured from `src/xpst/wizard.py`.
const GUIDES: &str = include_str!("../resources/wizard_guides.json");

/// The parsed guide table.
struct Guides {
    order: Vec<String>,
    opt_in: Vec<String>,
    guides: Map<String, Value>,
}

impl Guides {
    fn load() -> Result<Self, String> {
        let document: Value =
            serde_json::from_str(GUIDES).map_err(|error| format!("wizard guides: {error}"))?;
        let order: Vec<String> = document
            .get("guide_platforms")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        let opt_in: Vec<String> = document
            .get("opt_in_platforms")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        let guides = document
            .get("guides")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        if order.is_empty() || guides.is_empty() {
            return Err("wizard guides: the captured table is empty".to_string());
        }
        Ok(Self {
            order,
            opt_in,
            guides,
        })
    }

    fn guide(&self, key: &str) -> Option<&Value> {
        self.guides.get(key)
    }
}

/// `xpst wizard [platform] [--export-md PATH] [--status|--resume|--finish-later|--reset] [--json]`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let export_md = leaf_string(leaf, "opt:export-md");
    let show_status = leaf.is_some_and(|m| flag(m, "opt:status"));
    let resume_transaction = leaf.is_some_and(|m| flag(m, "opt:resume"));
    let finish_later = leaf.is_some_and(|m| flag(m, "opt:finish-later"));
    let reset_transaction = leaf.is_some_and(|m| flag(m, "opt:reset"));

    let guides = match Guides::load() {
        Ok(guides) => guides,
        Err(message) => {
            let _ = writeln!(err, "xpst wizard: {message}");
            return exit::GENERAL;
        }
    };

    if let Some(target) = export_md {
        return export(&guides, &target, json_mode, out, err);
    }

    let config_dir = match config_dir(global) {
        Ok(dir) => dir,
        Err(code) => return code,
    };
    let mut service = SetupTransactionService::new(&config_dir);

    let payload = if reset_transaction {
        service.reset_json(None, "wizard")
    } else if show_status {
        service.status_json(None, "wizard")
    } else if resume_transaction {
        if service.status(None).ok().flatten().is_none() {
            if let Err(error) = service.start(None) {
                return report(err, json_mode, out, error.message());
            }
        }
        service.resume_json(ResumeRequest::default(), "wizard")
    } else if finish_later {
        if service.status(None).ok().flatten().is_none() {
            if let Err(error) = service.start(None) {
                return report(err, json_mode, out, error.message());
            }
        }
        service.resume_json(
            ResumeRequest {
                finish_later: true,
                ..ResumeRequest::default()
            },
            "wizard",
        )
    } else {
        service.start_json(None, "wizard")
    };

    let mut payload = match payload {
        Ok(payload) => payload,
        Err(error) => return report(err, json_mode, out, error.message()),
    };

    if json_mode || !stdin_is_interactive() {
        let completion = payload
            .get("completion")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let complete = completion
            .get("complete")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let next_action = payload
            .get("pending_human_actions")
            .and_then(Value::as_array)
            .and_then(|actions| actions.first())
            .and_then(|action| action.get("label"))
            .cloned()
            .unwrap_or(Value::Null);
        let checklist = legacy_checklist(&guides, &mut service, &config_dir);

        // `payload.update({...})` — existing keys keep their position, new ones
        // are appended in this order.
        if let Some(map) = payload.as_object_mut() {
            map.insert("mode".to_string(), json!("agent"));
            map.insert("interactive".to_string(), json!(false));
            map.insert("all_pass".to_string(), json!(complete));
            map.insert("completed".to_string(), json!(complete));
            map.insert("next_action".to_string(), next_action);
            map.insert("checklist".to_string(), Value::Array(checklist));
        }

        let has_transaction = payload
            .get("transaction_id")
            .map(|value| !value.is_null())
            .unwrap_or(false);
        if has_transaction && !complete && !reset_transaction && !show_status && !finish_later {
            payload = pending_payload(payload, "wizard");
        }

        let _ = write_python_json_value(out, &payload);

        let interactive_required = payload
            .get("error")
            .and_then(|error| error.get("code"))
            .and_then(Value::as_str)
            == Some("INTERACTIVE_REQUIRED");
        return if interactive_required {
            exit::AUTH_FAILURE
        } else {
            exit::SUCCESS
        };
    }

    if reset_transaction || show_status || finish_later {
        render(&payload, "wizard", out);
        return exit::SUCCESS;
    }

    // The interactive executor (`run_wizard`) opens a browser and runs live
    // health checks; it is not ported. Report the transaction and name the
    // Python command that performs the human actions.
    render(&payload, "wizard", out);
    let _ = write_text(
        out,
        "The interactive wizard is not ported to the Rust engine yet: run \
         `python -m xpst wizard` (or `xpst connect <platform>`) to perform the human \
         approval steps, then re-run this command.",
    );
    exit::SUCCESS
}

/// `xpst wizard --export-md PATH`.
fn export(
    guides: &Guides,
    target: &str,
    json_mode: bool,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let path = expanduser(target);
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            if let Err(error) = std::fs::create_dir_all(parent) {
                let _ = writeln!(
                    err,
                    "xpst wizard: cannot create {}: {error}",
                    parent.display()
                );
                return exit::GENERAL;
            }
        }
    }
    let markdown = render_markdown(guides);
    if let Err(error) = std::fs::write(&path, markdown.as_bytes()) {
        let _ = writeln!(err, "xpst wizard: cannot write {}: {error}", path.display());
        return exit::GENERAL;
    }

    if json_mode {
        let payload = json!({
            "exported": path.display().to_string(),
            "alias": {"command": "wizard", "alias_of": "setup", "deprecated": true},
        });
        let _ = write_python_json_value(out, &payload);
    } else {
        let _ = write_text(out, &format!("Guide written to {}", path.display()));
    }
    exit::SUCCESS
}

/// `export_markdown()`: the header, then every guide in `GUIDE_PLATFORMS` order.
fn render_markdown(guides: &Guides) -> String {
    let mut parts: Vec<String> = vec![
        "# xPST First-Run Connection Guide".to_string(),
        String::new(),
        "One-time, per-platform human approval steps. The in-app wizard \
         (`xpst wizard`) shows the same instructions interactively."
            .to_string(),
        String::new(),
    ];
    for key in &guides.order {
        if let Some(guide) = guides.guide(key) {
            parts.push(render_platform_markdown(guide));
            parts.push(String::new());
        }
    }
    parts.join("\n")
}

/// `render_platform_markdown()`.
fn render_platform_markdown(guide: &Value) -> String {
    let title = guide.get("title").and_then(Value::as_str).unwrap_or("");
    let why = guide.get("why").and_then(Value::as_str).unwrap_or("");
    let mut lines = vec![
        format!("## {title}"),
        String::new(),
        why.to_string(),
        String::new(),
    ];
    if let Some(steps) = guide.get("steps").and_then(Value::as_array) {
        for (index, step) in steps.iter().enumerate() {
            lines.push(format!(
                "{}. {}",
                index + 1,
                step.as_str().unwrap_or_default()
            ));
        }
    }
    let docs_url = guide.get("docs_url").and_then(Value::as_str).unwrap_or("");
    if !docs_url.is_empty() {
        lines.push(String::new());
        lines.push(format!("More details: {docs_url}"));
    }
    let mut rendered = lines.join("\n");
    rendered.push('\n');
    rendered
}

/// `_legacy_wizard_checklist()`.
fn legacy_checklist(
    guides: &Guides,
    service: &mut SetupTransactionService,
    config_dir: &Path,
) -> Vec<Value> {
    let legacy_state: Value = std::fs::read_to_string(config_dir.join("wizard_state.json"))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}));

    let active = service
        .status(None)
        .ok()
        .flatten()
        .unwrap_or_else(|| json!({}));

    // `{(step.role, step.capability): step}` — the LAST step for a pair wins,
    // which is what a dict comprehension does.
    let mut steps: Vec<((String, String), Value)> = Vec::new();
    if let Some(items) = active.get("steps").and_then(Value::as_array) {
        for step in items {
            let role = step
                .get("role")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let capability = step
                .get("capability")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let key = (role, capability);
            if let Some(existing) = steps.iter_mut().find(|(candidate, _)| *candidate == key) {
                existing.1 = step.clone();
            } else {
                steps.push((key, step.clone()));
            }
        }
    }
    let find = |role: &str, capability: &str| -> Option<Value> {
        steps
            .iter()
            .find(|((candidate_role, candidate_capability), _)| {
                candidate_role == role && candidate_capability == capability
            })
            .map(|(_, step)| step.clone())
    };

    let mut checklist = Vec::with_capacity(guides.order.len());
    for key in &guides.order {
        let Some(guide) = guides.guide(key) else {
            continue;
        };
        let step = find("source", key).or_else(|| find("video_destination", key));
        let verified = step
            .as_ref()
            .and_then(|step| step.get("state"))
            .and_then(Value::as_str)
            == Some("verified");
        let previous = legacy_state
            .get("platforms")
            .and_then(|platforms| platforms.get(key))
            .cloned()
            .unwrap_or_else(|| json!({}));

        let mut entry = Map::new();
        entry.insert("platform".to_string(), json!(key));
        entry.insert(
            "title".to_string(),
            guide.get("title").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "why".to_string(),
            guide.get("why").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "steps".to_string(),
            guide.get("steps").cloned().unwrap_or_else(|| json!([])),
        );
        entry.insert(
            "docs_url".to_string(),
            guide.get("docs_url").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "opt_in".to_string(),
            json!(guides.opt_in.iter().any(|candidate| candidate == key)),
        );
        entry.insert(
            "health".to_string(),
            json!(if verified { "pass" } else { "fail" }),
        );
        entry.insert(
            "last_wizard_status".to_string(),
            previous.get("status").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "action".to_string(),
            if verified {
                Value::Null
            } else {
                json!(format!("xpst wizard {key}"))
            },
        );
        checklist.push(Value::Object(entry));
    }
    checklist
}

/// `_setup_pending_payload()`: `ok: false` plus the `INTERACTIVE_REQUIRED` error,
/// without hiding the resumable state.
fn pending_payload(mut payload: Value, command: &str) -> Value {
    let hint = if command == "onboard" {
        format!("xpst {command} --dry-run --json")
    } else {
        format!("xpst {command} --resume")
    };
    if let Some(map) = payload.as_object_mut() {
        map.insert("ok".to_string(), json!(false));
        map.insert(
            "error".to_string(),
            json!({
                "code": "INTERACTIVE_REQUIRED",
                "message": format!(
                    "{command} cannot perform human approval actions in non-TTY JSON mode."
                ),
                "action": "Complete the listed human actions, then run the command again or use setup resume.",
                "hint": hint,
            }),
        );
    }
    payload
}

/// `_render_setup_transaction()` — the terse human view.
fn render(payload: &Value, command: &str, out: &mut dyn Write) {
    let transaction_id = payload
        .get("transaction_id")
        .and_then(Value::as_str)
        .unwrap_or("none");
    let completion = payload
        .get("completion")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let state = completion
        .get("state")
        .or_else(|| payload.get("state"))
        .and_then(Value::as_str)
        .unwrap_or("not_started");
    let mut rendered = format!("Setup transaction {transaction_id}\n  State: {state}\n");
    let actions = payload
        .get("pending_human_actions")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let complete = completion
        .get("complete")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if actions > 0 {
        rendered.push_str(&format!("  {actions} human action(s) pending.\n"));
        rendered.push_str(&format!("  Resume with: xpst {command} --resume\n"));
    } else if complete {
        rendered.push_str("  Setup readiness verified.\n");
    }
    let _ = write_text(out, &rendered);
}

/// `Path.expanduser()` for the two forms the engine writes.
fn expanduser(path: &str) -> PathBuf {
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

/// `_setup_transaction_service()`: `--config`'s parent, else the profile dir.
fn config_dir(global: &GlobalOptions) -> Result<PathBuf, i32> {
    if let Some(path) = &global.config_path {
        let expanded = expanduser(path);
        return Ok(expanded
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from(".")));
    }
    match crate::paths::config_dir() {
        Some(dir) => Ok(dir),
        None => Err(exit::CONFIG_ERROR),
    }
}

/// `stdin_is_interactive()` on POSIX: `sys.stdin.isatty()`.
fn stdin_is_interactive() -> bool {
    std::io::stdin().is_terminal()
}

/// Report a `SetupError` the way the other commands report a failure.
fn report(err: &mut dyn Write, json_mode: bool, out: &mut dyn Write, message: &str) -> i32 {
    if json_mode {
        let payload = crate::output::error_payload_with(
            "SETUP_REQUEST_INVALID",
            message,
            serde_json::json!({}),
        );
        let _ = write_python_json_value(out, &payload);
    } else {
        let _ = writeln!(err, "{message}");
    }
    exit::GENERAL
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_captured_table_is_loadable_and_complete() {
        let guides = Guides::load().expect("guides");
        assert_eq!(guides.order.len(), 8, "GUIDE_PLATFORMS count drifted");
        assert_eq!(guides.opt_in, vec!["bluesky".to_string()]);
        for key in &guides.order {
            let guide = guides.guide(key).unwrap_or_else(|| panic!("{key} missing"));
            assert!(
                !guide
                    .get("steps")
                    .and_then(Value::as_array)
                    .unwrap_or(&Vec::new())
                    .is_empty(),
                "{key} has no steps"
            );
        }
    }

    #[test]
    fn the_markdown_export_matches_the_captured_fixture() {
        let guides = Guides::load().expect("guides");
        let rendered = render_markdown(&guides);
        let fixture = include_str!("../../tests/fixtures/wizard_export.md");
        assert_eq!(rendered, fixture);
    }

    #[test]
    fn the_pending_payload_is_the_documented_shape() {
        let payload = pending_payload(json!({"ok": true, "transaction_id": "t"}), "wizard");
        assert_eq!(payload["ok"], json!(false));
        assert_eq!(payload["error"]["code"], json!("INTERACTIVE_REQUIRED"));
        assert_eq!(payload["error"]["hint"], json!("xpst wizard --resume"));
    }
}
