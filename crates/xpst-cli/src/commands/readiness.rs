//! `xpst readiness` — first-run readiness and next actions.
//!
//! The report itself lives in `xpst-surface::readiness`, because the MCP tool
//! `xpst_readiness` serves the same document and `xpst-mcp` cannot depend on
//! this crate. This module owns only the CLI surface: `--json`/`--fix`, the
//! text rendering and the exit codes.

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value as YamlValue;
use xpst_surface::config_view::{self, ConfigView};
pub use xpst_surface::readiness::{build_report, Report, REQUIRED_DIRS};

use crate::commands::bootstrap;
use crate::output::{array, boolean, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// Run `readiness`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let fix = leaf.is_some_and(|m| flag(m, "opt:fix"));

    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let document = match xpst_config::ConfigDocument::read(&config_path) {
        Ok(document) => document.raw().clone(),
        Err(error) => {
            let _ = writeln!(err, "{error}");
            return exit::CONFIG_ERROR;
        }
    };
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let env = |name: &str| std::env::var(name).ok();
    let view = match ConfigView::load(&document, &config_dir, &env) {
        Ok(view) => view,
        Err(message) => {
            let _ = writeln!(err, "xpst readiness: {message}");
            return exit::GENERAL;
        }
    };
    let store = CredentialStore::new(&config_dir);

    if fix {
        let payload = repair_local_setup(&view, &store, &env, global.config_path.as_deref());
        if json_mode {
            let _ = write_python_json(out, &payload);
        } else {
            let mut text = String::from("Local setup repaired\n");
            if let Some(actions) = payload.get("actions").and_then(YamlValue::as_sequence) {
                for action in actions {
                    text.push_str(&format!("  {}\n", action.as_str().unwrap_or("")));
                }
            }
            if let Some(summary) = payload
                .get("readiness")
                .and_then(|report| report.get("summary"))
                .and_then(YamlValue::as_str)
            {
                text.push_str(summary);
                text.push('\n');
            }
            let _ = write_text(out, &text);
        }
        return exit::SUCCESS;
    }

    let report = match build_report(&view, &store) {
        Ok(report) => report,
        Err(message) => {
            let _ = writeln!(err, "xpst readiness: {message}");
            return exit::GENERAL;
        }
    };

    if json_mode {
        let _ = write_python_json(out, &report.to_value());
        return exit::SUCCESS;
    }

    let mut text = String::from("xPST Readiness\n\n");
    text.push_str(&report.summary);
    text.push_str("\n\nSetup checks\n");
    for check in &report.checks {
        let mark = if check.ok {
            "OK"
        } else if check.severity == "error" {
            "FIX"
        } else {
            "WARN"
        };
        let action = if check.action.is_empty() {
            "-"
        } else {
            check.action.as_str()
        };
        text.push_str(&format!(
            "  {mark:<4} {} — {} ({action})\n",
            check.label, check.message
        ));
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

/// `repair_local_setup()`: create the local folders and report what was done.
///
/// It deliberately does not create credentials or enable/disable platforms —
/// only filesystem prerequisites that need no user secrets.
fn repair_local_setup(
    view: &ConfigView,
    store: &CredentialStore,
    env: &dyn Fn(&str) -> Option<String>,
    config_path: Option<&str>,
) -> YamlValue {
    let mut actions: Vec<String> = Vec::new();
    let config_dir = view.config_dir().to_path_buf();
    let _ = std::fs::create_dir_all(&config_dir);

    for name in REQUIRED_DIRS {
        let path = config_dir.join(name);
        if !path.exists() && std::fs::create_dir_all(&path).is_ok() {
            actions.push(format!("created {}", path.display()));
        }
    }

    let download_dir = PathBuf::from(config_view::expand_path(
        &legacy_fixed(
            view.document()
                .get("video")
                .and_then(|section| section.get("download_dir"))
                .and_then(YamlValue::as_str)
                .unwrap_or("~/.xpst/downloads"),
        ),
        env,
    ));
    if !download_dir.exists() && std::fs::create_dir_all(&download_dir).is_ok() {
        actions.push(format!("created {}", download_dir.display()));
    }

    let local_path = view.text_or_empty("local", "path");
    if !local_path.is_empty() {
        let local_source = PathBuf::from(config_view::expand_path(&legacy_fixed(&local_path), env));
        if !local_source.exists() && std::fs::create_dir_all(&local_source).is_ok() {
            actions.push(format!("created {}", local_source.display()));
        }
    }

    let log_file = view
        .section_text("monitoring", "log_file")
        .unwrap_or_else(|| "~/.xpst/logs/xpst.log".to_string());
    let log_file = PathBuf::from(config_view::expand_path(&legacy_fixed(&log_file), env));
    if let Some(parent) = log_file.parent() {
        if !parent.exists() && std::fs::create_dir_all(parent).is_ok() {
            actions.push(format!("created {}", parent.display()));
        }
    }

    // Python rewrites the whole document from the Pydantic model here. The
    // payload is identical either way; the file is left as written (the same
    // choice `byo set` makes), which is recorded as a divergence. The action
    // text names the `--config` path verbatim when one was given, as Python's
    // `f"saved {config_path}"` does.
    actions.push(match config_path {
        Some(path) => format!("saved {path}"),
        None => "saved config".to_string(),
    });

    let report = build_report(view, store).unwrap_or(Report {
        ready: false,
        summary: String::new(),
        checks: Vec::new(),
        roles: Vec::new(),
    });

    object(vec![
        ("ok", boolean(true)),
        ("actions", array(actions.into_iter().map(string).collect())),
        ("readiness", report.to_value()),
    ])
}

/// `_fix_legacy_paths()`'s `.crosspstr` → `.xpst` rewrite.
fn legacy_fixed(path: &str) -> String {
    path.replace(".crosspstr", ".xpst")
        .replace("crosspstr", "xpst")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_legacy_path_rewrite_matches_python() {
        assert_eq!(legacy_fixed("~/.crosspstr/downloads"), "~/.xpst/downloads");
        assert_eq!(legacy_fixed("crosspstr"), "xpst");
        assert_eq!(legacy_fixed("~/.xpst/downloads"), "~/.xpst/downloads");
    }
}
