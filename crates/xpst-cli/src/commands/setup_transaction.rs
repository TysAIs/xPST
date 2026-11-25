//! The Click surface shared by `setup` and its compatibility aliases.
//!
//! `setup` is canonical; `wizard` and `onboard` address the same durable
//! transaction and own no state of their own. Everything that is identical
//! between them lives here — the flag dispatch, the `INTERACTIVE_REQUIRED`
//! document, the terse human view and the profile resolution — so the alias
//! modules add only what the alias itself owns (`wizard`'s legacy checklist and
//! markdown export, `onboard`'s dry-run plan) instead of a second copy of the
//! transaction rules.
//!
//! Parity target: `_setup_transaction_service()`, `_setup_pending_payload()`
//! and `_render_setup_transaction()` in `src/xpst/cli.py`.

use std::io::{IsTerminal, Write};
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::{json, Value};
use xpst_surface::setup_transaction::{ResumeRequest, SetupTransactionService};

use crate::output::write_text;
use crate::{exit, flag, GlobalOptions};

/// The four mutually exclusive transaction flags, in Click's precedence order.
#[derive(Debug, Default, Clone, Copy)]
pub struct Flags {
    pub show_status: bool,
    pub resume: bool,
    pub finish_later: bool,
    pub reset: bool,
}

impl Flags {
    /// Read the flags off a leaf's matches.
    pub fn read(leaf: Option<&ArgMatches>) -> Self {
        Self {
            show_status: leaf.is_some_and(|m| flag(m, "opt:status")),
            resume: leaf.is_some_and(|m| flag(m, "opt:resume")),
            finish_later: leaf.is_some_and(|m| flag(m, "opt:finish-later")),
            reset: leaf.is_some_and(|m| flag(m, "opt:reset")),
        }
    }
}

/// `if reset: … elif status: … elif resume: … elif finish_later: … else: start`.
///
/// `resume` and `finish_later` start a transaction first when none exists —
/// Python's `if service.status() is None: service.start()`.
///
/// The two flags are independent Click booleans with no mutual exclusion, so
/// both can be passed at once. Python's `elif` chain then gives `--resume`
/// precedence and never reaches the `--finish-later` branch, so the request is
/// a plain resume. Mirroring that here is not cosmetic: `finish_later` is
/// persisted into `setup_transaction.json` (`completion.state` /
/// `completion.reason`), so ORing the flags would write a different state store
/// than the Python engine for the same argv.
pub fn resolve(
    service: &mut SetupTransactionService,
    flags: &Flags,
    alias: &str,
) -> Result<Value, String> {
    if flags.reset {
        return service
            .reset_json(None, alias)
            .map_err(|error| error.message().to_string());
    }
    if flags.show_status {
        return service
            .status_json(None, alias)
            .map_err(|error| error.message().to_string());
    }
    if flags.resume || flags.finish_later {
        if service
            .status(None)
            .map_err(|error| error.message().to_string())?
            .is_none()
        {
            service
                .start(None)
                .map_err(|error| error.message().to_string())?;
        }
        return service
            .resume_json(
                ResumeRequest {
                    // `elif resume_transaction:` comes before `elif finish_later:`,
                    // so a resume request is never a finish-later request.
                    finish_later: flags.finish_later && !flags.resume,
                    ..ResumeRequest::default()
                },
                alias,
            )
            .map_err(|error| error.message().to_string());
    }
    service
        .start_json(None, alias)
        .map_err(|error| error.message().to_string())
}

/// `_setup_pending_payload()`: `ok: false` plus the `INTERACTIVE_REQUIRED`
/// error, without hiding the resumable state.
pub fn pending_payload(mut payload: Value, command: &str) -> Value {
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

/// True when the payload carries the `INTERACTIVE_REQUIRED` document.
pub fn interactive_required(payload: &Value) -> bool {
    payload
        .get("error")
        .and_then(|error| error.get("code"))
        .and_then(Value::as_str)
        == Some("INTERACTIVE_REQUIRED")
}

/// `_render_setup_transaction()` — the terse human view.
pub fn render(payload: &Value, command: &str, out: &mut dyn Write) {
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

/// `_setup_transaction_service()`: `--config`'s parent, else the profile dir.
pub fn config_dir(global: &GlobalOptions) -> Result<PathBuf, i32> {
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
pub fn stdin_is_interactive() -> bool {
    std::io::stdin().is_terminal()
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_pending_payload_is_the_documented_shape() {
        let payload = pending_payload(json!({"ok": true, "transaction_id": "t"}), "setup");
        assert_eq!(payload["ok"], json!(false));
        assert_eq!(payload["error"]["code"], json!("INTERACTIVE_REQUIRED"));
        assert_eq!(payload["error"]["hint"], json!("xpst setup --resume"));
        assert!(interactive_required(&payload));
    }

    #[test]
    fn onboard_gets_the_dry_run_hint() {
        let payload = pending_payload(json!({}), "onboard");
        assert_eq!(
            payload["error"]["hint"],
            json!("xpst onboard --dry-run --json")
        );
    }
}
