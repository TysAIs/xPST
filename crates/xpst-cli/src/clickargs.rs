//! Click's argument-conversion contract, for the options the Rust tree keeps as
//! text.
//!
//! The command tree is generated from the Click manifest, so every option is a
//! `String` in clap. Click, by contrast, converts an `IntParamType` /
//! `FloatParamType` / `IntRange` option *before* the command body runs: a value
//! that does not convert is a usage error (exit 2, empty stdout) with a fixed
//! message. Without this module the Rust engine accepted `--port abc` and
//! printed `http://127.0.0.1:abc/bio`, and silently fell back to the default for
//! `--ttl abc` — a value the Python engine refuses.
//!
//! Two things are reproduced here:
//!
//! * **Conversion** — [`crate::pythonnum`] accepts exactly what Python's
//!   `int()`/`float()` accept, so `--port 0900` is the integer 900 in both
//!   engines rather than the string `"0900"` in one.
//! * **The message** — Click's `Usage: … / Try '… --help' for help. / Error:
//!   Invalid value for '--port' / '-p': 'abc' is not a valid integer.` The
//!   program name is `xpst` (the binary) where Click prints `python -m xpst`;
//!   that is the only difference, and it is the same one every usage error in
//!   this engine already has.
//!
//! Range bounds come from the manifest (`min`/`max`, captured from Click's
//! `IntRange`), so `generate ideas --count 999` is refused here even though the
//! command itself is still a stub — exactly as Click refuses it before the
//! callback runs.

use std::io::Write;

use clap::ArgMatches;

use crate::parity::{self, ParamSpec};
use crate::{cli, exit, pythonnum};

/// Validate every numeric option the caller supplied.
///
/// Returns `Some(exit code)` when a value does not convert (the message has
/// already been written to `err`), `None` when the invocation may proceed.
pub fn validate_numeric_options<E: Write>(
    path: &[String],
    leaf: Option<&ArgMatches>,
    err: &mut E,
) -> Option<i32> {
    let leaf = leaf?;
    let spec = parity::command_at(path)?;
    for param in &spec.params {
        if param.is_flag || param.is_positional() {
            continue;
        }
        let Some(long) = param.long_opts().first().copied() else {
            continue;
        };
        let Some(raw) = crate::string_value(leaf, &cli::option_id(long)) else {
            continue;
        };
        match param.type_name.as_str() {
            // Python integers are arbitrary precision, so the conversion is
            // checked on the canonical text, not on an `i64`: `--port 10^20` is
            // a valid invocation.
            "integer" => {
                if pythonnum::int_text(&raw).is_none() {
                    let _ = writeln!(
                        err,
                        "{}",
                        invalid_value(path, param, &format!("'{raw}' is not a valid integer."))
                    );
                    return Some(exit::CONFIG_ERROR);
                }
            }
            "integer range" => {
                let Some(canonical) = pythonnum::int_text(&raw) else {
                    let _ = writeln!(
                        err,
                        "{}",
                        invalid_value(
                            path,
                            param,
                            &format!("'{raw}' is not a valid integer range.")
                        )
                    );
                    return Some(exit::CONFIG_ERROR);
                };
                // A value beyond `i128` is out of every range Click can declare,
                // so a failed parse is a range failure, not a conversion one.
                let value = canonical.parse::<i128>().ok().map(|value| value as f64);
                if let (Some(min), Some(max)) = (param.min, param.max) {
                    let in_range = value.is_some_and(|value| value >= min && value <= max);
                    if !in_range {
                        // Click prints the converted value, not the raw text:
                        // `--count 09` reports `9 is not in the range 1<=x<=10.`
                        let _ = writeln!(
                            err,
                            "{}",
                            invalid_value(
                                path,
                                param,
                                &format!(
                                    "{canonical} is not in the range {}<=x<={}.",
                                    bound(min),
                                    bound(max)
                                )
                            )
                        );
                        return Some(exit::CONFIG_ERROR);
                    }
                }
            }
            "float" if pythonnum::float(&raw).is_none() => {
                let _ = writeln!(
                    err,
                    "{}",
                    invalid_value(path, param, &format!("'{raw}' is not a valid float."))
                );
                return Some(exit::CONFIG_ERROR);
            }
            _ => {}
        }
    }
    None
}

/// A numeric option's converted value, for a handler that runs after
/// [`validate_numeric_options`] has already refused anything Python would.
///
/// Using this instead of `raw.parse::<i64>()` also canonicalises the value the
/// way Click does: `--port 0900` is the integer 900, so the URL says `:900`.
/// A value outside `i64` saturates rather than becoming `None` (which a handler
/// would read as "not supplied" and replace with a default).
pub fn leaf_integer(leaf: Option<&ArgMatches>, id: &str) -> Option<i64> {
    crate::leaf_string(leaf, id).and_then(|raw| pythonnum::int_saturating(&raw))
}

