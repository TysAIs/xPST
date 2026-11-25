//! `xpst state export|import|backup` — durable copies of `state.json`.
//!
//! Parity targets: `src/xpst/cli.py::state_export`, `state_import` and
//! `state_backup`. `state.json` is the single source of truth for what has been
//! posted, so these three commands are the operator's undo button: export a
//! validated copy, restore one (backing the current file up first), and snapshot
//! with rotation.
//!
//! The commands are deliberately thin — they copy bytes and validate JSON, and
//! never rewrite the document through a model. That is what makes the exported
//! and imported files byte-identical to the source: `shutil.copy2` and
//! `std::fs::copy` both preserve the content and the mode.
//!
//! **Divergences, recorded in the ledger note:**
//!
//! * A corrupt `state.json` makes Python raise `JSONDecodeError` (traceback on
//!   stderr, empty stdout, exit 1). The Rust engine prints one line on stderr,
//!   writes nothing to stdout and exits 1 — the same observable contract without
//!   the traceback.
//! * `state import` without `--yes` prompts with `click.confirm`. The Rust
//!   engine reads one line from stdin and accepts `y`/`yes` (case-insensitive),
//!   matching Click's accepted answers. An explicit `n` is a normal answer —
//!   exit 0, nothing on stderr — and only EOF aborts with `Aborted!` and exit 1,
//!   exactly as Click's `confirm()`/`Abort` split does.

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_config::Value;

use crate::commands::bootstrap;
use crate::output::{error_object, object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_flag, leaf_string, GlobalOptions};

/// `Path(p).expanduser()`: only a leading `~` is expanded, and only to `$HOME`.
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

/// The config directory, materializing the first-run config like every other
/// ported command.
fn config_dir(global: &GlobalOptions) -> Result<PathBuf, bootstrap::BootstrapError> {
    let path = bootstrap::ensure_config(global)?;
    Ok(path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from(".")))
}

fn json_mode(global: &GlobalOptions, leaf: Option<&ArgMatches>) -> bool {
    global.json || leaf.is_some_and(|m| flag(m, "opt:json"))
}

