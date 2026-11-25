//! xPST Rust engine — P0 scaffold.
//!
//! This build ships the command tree and the local, offline read surface
//! (`config show`, `config validate`, `doctor`, `version`, `status`,
//! `schedule list`, `failures list`). It never posts and never opens a socket.
//! Every command that has no Rust implementation yet exits non-zero with an
//! explicit `not implemented in the Rust engine yet` message, so a script
//! pointed at this binary can tell "the Rust engine cannot do this" apart from
//! "the operation failed".
//!
//! The one write any command performs is `status`'s first-run `config.yaml`,
//! reproduced from the Python loader so a fresh profile behaves identically;
//! see `commands::bootstrap`.
//!
//! `docs/technical/rust-parity-ledger.md` records the status of every command,
//! generated from [`implemented::IMPLEMENTED`] — the same table `dispatch`
//! reads.
//!
//! See `docs/PROPOSAL-RUST-REBUILD.md` §5 (P0) for the phase contract and
//! `docs/DIRECTION.md` D-3/D-4 for why the rebuild exists.

pub mod cli;
pub mod commands;
pub mod exit;
pub mod implemented;
pub mod ledger;
pub mod output;
pub mod parity;
pub mod paths;
pub mod rich;

use std::ffi::OsString;
use std::io::{IsTerminal, Write};

use clap::error::ErrorKind;
use clap::ArgMatches;

/// Options shared by every command, mirroring the Click root group.
#[derive(Debug, Clone)]
pub struct GlobalOptions {
    /// `--json`, or automatically on when stdout is not a TTY — the Python
    /// engine does the same (`cli.py`: `ctx.obj["json"] = json_output or not
    /// _stdout_is_tty()`), and scripts depend on it.
    pub json: bool,
    /// `--config` from the root group.
    pub config_path: Option<String>,
    pub verbose: bool,
    pub quiet: bool,
}

impl GlobalOptions {
    fn from_matches(matches: &ArgMatches, stdout_is_tty: bool) -> Self {
        Self {
            json: flag(matches, "opt:json") || !stdout_is_tty,
            config_path: string_value(matches, "opt:config"),
            verbose: flag(matches, "opt:verbose"),
            quiet: flag(matches, "opt:quiet"),
        }
    }
}

/// Entry point used by `main`.
pub fn run(args: Vec<OsString>) -> i32 {
    let stdout_is_tty = std::io::stdout().is_terminal();
    let mut out = std::io::stdout();
    let mut err = std::io::stderr();
    run_with_io(args, &mut out, &mut err, stdout_is_tty)
}

/// Testable entry point: all output goes through the injected writers.
pub fn run_with_io<W: Write, E: Write>(
    args: Vec<OsString>,
    out: &mut W,
    err: &mut E,
    stdout_is_tty: bool,
) -> i32 {
    let command = cli::build();
    let matches = match command.try_get_matches_from(args) {
        Ok(matches) => matches,
        Err(error) => {
            let code = match error.kind() {
                ErrorKind::DisplayHelp | ErrorKind::DisplayVersion => exit::SUCCESS,
                _ => exit::CONFIG_ERROR,
            };
            // Help and version go to stdout; usage errors to stderr — the same
            // split Click uses.
            if code == exit::SUCCESS {
                let _ = writeln!(out, "{error}");
            } else {
                let _ = writeln!(err, "{error}");
            }
            return code;
        }
    };

    if flag(&matches, &cli::option_id("--version")) {
        let _ = writeln!(out, "xPST, version {}", env!("CARGO_PKG_VERSION"));
        return exit::SUCCESS;
    }

    let path = resolve_path(&matches);
    if path.is_empty() {
        // A bare group invocation prints usage and exits 2, like Click.
        let _ = writeln!(err, "{}", cli::build().render_help());
        return exit::CONFIG_ERROR;
    }

    let global = GlobalOptions::from_matches(&matches, stdout_is_tty);
    let leaf = leaf_matches(&matches, &path);
    dispatch(&path, &global, leaf, out, err)
}

