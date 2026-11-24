//! `xpst security-audit` — the live installation checks.
//!
//! The checks themselves live in `xpst-surface::security`, because the MCP tool
//! `xpst_security_audit` serves the same document and `xpst-mcp` cannot depend
//! on this crate. This module owns only the CLI surface: `--json`, the text
//! rendering and the exit code.

use std::io::Write;

use clap::ArgMatches;

use crate::commands::config_view;
use crate::output::{python_pretty_json_cfg, write_text};
use crate::{exit, flag, GlobalOptions};

/// Run `security-audit`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let view = match config_view::resolve(global) {
        Ok(view) => view,
        Err(error) => return crate::commands::bootstrap::report(err, json_mode, out, &error),
    };

    let mut payload = xpst_surface::security::payload(&view);
    // `cli.py:7906` adds the count on top of `run_security_checks`; the MCP
    // tool serves the raw document, so this is the CLI's own field.
    if let xpst_config::Value::Mapping(map) = &mut payload {
        let count = map
            .get("checks")
            .and_then(xpst_config::Value::as_sequence)
            .map(<[xpst_config::Value]>::len)
            .unwrap_or_default();
        map.insert(
            xpst_config::Value::from("check_count"),
            xpst_config::Value::Number((count as i64).into()),
        );
    }
    if json_mode {
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let _ = write_text(out, &xpst_surface::security::render_text(&payload));
    exit::SUCCESS
}
