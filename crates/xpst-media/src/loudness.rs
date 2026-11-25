//! EBU R128 loudness measurement, ported from `xpst/media/loudness.py`.
//!
//! Only the *measurement* pass is ported — the one `verify_media` needs to
//! answer "is this file off-target for the destination?". The normalisation
//! pass (`build_loudnorm_filter`, the linear-mode second pass) belongs to the
//! encoder, which is not part of this slice.
//!
//! The command line is Python's, verbatim: a different filter string would
//! measure a different thing, and the check compares the measured number
//! against the destination's target.
//!
//! Never panics and never returns an error: a missing binary, an unprobeable
//! file or a timeout all degrade to `None`, because a loudness hiccup must not
//! block a legitimate upload.

use std::path::Path;
use std::process::Command;

/// True-peak ceiling every destination is normalised to (`-1.5 dBTP`).
pub const TRUE_PEAK_TARGET: f64 = -1.5;
/// Loudness range target (`11 LU`).
pub const LRA_TARGET: f64 = 11.0;

/// The four values `loudnorm`'s analysis pass reports.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Loudness {
    pub input_i: f64,
    pub input_tp: f64,
    pub input_lra: f64,
    pub input_thresh: f64,
}

/// Run the `loudnorm` analysis pass and return the measured values.
///
/// `None` on any failure (missing binary, unprobeable file, timeout, a stderr
/// block without the four measured keys), which is what Python's
/// `measure_loudness` returns.
#[must_use]
pub fn measure(ffmpeg_path: Option<&str>, input: &Path, target_i: f64) -> Option<Loudness> {
    let binary = ffmpeg_path
        .map(str::to_owned)
        .or_else(crate::resolve_ffmpeg)
        .unwrap_or_else(|| "ffmpeg".to_owned());
    let filter =
        format!("loudnorm=I={target_i}:TP={TRUE_PEAK_TARGET}:LRA={LRA_TARGET}:print_format=json");
    let output = Command::new(&binary)
        .arg("-hide_banner")
        .arg("-nostats")
        .arg("-i")
        .arg(input)
        .arg("-vn")
        .arg("-sn")
        .arg("-af")
        .arg(&filter)
        .arg("-f")
        .arg("null")
        .arg("-")
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    // The filter prints one JSON object on stderr; Python returns the FIRST
    // parseable block that carries the measured keys, so this does too.
    for block in json_blocks(&stderr) {
        let Ok(document) = serde_json::from_str::<serde_json::Value>(&block) else {
            continue;
        };
        let Some(object) = document.as_object() else {
            continue;
        };
        if !["input_i", "input_tp", "input_lra", "input_thresh"]
            .iter()
            .all(|key| object.contains_key(*key))
        {
            continue;
        }
        let read = |key: &str| -> Option<f64> {
            match object.get(key)? {
                serde_json::Value::Number(number) => number.as_f64(),
                // `float("−inf")` succeeds in Python, so a string that parses as
                // a float is accepted there too.
                serde_json::Value::String(text) => text.trim().parse::<f64>().ok(),
                _ => None,
            }
        };
        let (Some(input_i), Some(input_tp), Some(input_lra), Some(input_thresh)) = (
            read("input_i"),
            read("input_tp"),
            read("input_lra"),
            read("input_thresh"),
        ) else {
            // A non-numeric value is Python's `TypeError`/`ValueError` branch:
            // the whole measurement is unusable, not just this block.
            return None;
        };
        return Some(Loudness {
            input_i,
            input_tp,
            input_lra,
            input_thresh,
        });
    }
    None
}

/// Every `\{[^{}]*\}` block in `text`, in order.
///
/// Python's `_JSON_BLOCK_RE` is `\{[^{}]*\}`: a brace pair with no nested
/// brace. `loudnorm`'s block is flat, so this is the whole grammar needed.
fn json_blocks(text: &str) -> Vec<String> {
    let bytes = text.as_bytes();
    let mut blocks = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'{' {
            index += 1;
            continue;
        }
        // Scan forward for a `}` with no `{` in between. A nested `{` means this
        // opening brace cannot start a candidate, so the scan resumes one byte
        // later — which is how the regex finds the INNER block of `{"a":{"b":1}}`.
        let mut end = index + 1;
        let mut closed = false;
        while end < bytes.len() {
            match bytes[end] {
                b'{' => break,
                b'}' => {
                    closed = true;
                    break;
                }
                _ => end += 1,
            }
        }
        if closed {
            blocks.push(text[index..=end].to_owned());
            index = end + 1;
        } else {
            index += 1;
        }
    }
    blocks
}

/// `f"{value:.1f}"` for a float that may be infinite or NaN.
///
/// Rust renders NaN as `NaN` where Python renders `nan`, and the detail string
/// is part of the payload, so the two spellings are reconciled here.
#[must_use]
pub fn format_one_decimal(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value.is_sign_negative() {
            "-inf".to_owned()
        } else {
            "inf".to_owned()
        };
    }
    format!("{value:.1}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_flat_json_block_is_found() {
        let text = "noise\n{\"input_i\":\"-13.5\",\"input_tp\":\"-1.0\"}\nmore";
        assert_eq!(
            json_blocks(text),
            vec!["{\"input_i\":\"-13.5\",\"input_tp\":\"-1.0\"}".to_owned()]
        );
    }

    #[test]
    fn a_nested_block_is_not_a_candidate() {
        // `\{[^{}]*\}` cannot match a block with a nested object, so the outer
        // braces are skipped and the inner one is the candidate.
        let text = "{\"a\":{\"b\":1}}";
        assert_eq!(json_blocks(text), vec!["{\"b\":1}".to_owned()]);
    }

    #[test]
    fn python_float_spellings_are_reproduced() {
        assert_eq!(format_one_decimal(-13.54), "-13.5");
        assert_eq!(format_one_decimal(f64::NAN), "nan");
        assert_eq!(format_one_decimal(f64::INFINITY), "inf");
        assert_eq!(format_one_decimal(f64::NEG_INFINITY), "-inf");
    }
}