/// A numeric option's canonical decimal text, for a handler that renders it.
///
/// `bio --port` and `pair --port` interpolate the value into a URL, and Python
/// prints the *integer*: `--port 0900` is `:900`, and a value beyond `i64` is
/// still printed in full.
pub fn leaf_integer_text(leaf: Option<&ArgMatches>, id: &str) -> Option<String> {
    crate::leaf_string(leaf, id).and_then(|raw| pythonnum::int_text(&raw))
}

/// [`leaf_integer`] for a `FloatParamType` option.
pub fn leaf_float(leaf: Option<&ArgMatches>, id: &str) -> Option<f64> {
    crate::leaf_string(leaf, id).and_then(|raw| pythonnum::float(&raw))
}

/// Render a range bound the way Click does: `1`, not `1.0`.
fn bound(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

/// Click's `BadParameter` rendering for a converted option.
fn invalid_value(path: &[String], param: &ParamSpec, detail: &str) -> String {
    format!(
        "{usage}\nTry '{prog} --help' for help.\n\nError: Invalid value for {hint}: {detail}",
        usage = usage_line(path),
        prog = program(path),
        hint = error_hint(param),
    )
}

/// `xpst <path>` — the program name Click would print for this command.
fn program(path: &[String]) -> String {
    if path.is_empty() {
        cli::BIN_NAME.to_string()
    } else {
        format!("{} {}", cli::BIN_NAME, path.join(" "))
    }
}

/// Click's usage line: `Usage: xpst state import [OPTIONS] SOURCE`.
///
/// `[OPTIONS]` appears only when the command declares an option, and a
/// positional is bracketed when it is optional — both are Click's rules, and
/// both are read from the manifest rather than guessed.
fn usage_line(path: &[String]) -> String {
    let mut line = format!("Usage: {}", program(path));
    let Some(spec) = parity::command_at(path) else {
        return line;
    };
    if spec.params.iter().any(|param| !param.is_positional()) {
        line.push_str(" [OPTIONS]");
    }
    for param in spec.params.iter().filter(|param| param.is_positional()) {
        let name = param
            .metavar
            .clone()
            .unwrap_or_else(|| param.name.to_uppercase());
        if param.required {
            line.push(' ');
            line.push_str(&name);
        } else {
            line.push_str(&format!(" [{name}]"));
        }
    }
    line
}

/// Click's `param.get_error_hint()`: `'--port' / '-p'`, or `'--ttl'`.
fn error_hint(param: &ParamSpec) -> String {
    if param.is_positional() {
        let name = param
            .metavar
            .clone()
            .unwrap_or_else(|| param.name.to_uppercase());
        return format!("'{name}'");
    }
    let longs = param.long_opts();
    let shorts = param.short_opts();
    let mut parts: Vec<String> = longs.iter().map(|long| format!("'{long}'")).collect();
    parts.extend(shorts.iter().map(|short| format!("'-{short}'")));
    parts.join(" / ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn path(segments: &[&str]) -> Vec<String> {
        segments.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn usage_lines_match_click() {
        assert_eq!(usage_line(&path(&["bio"])), "Usage: xpst bio [OPTIONS]");
        assert_eq!(
            usage_line(&path(&["state", "import"])),
            "Usage: xpst state import [OPTIONS] SOURCE"
        );
        assert_eq!(
            usage_line(&path(&["failures", "clear"])),
            "Usage: xpst failures clear [OPTIONS] [VIDEO_ID]"
        );
    }

    #[test]
    fn error_hints_match_click() {
        let bio = parity::command_at(&path(&["bio"])).expect("bio");
        let port = bio.params.iter().find(|p| p.name == "port").expect("port");
        assert_eq!(error_hint(port), "'--port' / '-p'");

        let pair = parity::command_at(&path(&["pair"])).expect("pair");
        let ttl = pair.params.iter().find(|p| p.name == "ttl_s").expect("ttl");
        assert_eq!(error_hint(ttl), "'--ttl'");
    }

    #[test]
    fn invalid_value_message_matches_click() {
        let bio = parity::command_at(&path(&["bio"])).expect("bio");
        let port = bio.params.iter().find(|p| p.name == "port").expect("port");
        assert_eq!(
            invalid_value(&path(&["bio"]), port, "'abc' is not a valid integer."),
            "Usage: xpst bio [OPTIONS]\n\
             Try 'xpst bio --help' for help.\n\n\
             Error: Invalid value for '--port' / '-p': 'abc' is not a valid integer."
        );
    }

    #[test]
    fn range_bounds_render_as_integers() {
        assert_eq!(bound(1.0), "1");
        assert_eq!(bound(10.0), "10");
        assert_eq!(bound(0.5), "0.5");
    }
}
