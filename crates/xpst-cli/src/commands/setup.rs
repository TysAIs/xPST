//! `xpst setup` — the canonical, durable setup transaction.
//!
//! Parity target: `setup()` in `src/xpst/cli.py`. `setup` is the canonical
//! command; `wizard` and `onboard` are compatibility aliases that address the
//! same transaction and own no state of their own. The dispatch, the
//! `INTERACTIVE_REQUIRED` document and the human view are shared with `wizard`
//! through [`crate::commands::setup_transaction`]; what is `setup`'s alone is
//! the exit code (`EXIT_CONFIG_ERROR` = 2, where `wizard` uses
//! `EXIT_AUTH_FAILURE` = 3) and the absence of the alias-only extras (the
//! legacy checklist, the platform argument and `--export-md`).
//!
//! **Divergence, recorded in the ledger note:** the interactive executor
//! (`run_setup()`, which prompts and runs the connect flows) is not ported.
//! `--json` and a non-TTY stdin — the agent contract, and the only path the
//! harness exercises — are identical; an interactive terminal gets the
//! transaction state and a line naming the Python command that performs the
//! human actions.

use std::io::Write;

use clap::ArgMatches;
use serde_json::Value;
use xpst_surface::setup_transaction::SetupTransactionService;

use crate::commands::setup_transaction::{
    self, interactive_required, pending_payload, render, Flags,
};
use crate::output::{error_payload_with, write_python_json_value, write_text};
use crate::{exit, flag, GlobalOptions};

/// `xpst setup [--status|--resume|--finish-later|--reset] [--json]`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let flags = Flags::read(leaf);

    let config_dir = match setup_transaction::config_dir(global) {
        Ok(dir) => dir,
        Err(code) => return code,
    };
    let mut service = SetupTransactionService::new(&config_dir);

    let mut payload = match setup_transaction::resolve(&mut service, &flags, "setup") {
        Ok(payload) => payload,
        Err(message) => return report(err, json_mode, out, &message),
    };

    if json_mode || !setup_transaction::stdin_is_interactive() {
        let complete = payload
            .get("completion")
            .and_then(|completion| completion.get("complete"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let has_transaction = payload
            .get("transaction_id")
            .map(|value| !value.is_null())
            .unwrap_or(false);
        if has_transaction && !complete && !flags.reset && !flags.show_status && !flags.finish_later
        {
            payload = pending_payload(payload, "setup");
        }

        let _ = write_python_json_value(out, &payload);

        // `ctx.exit(EXIT_CONFIG_ERROR)` — 2, not `wizard`'s 3.
        return if interactive_required(&payload) {
            exit::CONFIG_ERROR
        } else {
            exit::SUCCESS
        };
    }

    if flags.reset || flags.show_status || flags.finish_later {
        render(&payload, "setup", out);
        return exit::SUCCESS;
    }

    // The interactive executor (`run_setup`) prompts and runs the connect
    // flows; it is not ported. Report the transaction and name the Python
    // command that performs the human actions.
    render(&payload, "setup", out);
    let _ = write_text(
        out,
        "The interactive setup is not ported to the Rust engine yet: run \
         `python -m xpst setup` (or `xpst connect <platform>`) to perform the human \
         approval steps, then re-run this command.",
    );
    exit::SUCCESS
}

/// Report a `SetupError` the way the other commands report a failure.
fn report(err: &mut dyn Write, json_mode: bool, out: &mut dyn Write, message: &str) -> i32 {
    if json_mode {
        let payload = error_payload_with("SETUP_REQUEST_INVALID", message, serde_json::json!({}));
        let _ = write_python_json_value(out, &payload);
    } else {
        let _ = writeln!(err, "{message}");
    }
    exit::GENERAL
}