fn dispatch<W: Write, E: Write>(
    path: &[String],
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut W,
    err: &mut E,
) -> i32 {
    let key = path.join(" ");
    if let Some(command) = implemented::lookup(&key) {
        let mut out_ref: &mut dyn Write = out;
        let mut err_ref: &mut dyn Write = err;
        return (command.handler)(global, leaf, &mut out_ref, &mut err_ref);
    }
    not_implemented(&key, global, leaf, out, err)
}

/// The documented P0 answer for a command whose behavior has not moved yet.
fn not_implemented<W: Write, E: Write>(
    command: &str,
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut W,
    err: &mut E,
) -> i32 {
    let json = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let message = "not implemented in the Rust engine yet";
    if json {
        let payload = serde_json::json!({
            "ok": false,
            "error": {
                "code": "NOT_IMPLEMENTED",
                "message": message,
                "command": command,
                "engine": "rust",
                "phase": "P0",
            }
        });
        let _ = writeln!(out, "{payload}");
    } else {
        let _ = writeln!(err, "xpst {command}: {message}");
        let _ = writeln!(
            err,
            "  This build is the P0 scaffold: read paths and the command tree only."
        );
        let _ = writeln!(
            err,
            "  The Python engine is still the shipped surface — use `xpst` from the \
             Python install for this command."
        );
    }
    exit::NOT_IMPLEMENTED
}

/// Resolve the subcommand chain from parsed matches.
pub fn resolve_path(matches: &ArgMatches) -> Vec<String> {
    let mut path = Vec::new();
    let mut current = matches;
    while let Some((name, sub)) = current.subcommand() {
        path.push(name.to_string());
        current = sub;
    }
    path
}

fn leaf_matches<'a>(matches: &'a ArgMatches, path: &[String]) -> Option<&'a ArgMatches> {
    let mut current = matches;
    for segment in path {
        let (name, sub) = current.subcommand()?;
        if name != segment {
            return None;
        }
        current = sub;
    }
    Some(current)
}

/// Read a boolean flag, tolerating its absence (a command that does not declare
/// `--json` must not panic when the caller asks about it).
///
/// The id is `opt:<long>` — the flag as the user types it (see
/// [`cli::option_id`]). Passing a Click parameter name (`as_json`,
/// `config_file`) reads as absent, which is how flags came to be silently
/// ignored; `tests/argument_ids.rs` rejects such a literal.
pub fn flag(matches: &ArgMatches, id: &str) -> bool {
    if matches.try_contains_id(id).unwrap_or(false) {
        matches.get_flag(id)
    } else {
        false
    }
}

/// Effective value of a boolean flag, including Click's on-by-default flags.
///
/// The manifest records two flags that default to on (`config import --merge`,
/// `dashboard --api-only`) together with their OFF forms. Clap's `SetTrue`
/// cannot express "true unless the paired off-flag is present", so the default
/// is resolved here, from the same manifest that drives the tree.
///
/// The OFF form wins when both are given. Click uses last-wins ordering; no P0
/// command can observe the difference because both such commands are
/// unimplemented, and the behavior is pinned by a test.
pub fn flag_with_default(matches: &ArgMatches, path: &[String], name: &str) -> bool {
    let Some(param) = parity::command_at(path)
        .and_then(|command| command.params.iter().find(|param| param.name == name))
    else {
        // A caller asking about a flag its command does not declare gets
        // `false`, exactly like `flag`.
        return false;
    };
    let longs = param.long_opts();
    // Click's OFF form carries its own id (`--replace` is `off:replace`).
    if param.is_flag {
        if let Some(off) = longs.get(1) {
            if flag(matches, &cli::negated_option_id(off)) {
                return false;
            }
        }
    }
    if let Some(on) = longs.first() {
        if flag(matches, &cli::option_id(on)) {
            return true;
        }
    }
    param
        .default
        .as_ref()
        .and_then(|default| default.as_bool())
        .unwrap_or(false)
}

/// Read a single string value by argument id.
pub fn string_value(matches: &ArgMatches, id: &str) -> Option<String> {
    if matches.try_contains_id(id).unwrap_or(false) {
        matches.get_one::<String>(id).cloned()
    } else {
        None
    }
}
