//! `xpst disconnect <platform>` — remove a platform's stored credentials and
//! disable it.
//!
//! Parity target: `disconnect()` in `src/xpst/cli.py` and
//! `xpst.connect.disconnect_platform()`. The engine half already lives in
//! `xpst_surface::disconnect` (the `xpst_disconnect` MCP tool serves it), so
//! this module is only the Click surface: the confirmation guard, the payload
//! renderer and the human view.
//!
//! Two things here are the CLI's alone and are not in the MCP tool:
//!
//! * the **destructive-action guard** — a non-interactive or `--json`
//!   invocation without `--yes` answers `CONFIRMATION_REQUIRED` and exits 1
//!   instead of removing anything. Python tests `sys.stdin.isatty()` directly
//!   here (not `stdin_is_interactive()`), so a pipe and a NUL device both count
//!   as non-interactive;
//! * the **human view**, which is plain text with the same facts as Python's
//!   Rich lines (the ledger's standing divergence).
//!
//! `load_config()` runs BEFORE the guard in Python, so a config that will not
//! load exits 2 before the refusal is ever printed. The Rust engine reads the
//! raw document (the same choice `byo` makes) and reaches the guard first; a
//! config that fails validation therefore refuses here and fails at save with
//! Python's `Configuration errors:` sentence. Recorded as a divergence.

use std::io::{BufRead, Write};
use std::path::Path;

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;
use xpst_surface::config_save::SaveRequest;
use xpst_surface::disconnect::disconnect as disconnect_platform;
use xpst_surface::readiness::title_case;

use crate::commands::bootstrap;
use crate::output::{error_payload_with, write_python_json, write_python_json_value, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// The refusal sentence, verbatim from `disconnect()`.
const CONFIRMATION_MESSAGE: &str = "Refusing to disconnect without confirmation. \
Re-run with --yes to confirm credential removal for this platform.";

/// `xpst disconnect <platform> [--yes] [--json]`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let yes = leaf.is_some_and(|m| flag(m, "opt:yes"));
    let platform = leaf_string(leaf, "arg:platform").unwrap_or_default();

    // `config = load_config(...)` — resolved (and, on a first run, written)
    // before the guard, exactly as Python does.
    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let file = match xpst_config::ConfigDocument::read(&config_path) {
        Ok(document) => document.raw().clone(),
        Err(error) => {
            let _ = writeln!(err, "{error}");
            return exit::CONFIG_ERROR;
        }
    };
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));

    // `if not yes and (as_json or not sys.stdin.isatty())`.
    if !yes && (json_mode || !stdin_is_a_tty()) {
        if json_mode {
            let payload = error_payload_with(
                "CONFIRMATION_REQUIRED",
                CONFIRMATION_MESSAGE,
                serde_json::json!({"platform": platform, "hint": "--yes"}),
            );
            let _ = write_python_json_value(out, &payload);
        } else {
            let _ = write_text(out, "Refusing to disconnect without confirmation.");
            let _ = write_text(out, &format!("Re-run with --yes to disconnect {platform}."));
        }
        return exit::GENERAL;
    }

    if !yes {
        // `click.confirm(..., abort=True)`: a `n` raises `Abort` (exit 1), an
        // EOF does the same. There is no "no" path that returns success here.
        match confirm(
            &format!("Disconnect {platform} (remove stored credentials)?"),
            out,
            err,
        ) {
            Answer::Yes => {}
            Answer::No | Answer::Abort => return exit::GENERAL,
        }
    }

    let env = |name: &str| std::env::var(name).ok();
    let home = env("HOME").unwrap_or_default();
    let store =
        |dir: &str, key: &str, value: &str| CredentialStore::new(dir).store(key, value).is_ok();
    let request = SaveRequest {
        config_dir: &config_dir,
        home: Path::new(&home),
        env: &env,
        store_secret: &store,
    };

    let platform_value = YamlValue::from(platform.clone());
    let outcome = match disconnect_platform(&platform_value, Some(&file), &request) {
        Ok(outcome) => outcome,
        Err(message) => {
            if json_mode {
                let payload =
                    error_payload_with("DISCONNECT_FAILED", &message, serde_json::json!({}));
                let _ = write_python_json_value(out, &payload);
            } else {
                let _ = write_text(out, &format!("Disconnect failed: {message}"));
            }
            return exit::GENERAL;
        }
    };

    if json_mode {
        let _ = write_python_json(out, &outcome.to_payload());
        return exit::SUCCESS;
    }

    if !global.quiet {
        if outcome.success {
            let title = title_case(&platform);
            if outcome.removed.is_empty() {
                let _ = write_text(
                    out,
                    &format!("{title} disconnected. No stored credentials found."),
                );
            } else {
                let _ = write_text(
                    out,
                    &format!(
                        "{title} disconnected. Removed: {}",
                        outcome.removed.join(", ")
                    ),
                );
            }
            if outcome.disabled {
                let _ = write_text(out, &format!("accounts.{platform}.enabled → false"));
            }
        } else {
            let _ = write_text(
                out,
                &format!(
                    "Disconnect failed: {}",
                    outcome.error.as_deref().unwrap_or("unknown error")
                ),
            );
            return exit::GENERAL;
        }
    }

    exit::SUCCESS
}

/// `sys.stdin.isatty()` — the exact predicate `disconnect()` uses.
///
/// Deliberately NOT `stdin_is_interactive()`: that helper adds a Windows
/// `GetConsoleMode` check the command does not call, so a NUL device is
/// interactive for the helper and non-interactive here.
fn stdin_is_a_tty() -> bool {
    use std::io::IsTerminal;
    std::io::stdin().is_terminal()
}

/// The two outcomes of `click.confirm(..., abort=True)`.
enum Answer {
    Yes,
    No,
    Abort,
}

/// `click.confirm`: one line from stdin, `y`/`yes` (any case) means yes.
fn confirm(prompt: &str, out: &mut dyn Write, err: &mut dyn Write) -> Answer {
    let _ = write!(out, "{prompt} [y/N]: ");
    let _ = out.flush();
    let mut line = String::new();
    match std::io::stdin().lock().read_line(&mut line) {
        Ok(0) | Err(_) => {
            let _ = writeln!(err, "Aborted!");
            Answer::Abort
        }
        Ok(_) => {
            let answer = line.trim().to_lowercase();
            if answer == "y" || answer == "yes" {
                Answer::Yes
            } else {
                Answer::No
            }
        }
    }
}

/// `{"ok": false, "error": {...}}` — kept for the payload shape tests.
#[cfg(test)]
fn refusal_payload(platform: &str) -> YamlValue {
    use crate::output::{boolean, object, string};
    object(vec![
        ("ok", boolean(false)),
        (
            "error",
            object(vec![
                ("code", string("CONFIRMATION_REQUIRED")),
                ("message", string(CONFIRMATION_MESSAGE)),
                ("platform", string(platform)),
                ("hint", string("--yes")),
            ]),
        ),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_refusal_payload_is_the_documented_shape() {
        let payload = refusal_payload("x");
        assert_eq!(payload.get("ok"), Some(&YamlValue::Bool(false)));
        let error = payload.get("error").expect("error");
        assert_eq!(
            error.get("code").and_then(YamlValue::as_str),
            Some("CONFIRMATION_REQUIRED")
        );
        assert_eq!(error.get("hint").and_then(YamlValue::as_str), Some("--yes"));
    }

    #[test]
    fn the_refusal_sentence_is_pythons() {
        assert_eq!(
            CONFIRMATION_MESSAGE,
            "Refusing to disconnect without confirmation. Re-run with --yes to confirm \
             credential removal for this platform."
        );
    }
}
