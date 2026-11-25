//! `xpst media status` — which ffmpeg/ffprobe xPST will use, and where it came
//! from.
//!
//! Parity target: `src/xpst/cli.py::media_status`, which prints
//! `{"bin_dir", "binaries", "ok"}` from `media_binary_status()`. Nothing here
//! downloads, spawns or probes a binary: the command answers "what would the
//! engine run", which is the question an operator asks when a re-encode fails.
//!
//! The payload is built from `xpst_config::Value` so the key order and the
//! separators match `json.dumps` byte for byte.

use std::io::Write;

use clap::ArgMatches;

use crate::output::{boolean, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// Run `media status`.
pub fn status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let rows = xpst_media::status();
    let bin_dir = xpst_media::media_bin_dir()
        .map(|dir| dir.to_string_lossy().to_string())
        .unwrap_or_default();
    let ok = rows.iter().all(|row| row.ok);

    if json_mode {
        let binaries = object(
            rows.iter()
                .map(|row| {
                    (
                        row.name,
                        object(vec![
                            ("name", string(row.name)),
                            ("ok", boolean(row.ok)),
                            (
                                "path",
                                row.path.clone().map_or_else(crate::output::null, string),
                            ),
                            ("source", string(row.source)),
                        ]),
                    )
                })
                .collect(),
        );
        let payload = object(vec![
            ("bin_dir", string(bin_dir)),
            ("binaries", binaries),
            ("ok", boolean(ok)),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let mut text = String::new();
    for row in &rows {
        if row.ok {
            text.push_str(&format!(
                "✓ {} ({}): {}\n",
                row.name,
                row.source,
                row.path.as_deref().unwrap_or("")
            ));
        } else {
            text.push_str(&format!("• {}: not found\n", row.name));
        }
    }
    if !ok {
        text.push_str(
            "Run xpst media fetch to download a verified static build, or install ffmpeg \
             (macOS: brew install ffmpeg).",
        );
    }
    let _ = write_text(out, text.trim_end());
    // `err` is unused: `media status` has no failure path that is not a missing
    // binary, which is reported in the payload rather than as an error.
    let _ = err;
    exit::SUCCESS
}
