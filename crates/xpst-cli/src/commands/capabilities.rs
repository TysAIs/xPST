//! `xpst capabilities` — what xPST can publish, per destination.
//!
//! Parity target: `src/xpst/cli.py::capabilities`, which prints
//! `xpst.content.capability_document()` — the same document the MCP tool
//! `xpst_capabilities` and `GET /api/capabilities` return, so what a human reads
//! and what an agent plans against cannot drift.
//!
//! **The document is captured, not recomputed.** It is derived from the content
//! contract's tables (`DESTINATION_CONTENT_PROFILES`, `CONTENT_TYPES`,
//! `media.specs.PLATFORM_SPECS`), which is a large data port with no behaviour
//! of its own; transcribing it into Rust would make a typo invisible. Instead
//! `xtask/scripts/capture_capabilities.py` runs the shipped Python and writes
//! `resources/capabilities.json` as the exact bytes `json_output()` prints, and
//! `xtask/scripts/capture_local_surface.py --check-with` diffs the Rust payload
//! against a live `python -m xpst capabilities --json` on every CI run — so a
//! Python-side change fails the build until the resource is refreshed. The
//! ledger note says the same thing.
//!
//! The resource lives in `xpst-surface` because the `xpst_capabilities` MCP tool
//! nests the same document in its own payload; this module is the CLI renderer.
//!
//! Text mode renders a plain table rather than Rich's box drawing, which is the
//! same choice every other implemented command makes; `--json` is the parity
//! surface.

use std::io::Write;

use clap::ArgMatches;
use serde_json::Value;

use crate::output::write_text;
use crate::{exit, flag, GlobalOptions};

/// The captured document, byte-identical to `json_output(capability_document())`.
const DOCUMENT: &str = xpst_surface::capabilities::DOCUMENT;

/// Run `capabilities`.
pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    if json_mode {
        // Already one line with Python's separators and a trailing newline.
        let _ = write!(out, "{DOCUMENT}");
        return exit::SUCCESS;
    }

    let document: Value = match serde_json::from_str(DOCUMENT) {
        Ok(document) => document,
        Err(error) => {
            // A corrupt embedded resource is a build defect, not a user error.
            let _ = writeln!(
                err,
                "xpst capabilities: embedded document is invalid: {error}"
            );
            return exit::GENERAL;
        }
    };
    let _ = write_text(out, &render_text(&document));
    exit::SUCCESS
}

