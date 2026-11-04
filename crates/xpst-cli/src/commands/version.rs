//! `xpst version` — what this binary is.
//!
//! The Python engine prints its dependency versions here; a Rust binary has no
//! Python dependencies to print. The payload keeps the `version` key and names
//! the engine explicitly so a caller can tell the two apart instead of
//! mistaking an absent `dependencies` map for an empty one.

use std::io::Write;

use clap::ArgMatches;
use serde_json::json;

use crate::output::{write_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// Version of this build. `XPST_GIT_COMMIT` is injected by CI/release; "unknown"
/// is reported when the build was not stamped (never guessed).
fn commit() -> &'static str {
    option_env!("XPST_GIT_COMMIT").unwrap_or("unknown")
}

pub fn run<W: Write, E: Write>(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut W,
    err: &mut E,
) -> i32 {
    let _ = err;
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));

    if json_mode {
        let payload = json!({
            "version": env!("CARGO_PKG_VERSION"),
            "engine": "rust",
            "phase": "P0",
            "commit": commit(),
            "python_required": false,
            "live_checks": false,
        });
        let _ = write_json(out, &payload);
    } else {
        let text = format!(
            "xPST, version {}\n\
             engine: rust (P0 scaffold — read paths and command tree only)\n\
             commit: {}\n\
             python_required: no",
            env!("CARGO_PKG_VERSION"),
            commit()
        );
        let _ = write_text(out, &text);
    }
    exit::SUCCESS
}
