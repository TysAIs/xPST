//! `xpst logs` — the tail of the configured log file.
//!
//! Parity target: `src/xpst/cli.py::logs`. The path comes from
//! `monitoring.log_file`, which the config view has already expanded the way
//! `_expand_paths` does (`~` and `$VARS`), so a profile-relative path resolves
//! inside the profile instead of the operator's real home.
//!
//! The payload is the last 50 lines with trailing whitespace stripped — Python
//! uses `rstrip()`, which strips every trailing whitespace character, not just
//! the newline.

use std::io::Write;

use clap::ArgMatches;

use crate::commands::config_view;
use crate::output::{array, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// How many trailing lines `logs` reports (`lines[-50:]`).
const TAIL: usize = 50;

/// Run `logs`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let view = match config_view::resolve(global) {
        Ok(view) => view,
        Err(error) => return crate::commands::bootstrap::report(out, err, &error),
    };
    let log_file = view
        .section_text("monitoring", "log_file")
        .unwrap_or_default();
    let path = crate::commands::state::expanduser(&log_file);

    if !path.exists() {
        if json {
            let payload = object(vec![
                ("logs", array(Vec::new())),
                ("error", string("No log file found")),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, "No log file found");
        }
        return exit::SUCCESS;
    }

    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) => {
            let _ = writeln!(err, "xpst logs: cannot read {}: {error}", path.display());
            return exit::GENERAL;
        }
    };
    // `readlines()` keeps the newline; `rstrip()` removes it and any other
    // trailing whitespace. A file with no trailing newline still yields its last
    // line, which `split('\n')` gives us as long as we drop the empty tail.
    let mut lines: Vec<&str> = text.split('\n').collect();
    if lines.last() == Some(&"") {
        lines.pop();
    }
    let tail: Vec<&str> = lines.iter().rev().take(TAIL).rev().copied().collect();

    if json {
        let payload = object(vec![(
            "logs",
            array(tail.iter().map(|line| string(line.trim_end())).collect()),
        )]);
        let _ = write_python_json(out, &payload);
    } else {
        let mut rendered = String::new();
        for line in &tail {
            rendered.push_str(line.trim_end());
            rendered.push('\n');
        }
        let _ = write_text(out, &rendered);
    }
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_tail_keeps_the_last_fifty_lines() {
        let text = (0..60).map(|i| format!("line {i}\n")).collect::<String>();
        let mut lines: Vec<&str> = text.split('\n').collect();
        if lines.last() == Some(&"") {
            lines.pop();
        }
        let tail: Vec<&str> = lines.iter().rev().take(TAIL).rev().copied().collect();
        assert_eq!(tail.len(), 50);
        assert_eq!(tail[0], "line 10");
        assert_eq!(tail[49], "line 59");
    }

    #[test]
    fn trailing_whitespace_is_stripped_like_rstrip() {
        assert_eq!("abc  \t".trim_end(), "abc");
        assert_eq!("abc".trim_end(), "abc");
    }
}