fn render_text(document: &Value) -> String {
    let mut text = String::from("What xPST can publish\n");
    let vocabulary = document["content_types"]
        .as_array()
        .map(|types| {
            types
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    text.push_str(&format!("Vocabulary: {vocabulary}\n\n"));

    let mut rows: Vec<[String; 5]> = Vec::new();
    let mut platforms: Vec<&str> = document["platforms"]
        .as_object()
        .map(|map| map.keys().map(String::as_str).collect())
        .unwrap_or_default();
    platforms.sort_unstable();
    for platform in platforms {
        let profile = &document["platforms"][platform];
        let declared = join_list(&profile["declared"]);
        let implemented = join_list(&profile["implemented"]);
        let mut note = profile["note"].as_str().unwrap_or("").to_string();
        let false_declarations = join_list(&profile["declared_but_unimplemented"]);
        if !false_declarations.is_empty() {
            if !note.is_empty() {
                note.push(' ');
            }
            note.push_str(&format!("FALSE DECLARATION: {false_declarations}"));
        }
        rows.push([
            format!(
                "{} ({platform})",
                profile["display_name"].as_str().unwrap_or(platform)
            ),
            declared,
            implemented,
            envelope_text(document, platform),
            note,
        ]);
    }

    let headers = [
        "Destination",
        "Declared",
        "Implemented",
        "Envelope",
        "Notes",
    ];
    let mut widths = headers.map(str::len);
    for row in &rows {
        for (index, cell) in row.iter().enumerate() {
            widths[index] = widths[index].max(cell.chars().count());
        }
    }
    let line = |cells: [&str; 5]| {
        let mut line = String::new();
        for (index, cell) in cells.iter().enumerate() {
            if index > 0 {
                line.push_str(" | ");
            }
            line.push_str(cell);
            if index + 1 < cells.len() {
                line.push_str(&" ".repeat(widths[index] - cell.chars().count()));
            }
        }
        line.trim_end().to_string()
    };
    text.push_str(&line(headers));
    text.push('\n');
    for row in &rows {
        text.push_str(&line([
            row[0].as_str(),
            row[1].as_str(),
            row[2].as_str(),
            row[3].as_str(),
            row[4].as_str(),
        ]));
        text.push('\n');
    }

    if let Some(map) = document["declared_but_unimplemented"].as_object() {
        if !map.is_empty() {
            let mut entries: Vec<String> = map
                .iter()
                .map(|(platform, types)| format!("{platform}={}", join_list(types)))
                .collect();
            entries.sort();
            text.push_str(&format!(
                "\nA destination declares a content type with no implementation: {}\n",
                entries.join(", ")
            ));
        }
    }
    text.trim_end().to_string()
}

fn join_list(value: &Value) -> String {
    value
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .filter(|joined| !joined.is_empty())
        .unwrap_or_else(|| "—".to_string())
}

/// `_capability_envelope_text()`: the one-line envelope for the table.
fn envelope_text(document: &Value, platform: &str) -> String {
    let envelope = &document["envelopes"][platform];
    let video = &envelope["video"];
    let image = &envelope["image"];
    let caption = envelope["caption"]["max_characters"].as_u64();
    let mut parts: Vec<String> = Vec::new();
    if let Some(seconds) = video["max_duration_seconds"].as_u64() {
        if seconds > 0 {
            parts.push(format!("video ≤{seconds}s"));
        }
    }
    if let Some(bytes) = video["max_bytes"].as_u64() {
        if bytes > 0 {
            parts.push(format!("≤{} MB", bytes / (1024 * 1024)));
        }
    }
    if let Some(bytes) = image["max_bytes"].as_u64() {
        if bytes > 0 {
            parts.push(format!("image ≤{} MB", bytes / (1024 * 1024)));
        }
    }
    if let Some(caption) = caption {
        if caption > 0 {
            parts.push(format!("text ≤{caption} ch"));
        }
    }
    if parts.is_empty() {
        "—".to_string()
    } else {
        parts.join(", ")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_embedded_document_is_the_python_payload() {
        // A capture that silently produced a stub would make every surface
        // advertise nothing, so assert the shape the product needs.
        let document: Value = serde_json::from_str(DOCUMENT).expect("valid JSON");
        assert_eq!(document["contract_version"], 2);
        assert_eq!(
            document["content_types"],
            serde_json::json!(["video", "image", "carousel", "text", "thread"])
        );
        let platforms = document["platforms"].as_object().expect("platforms");
        assert_eq!(
            platforms.len(),
            9,
            "the document must cover every destination"
        );
        for platform in [
            "youtube",
            "x",
            "instagram",
            "tiktok",
            "threads",
            "facebook",
            "rumble",
            "bluesky",
            "messenger",
        ] {
            assert!(platforms.contains_key(platform), "missing {platform}");
        }
        assert!(
            DOCUMENT.ends_with('\n'),
            "the resource is one line plus a newline"
        );
        assert_eq!(DOCUMENT.lines().count(), 1, "the resource must be one line");
    }

    #[test]
    fn the_text_table_names_every_destination() {
        let document: Value = serde_json::from_str(DOCUMENT).unwrap();
        let text = render_text(&document);
        for platform in [
            "youtube",
            "x",
            "instagram",
            "tiktok",
            "threads",
            "facebook",
            "rumble",
            "bluesky",
            "messenger",
        ] {
            assert!(text.contains(platform), "the table omits {platform}");
        }
        assert!(text.starts_with("What xPST can publish"));
    }
}
