//! `xpst update --components` — the offline component inventory.
//!
//! Parity target: `update()` in `src/xpst/cli.py` (lines 506-560). The command
//! has four paths and only one is local:
//!
//! | invocation | shipped behaviour | here |
//! |---|---|---|
//! | `--self` | `get_latest_version("xpst")` over PyPI, then a pip install | stub |
//! | `--check` | `update_all(check_only=True)` — PyPI for every package | stub |
//! | `--components` | `check_update_components(include_network=False)` | **ported** |
//! | (none) | `update_all(check_only=False)` — PyPI, then pip installs | stub |
//!
//! `--components --check` is the network half of the same document
//! (`include_network=True`), so it stays a stub with `--check`.
//!
//! The document itself is [`crate::commands::updates::components`], shared with
//! the `updates` section of `xpst diagnostics`.

use std::io::Write;

use clap::ArgMatches;

use crate::commands::updates;
use crate::output::write_text;
use crate::{exit, flag, GlobalOptions};

/// `xpst update [--check] [--components] [--self] [--json]`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let check_only = leaf.is_some_and(|m| flag(m, "opt:check"));
    let components = leaf.is_some_and(|m| flag(m, "opt:components"));
    let self_update = leaf.is_some_and(|m| flag(m, "opt:self"));

    // `--self` wins over `--components` in the shipped body, and `--check`
    // turns the component inventory into a PyPI query. Everything except
    // `--components` alone is the network half.
    if self_update || check_only || !components {
        return crate::not_implemented("update", global, leaf, out, err);
    }

    let document = updates::components("pip");
    if json_mode {
        let _ = crate::output::write_python_json_value(out, &document);
        return exit::SUCCESS;
    }

    // Human view: the same facts as plain lines (the standing divergence —
    // Rich tables are not reproduced).
    let mut rendered = String::new();
    for section in ["app", "packages", "helpers", "provider_metadata"] {
        let Some(rows) = document.get(section).and_then(|value| value.as_array()) else {
            continue;
        };
        if rows.is_empty() {
            continue;
        }
        let title = section.replace('_', " ");
        rendered.push_str(&format!("{}\n", title.to_uppercase()));
        for row in rows {
            let name = row
                .get("name")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            let status = row
                .get("status")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            let version = row
                .get("current_version")
                .and_then(|value| value.as_str())
                .unwrap_or("-");
            let action = row
                .get("action")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            rendered.push_str(&format!("  {name} ({version}): {status} — {action}\n"));
        }
        rendered.push('\n');
    }
    let _ = write_text(out, &rendered);
    exit::SUCCESS
}