/// `state export OUTPUT`.
pub fn export(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let dir = match config_dir(global) {
        Ok(dir) => dir,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let source = dir.join("state.json");
    if !source.exists() {
        if json {
            let _ = write_python_json(
                out,
                &error_object("NO_STATE_FILE", "No state.json to export.", Vec::new()),
            );
        } else {
            let _ = write_text(out, "No state.json to export.");
        }
        return exit::GENERAL;
    }

    // `json.loads(src.read_text())` — refuse to export a corrupt document.
    let text = match std::fs::read_to_string(&source) {
        Ok(text) => text,
        Err(error) => {
            let _ = writeln!(
                err,
                "xpst state export: cannot read {}: {error}",
                source.display()
            );
            return exit::GENERAL;
        }
    };
    if let Err(error) = serde_json::from_str::<serde_json::Value>(&text) {
        let _ = writeln!(
            err,
            "xpst state export: {} is not valid JSON ({error}); refusing to export a corrupt state file",
            source.display()
        );
        return exit::GENERAL;
    }

    let output = leaf_string(leaf, "arg:output").unwrap_or_default();
    let dest = expanduser(&output);
    if let Some(parent) = dest.parent() {
        if !parent.as_os_str().is_empty() {
            if let Err(error) = std::fs::create_dir_all(parent) {
                let _ = writeln!(
                    err,
                    "xpst state export: cannot create {}: {error}",
                    parent.display()
                );
                return exit::GENERAL;
            }
        }
    }
    if let Err(error) = std::fs::copy(&source, &dest) {
        let _ = writeln!(
            err,
            "xpst state export: cannot write {}: {error}",
            dest.display()
        );
        return exit::GENERAL;
    }

    if json {
        let payload = object(vec![
            ("ok", Value::Bool(true)),
            ("exported", string(dest.display().to_string())),
            ("source", string(source.display().to_string())),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(
            out,
            &format!("Exported {} -> {}", source.display(), dest.display()),
        );
    }
    exit::SUCCESS
}

/// `state import SOURCE [--yes]`.
pub fn import(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let dir = match config_dir(global) {
        Ok(dir) => dir,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let dest = dir.join("state.json");
    let source = expanduser(&leaf_string(leaf, "arg:source").unwrap_or_default());

    // `click.Path(exists=True, dir_okay=False)` validates the argument before
    // the command body runs, so a missing SOURCE is a usage error (exit 2) with
    // Click's own wording — not a read failure.
    if !source.is_file() {
        let _ = writeln!(
            err,
            "Usage: xpst state import [OPTIONS] SOURCE\n\
             Try 'xpst state import --help' for help.\n\n\
             Error: Invalid value for 'SOURCE': File '{}' does not exist.",
            source.display()
        );
        return exit::CONFIG_ERROR;
    }

    let text = match std::fs::read_to_string(&source) {
        Ok(text) => text,
        Err(error) => {
            let _ = writeln!(
                err,
                "xpst state import: cannot read {}: {error}",
                source.display()
            );
            return exit::GENERAL;
        }
    };
    let document: serde_json::Value = match serde_json::from_str(&text) {
        Ok(document) => document,
        Err(error) => {
            let _ = writeln!(
                err,
                "xpst state import: {} is not valid JSON ({error})",
                source.display()
            );
            return exit::GENERAL;
        }
    };
    let posted = document
        .as_object()
        .and_then(|object| object.get("posted_videos"))
        .and_then(serde_json::Value::as_object);
    let Some(posted) = posted else {
        if json {
            let _ = write_python_json(
                out,
                &error_object(
                    "INVALID_STATE_FILE",
                    "Not a valid xPST state file (no posted_videos).",
                    vec![("source", string(source.display().to_string()))],
                ),
            );
        } else {
            let _ = write_text(out, "Not a valid xPST state file (no posted_videos).");
        }
        return exit::GENERAL;
    };

    if !leaf_flag(leaf, "opt:yes") {
        // Click's `confirm()` returns False on an explicit "n" — the command
        // then `return`s, so the exit code is 0 and stderr stays empty. Only an
        // EOF (or a read error) raises `Abort`, which is the "Aborted!" path.
        match confirm(
            &format!("Replace {} with {}?", dest.display(), source.display()),
            out,
            err,
        ) {
            Answer::Yes => {}
            Answer::No => return exit::SUCCESS,
            Answer::Abort => return exit::GENERAL,
        }
    }

    if dest.exists() {
        let stamp = jiff::ZonedDateTime::now()
            .strftime("%Y%m%d-%H%M%S")
            .to_string();
        let backup = dest.with_file_name(format!("state.json.pre-import-{stamp}"));
        if let Err(error) = std::fs::copy(&dest, &backup) {
            let _ = writeln!(
                err,
                "xpst state import: cannot back up {}: {error}",
                dest.display()
            );
            return exit::GENERAL;
        }
        if !json {
            let name = backup
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default();
            let _ = write_text(out, &format!("Current state backed up to {name}"));
        }
    }
    if let Err(error) = std::fs::copy(&source, &dest) {
        let _ = writeln!(
            err,
            "xpst state import: cannot write {}: {error}",
            dest.display()
        );
        return exit::GENERAL;
    }

    if json {
        let payload = object(vec![
            ("ok", Value::Bool(true)),
            ("imported", string(source.display().to_string())),
            ("posted_videos", Value::Number((posted.len() as i64).into())),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(
            out,
            &format!(
                "Imported {} posted-video records. Review with `xpst status` before the next run.",
                posted.len()
            ),
        );
    }
    exit::SUCCESS
}

/// The three outcomes of Click's `confirm()`.
///
/// `No` is a normal answer (the caller returns success and prints nothing);
/// `Abort` is Click's `Abort` exception — "Aborted!" on stderr, exit 1.
enum Answer {
    Yes,
    No,
    Abort,
}

/// `click.confirm`: one line from stdin, `y`/`yes` (any case) means yes.
///
/// Click echoes the prompt to stdout and reads stdin; with no stdin (EOF) it
/// aborts. The prompt text goes through the injected writer so a scripted caller
/// sees the same bytes, and the abort message goes to stderr like Click's.
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

/// `state backup [--keep N]`.
pub fn backup(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let dir = match config_dir(global) {
        Ok(dir) => dir,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let source = dir.join("state.json");
    if !source.exists() {
        if json {
            let _ = write_python_json(
                out,
                &error_object("NO_STATE_FILE", "No state.json to back up.", Vec::new()),
            );
        } else {
            let _ = write_text(out, "No state.json to back up.");
        }
        // Python returns (exit 0) here: nothing to do is not a failure.
        return exit::SUCCESS;
    }

    let backups = dir.join("backups");
    if let Err(error) = std::fs::create_dir_all(&backups) {
        let _ = writeln!(
            err,
            "xpst state backup: cannot create {}: {error}",
            backups.display()
        );
        return exit::GENERAL;
    }
    let stamp = jiff::ZonedDateTime::now()
        .strftime("%Y%m%d-%H%M%S")
        .to_string();
    let dest = backups.join(format!("state-{stamp}.json"));
    if let Err(error) = std::fs::copy(&source, &dest) {
        let _ = writeln!(
            err,
            "xpst state backup: cannot write {}: {error}",
            dest.display()
        );
        return exit::GENERAL;
    }

    let keep = crate::clickargs::leaf_integer(leaf, "opt:keep").unwrap_or(10);
    let mut existing: Vec<PathBuf> = std::fs::read_dir(&backups)
        .map(|entries| {
            entries
                .flatten()
                .map(|entry| entry.path())
                .filter(|path| {
                    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                    name.starts_with("state-") && name.ends_with(".json")
                })
                .collect()
        })
        .unwrap_or_default();
    existing.sort();
    if keep > 0 {
        let excess = existing.len().saturating_sub(keep as usize);
        for old in existing.iter().take(excess) {
            let _ = std::fs::remove_file(old);
        }
    }
    let retained = (existing.len() as i64).min(keep);

    if json {
        let payload = object(vec![
            ("ok", Value::Bool(true)),
            ("backup", string(dest.display().to_string())),
            ("retained", Value::Number(retained.into())),
        ]);
        let _ = write_python_json(out, &payload);
    } else {
        let _ = write_text(
            out,
            &format!("Backed up to {} ({retained} retained)", dest.display()),
        );
    }
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expanduser_only_touches_a_leading_tilde() {
        // SAFETY: single-threaded test process for this variable; the value is
        // restored below.
        let previous = std::env::var_os("HOME");
        std::env::set_var("HOME", "/home/tester");
        assert_eq!(expanduser("~/x.json"), PathBuf::from("/home/tester/x.json"));
        assert_eq!(expanduser("~"), PathBuf::from("/home/tester"));
        assert_eq!(expanduser("a/~/b"), PathBuf::from("a/~/b"));
        assert_eq!(expanduser("~other/x"), PathBuf::from("~other/x"));
        match previous {
            Some(value) => std::env::set_var("HOME", value),
            None => std::env::remove_var("HOME"),
        }
    }
}
