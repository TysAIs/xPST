//! The one list of commands whose behaviour has moved to the Rust engine.
//!
//! `dispatch` consults this table instead of matching on strings, and
//! `docs/technical/rust-parity-ledger.md` is generated from it. That makes the
//! ledger impossible to drift by construction: a command is either in this
//! table (and therefore implemented, with a handler the compiler requires) or
//! it is answered by [`crate::not_implemented`] and the ledger says stub.
//!
//! Adding an entry is the whole act of implementing a command. Adding a *row*
//! to the ledger without an entry does nothing, and `tests/parity_ledger.rs`
//! fails if the doc and this table disagree in either direction.

use std::io::Write;

use clap::ArgMatches;

use crate::{commands, GlobalOptions};

/// A command handler: global options, the leaf's parsed arguments, and the two
/// output streams.
pub type Handler = fn(&GlobalOptions, Option<&ArgMatches>, &mut dyn Write, &mut dyn Write) -> i32;

/// One implemented command.
pub struct ImplementedCommand {
    /// Space-joined command path, as in the parity manifest.
    pub path: &'static str,
    /// The crate that owns the behaviour (the CLI entry is always `xpst-cli`).
    pub owner: &'static str,
    /// What the implementer verified, in one sentence — the ledger's "notes".
    pub note: &'static str,
    pub handler: Handler,
}

/// Every implemented command, in manifest order.
pub const IMPLEMENTED: &[ImplementedCommand] = &[
    implemented(
        "config show",
        "xpst-config",
        "Masked YAML/JSON document; byte-compared with the Python output.",
        config_show,
    ),
    implemented(
        "byo clear",
        "xpst-auth",
        "Deletes the stored app pair and clears the config mirror; payload byte-compared with the Python CLI (the config file is edited in place, not rewritten from the model).",
        byo_clear,
    ),
    implemented(
        "byo status",
        "xpst-auth",
        "Reads the Fernet credential store the Python engine writes (scrypt + AES-128-CBC + HMAC-SHA256) and the config/env fallbacks; payload byte-compared with the Python CLI.",
        byo_status,
    ),
    implemented(
        "capabilities",
        "xpst-provider",
        "Serves the capability document captured from `xpst.content.capability_document()`; the payload is diffed against a live `python -m xpst capabilities --json` by the local-surface check.",
        capabilities,
    ),
    implemented(
        "chat platforms",
        "xpst-provider",
        "Readiness rows from the captured chat read-path registry plus the config channels and the YouTube token check; payload byte-compared with the Python CLI (checked_at masked).",
        chat_platforms,
    ),
    implemented(
        "config export",
        "xpst-config",
        "Writes the masked (or `--raw`) document with a PyYAML-identical emitter; payload and file byte-compared with the Python CLI.",
        config_export,
    ),
    implemented(
        "config fix",
        "xpst-config",
        "Detects the same five defects and reports `fixes_applied`/`details`; payload byte-compared with the Python CLI.",
        config_fix,
    ),
    implemented(
        "config import",
        "xpst-config",
        "Merge/replace with the same deep-merge, diff and validation rules; payload byte-compared with the Python CLI.",
        config_import,
    ),
    implemented(
        "config set",
        "xpst-config",
        "Dotted-key set with Python's bool/int/float/str parsing; payload and file byte-compared with the Python CLI.",
        config_set,
    ),
    implemented(
        "config validate",
        "xpst-config",
        "`{valid, checks}` payload; exit 2 on a blocker.",
        config_validate,
    ),
    implemented(
        "media status",
        "xpst-media",
        "Resolution order (env → PATH → system dirs → fetched) and the `env`/`system`/`fetched`/`missing` vocabulary of `media_binary_status()`; payload byte-compared with the Python CLI.",
        media_status,
    ),
    implemented(
        "messenger status",
        "xpst-provider",
        "Config + credential-presence payload (masked token preview, reply-rule count, comment platforms) read from the encrypted store with the config fallback; payload byte-compared with the Python CLI.",
        messenger_status,
    ),
    implemented(
        "doctor",
        "xpst-cli",
        "Local-only diagnostics; live probes are P1 (the report says so).",
        doctor,
    ),
    implemented(
        "version",
        "xpst-cli",
        "Version and dependency report; `--version` matches Click's shape.",
        version,
    ),
    implemented(
        "status",
        "xpst-state",
        "Statistics payload field-for-field with `get_statistics()` on captured fixtures.",
        status,
    ),
    implemented(
        "schedule list",
        "xpst-state",
        "Entries, pause marker and paused snapshots, as `schedule_list --json` reports them.",
        schedule_list,
    ),
    implemented(
        "schedule pause",
        "xpst-state",
        "Writes the same `schedule.paused.json` marker (0600, `json.dumps(indent=2)`) under the same `.schedule.lock`; payload byte-compared with the Python CLI.",
        schedule_pause,
    ),
    implemented(
        "schedule resume",
        "xpst-state",
        "Removes only the pause marker (snapshots survive) and reports `was_paused`; payload byte-compared with the Python CLI.",
        schedule_resume,
    ),
    implemented(
        "failures list",
        "xpst-state",
        "Dead-letter rows with status/resolved/age_hours, as `failures_list --json` reports them.",
        failures_list,
    ),
];

