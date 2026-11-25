//! `xpst transcript <video_id>` — a cached transcript from the knowledge
//! workspace.
//!
//! Parity target: `transcript()` in `src/xpst/cli.py`. The three lookups
//! (`<id>.json`, `content:<id>.json`, then the first file whose name contains
//! the id) already live in `xpst_surface::transcript` — the `xpst_transcript`
//! MCP tool serves them — so this module is the Click surface and the two
//! renderers.
//!
//! The CLI's contract differs from the tool's in three ways, and all three are
//! reproduced here:
//!
//! * a **miss exits 1** and prints `{"error": "Transcript not found",
//!   "video_id": …}` compactly (`json.dumps` defaults), where the tool serves
//!   the same document as a normal answer;
//! * a **hit** is printed with `json.dumps(result, indent=2)` — pretty, and
//!   `ensure_ascii=True`, so a non-ASCII transcript comes out as `\uXXXX`;
//! * an **invalid id** is Python's uncaught `ValueError`: empty stdout, exit 1.
//!   The Rust engine prints the exception line instead of the traceback frames
//!   (the ledger's standing divergence for uncaught `ValueError`s).

use std::io::Write;

use clap::ArgMatches;
use xpst_surface::transcript::{lookup_in_named, TranscriptError};

use crate::output::{object, string, write_python_json, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `xpst transcript <video_id> [--json]`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let video_id = leaf_string(leaf, "arg:video_id").unwrap_or_default();

    let Some(home) = xpst_surface::kb::xpst_home() else {
        let message = "cannot determine the xPST home: neither XPST_HOME nor HOME is set";
        let _ = writeln!(err, "{message}");
        return exit::GENERAL;
    };

    let found = match lookup_in_named(&home, &video_id, "content_hash") {
        Ok(found) => found,
        Err(TranscriptError::Invalid(message) | TranscriptError::Workspace(message)) => {
            // Python raises out of `Workspace.get_transcript()` with no handler,
            // so stdout stays empty and the process exits 1.
            let _ = writeln!(err, "ValueError: {message}");
            return exit::GENERAL;
        }
    };

    let Some(transcript) = found else {
        if json_mode {
            let payload = object(vec![
                ("error", string("Transcript not found")),
                ("video_id", string(video_id)),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, &format!("Transcript not found for: {video_id}"));
        }
        return exit::GENERAL;
    };

    if json_mode {
        let _ = write_text(out, &crate::output::python_pretty_json(&transcript));
        return exit::SUCCESS;
    }

    let mut rendered = format!("Transcript for {video_id}\n\n");
    rendered.push_str(
        transcript
            .get("text")
            .and_then(serde_json::Value::as_str)
            .unwrap_or(""),
    );
    let segments = transcript
        .get("segments")
        .and_then(serde_json::Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    if segments > 0 {
        rendered.push_str(&format!("\n\n{segments} segments"));
    }
    let _ = write_text(out, &rendered);
    exit::SUCCESS
}
