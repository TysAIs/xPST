//! `xpst setup` — the canonical, durable setup transaction.
//!
//! Parity target: `setup()` in `src/xpst/cli.py` (lines 447-481),
//! `_setup_transaction_service()`, `_setup_pending_payload()` and
//! `_render_setup_transaction()`.
//!
//! `setup` is the canonical command; `onboard` and `wizard` are compatibility
//! aliases that address the same transaction. The service itself is
//! `xpst_surface::setup_transaction` (the four `xpst_setup_*` MCP tools serve
//! it), so this module is the Click surface plus the two renderers.
//!
//! **Divergence, recorded in the ledger note:** the interactive executor
//! (`run_setup()`, `cli.py:480` → `setup.py:403`, which pip-installs yt-dlp and
//! runs the live connectivity probes) is not ported. `--json` and a non-TTY
//! stdin — the agent contract, and the only path the harness exercises — are
//! byte-identical; an interactive terminal gets the transaction state plus a
//! line naming `python -m xpst setup` for the human actions.
//!
//! The shared helpers live here rather than in `wizard` because `setup` is the
//! canonical command and `wizard` is the alias: the alias imports them, not the
//! other way round.

use std::io::{IsTerminal, Write};
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::{json, Value};
use xpst_surface::setup_transaction::{ResumeRequest, SetupTransactionService};

use crate::output::{write_python_json_value, write_text};
use crate::{exit, flag, GlobalOptions};

/// `xpst setup [--status|--resume|--finish-later|--reset] [--json]`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let show_status = leaf.is_some_and(|m| flag(m, "opt:status"));
    let resume_transaction = leaf.is_some_and(|m| flag(m, "opt:resume"));
    let finish_later = leaf.is_some_and(|m| flag(m, "opt:finish-later"));
    let reset_transaction = leaf.is_some_and(|m| flag(m, "opt:reset"));

    let config_dir = match config_dir(global) {
        Ok(dir) => dir,
        Err(code) => return code,
    };
    let mut service = SetupTransactionService::new(&config_dir);

    let payload = if reset_transaction {
        service.reset_json(None, "setup")
    } else if show_status {
        service.status_json(None, "setup")
    } else if resume_transaction {
        if service.status(None).ok().flatten().is_none() {
            if let Err(error) = service.start(None) {
                return report(err, json_mode, out, error.message());
            }
        }
        service.resume_json(ResumeRequest::default(), "setup")
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
            "setup",
        )
    } else {
        service.start_json(None, "setup")
    };

    let mut payload = match payload {
        Ok(payload) => payload,
        Err(error) => return report(err, json_mode, out, error.message()),
    };

    if json_mode || !stdin_is_interactive() {
        let complete = payload
            .get("completion")
            .and_then(|completion| completion.get("complete"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let has_transaction = payload
            .get("transaction_id")
            .map(|value| !value.is_null())
            .unwrap_or(false);
        if has_transaction && !complete && !reset_transaction && !show_status && !finish_later {
            payload = pending_payload(payload, "setup");
        }

        let _ = write_python_json_value(out, &payload);

        let interactive_required = payload
            .get("error")
            .and_then(|error| error.get("code"))
            .and_then(Value::as_str)
            == Some("INTERACTIVE_REQUIRED");
        return if interactive_required {
            exit::CONFIG_ERROR
        } else {
            exit::SUCCESS
        };
    }

    if reset_transaction || show_status || finish_later {
        render(&payload, "setup", out);
        return exit::SUCCESS;
    }

    // The interactive executor (`run_setup`) is not ported: report the
    // transaction and name the Python command that performs the human actions.
    render(&payload, "setup", out);
    let _ = write_text(
        out,
        "The interactive setup executor is not ported to the Rust engine yet: run \
         `python -m xpst setup` to perform the human approval steps, then re-run this \
         command.\n",
    );
    exit::SUCCESS
}

/// `_setup_pending_payload()`: `ok: false` plus the `INTERACTIVE_REQUIRED` error,
/// without hiding the resumable state.
///
/// Shared with `wizard`/`onboard`, which render the same transaction under their
/// own command name.
pub(crate) fn pending_payload(mut payload: Value, command: &str) -> Value {
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
pub(crate) fn render(payload: &Value, command: &str, out: &mut dyn Write) {
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
pub(crate) fn expanduser(path: &str) -> PathBuf {
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
pub(crate) fn config_dir(global: &GlobalOptions) -> Result<PathBuf, i32> {
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
pub(crate) fn stdin_is_interactive() -> bool {
    std::io::stdin().is_terminal()
}

/// Report a `SetupError` the way the other commands report a failure.
pub(crate) fn report(
    err: &mut dyn Write,
    json_mode: bool,
    out: &mut dyn Write,
    message: &str,
) -> i32 {
    if json_mode {
        let payload =
            crate::output::error_payload_with("SETUP_REQUEST_INVALID", message, json!({}));
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
    fn the_pending_payload_is_the_documented_shape() {
        let payload = pending_payload(json!({"ok": true, "transaction_id": "t"}), "setup");
        assert_eq!(payload["ok"], json!(false));
        assert_eq!(payload["error"]["code"], json!("INTERACTIVE_REQUIRED"));
        assert_eq!(
            payload["error"]["message"],
            json!("setup cannot perform human approval actions in non-TTY JSON mode.")
        );
        assert_eq!(payload["error"]["hint"], json!("xpst setup --resume"));
        // The resumable state survives the error: that is the point of the
        // compatibility payload.
        assert_eq!(payload["transaction_id"], json!("t"));
    }

    #[test]
    fn the_onboard_hint_names_the_dry_run() {
        let payload = pending_payload(json!({}), "onboard");
        assert_eq!(
            payload["error"]["hint"],
            json!("xpst onboard --dry-run --json")
        );
    }
}