const fn implemented(
    path: &'static str,
    owner: &'static str,
    note: &'static str,
    handler: Handler,
) -> ImplementedCommand {
    ImplementedCommand {
        path,
        owner,
        note,
        handler,
    }
}

/// Look a command up by its space-joined path.
pub fn lookup(key: &str) -> Option<&'static ImplementedCommand> {
    IMPLEMENTED.iter().find(|command| command.path == key)
}

// Thin shims: `dispatch` only has `&mut dyn Write` handles, and the older
// handlers are generic over `W: Write`. `&mut dyn Write` implements `Write`, so
// the shim costs one reborrow and no allocation.

fn config_show(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::config::show(global, leaf, &mut out_ref, &mut err_ref)
}

fn config_validate(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::config::validate(global, leaf, &mut out_ref, &mut err_ref)
}

fn media_status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::media::status(global, leaf, out, err)
}

fn messenger_status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::messenger::status(global, leaf, out, err)
}

fn byo_clear(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::byo::clear(global, leaf, out, err)
}

fn byo_status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::byo::status(global, leaf, out, err)
}

fn capabilities(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::capabilities::run(global, leaf, out, err)
}

fn chat_platforms(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::chat::platforms(global, leaf, out, err)
}

fn config_export(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::export(global, leaf, out, err)
}

fn config_fix(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::fix(global, leaf, out, err)
}

fn config_import(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::import(global, leaf, out, err)
}

fn config_set(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::config::set(global, leaf, out, err)
}

fn doctor(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::doctor::run(global, leaf, &mut out_ref, &mut err_ref)
}

fn version(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let mut out_ref: &mut dyn Write = out;
    let mut err_ref: &mut dyn Write = err;
    commands::version::run(global, leaf, &mut out_ref, &mut err_ref)
}

fn status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::status::run(global, leaf, out, err)
}

fn schedule_list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::run(global, leaf, out, err)
}

fn schedule_pause(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::pause(global, leaf, out, err)
}

fn schedule_resume(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::schedule::resume(global, leaf, out, err)
}

fn failures_list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    commands::failures::run(global, leaf, out, err)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_entry_names_a_manifest_command() {
        for command in IMPLEMENTED {
            let path: Vec<String> = command.path.split(' ').map(str::to_string).collect();
            assert!(
                crate::parity::command_at(&path).is_some(),
                "`{}` is implemented but not in the parity manifest",
                command.path
            );
        }
    }

    #[test]
    fn paths_are_unique() {
        let mut seen = std::collections::BTreeSet::new();
        for command in IMPLEMENTED {
            assert!(
                seen.insert(command.path),
                "`{}` is listed twice",
                command.path
            );
        }
    }

    #[test]
    fn every_entry_is_fully_described() {
        for command in IMPLEMENTED {
            assert!(
                !command.owner.trim().is_empty(),
                "{} has no owner",
                command.path
            );
            assert!(
                !command.note.trim().is_empty(),
                "{} has no note",
                command.path
            );
        }
    }
}
