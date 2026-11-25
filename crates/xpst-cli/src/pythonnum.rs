//! Python's `int()` / `float()` parsing, for values that arrive as text.
//!
//! Click converts an `IntParamType`/`FloatParamType` option *before* the command
//! body runs, so `--port abc` is a usage error (exit 2) and `--port 0900` is the
//! integer 900. The Rust engine keeps every option as a `String` (the tree is
//! generated from the Click manifest), so the same conversion has to happen
//! here — and it has to accept exactly what Python accepts, or a value the
//! Python engine takes becomes a usage error in the Rust engine.
//!
//! Python's rules, as implemented below:
//!
//! * surrounding whitespace is ignored (`int(" 9000 ") == 9000`);
//! * a single `+`/`-` sign is allowed;
//! * underscores may separate digits but not lead, trail or double up
//!   (`int("1_000") == 1000`, `int("_1")` and `int("1__0")` raise);
//! * `float()` additionally accepts `inf`/`infinity`/`nan` in any case.
//!
//! Deliberately *not* accepted: `0x10`, `1e3` for `int()`, and the `int()`
//! bases Python only honours with an explicit `base=` argument.
//!
//! Python integers are arbitrary precision, so `--port 99999999999999999999` is
//! a valid invocation that renders as-is. [`int_text`] therefore validates and
//! canonicalises without a width limit; [`int`] is the `i64` view for handlers
//! that need arithmetic, and [`int_saturating`] is the same view clamped instead
//! of rejected, so an out-of-range value cannot silently fall back to a default.

/// Python's `int(text)` as canonical decimal text, or `None` where it raises.
///
/// Canonical means no sign on zero, no leading zeros and no underscores:
/// `int("+000_900")` is `"900"`, which is what `f"{value}"` prints.
pub fn int_text(text: &str) -> Option<String> {
    let trimmed = text.trim();
    let (negative, digits) = match trimmed.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') {
        return None;
    }
    let mut cleaned = String::with_capacity(digits.len());
    let mut previous_underscore = false;
    for ch in digits.chars() {
        if ch == '_' {
            if previous_underscore {
                return None;
            }
            previous_underscore = true;
            continue;
        }
        if !ch.is_ascii_digit() {
            return None;
        }
        previous_underscore = false;
        cleaned.push(ch);
    }
    let without_zeros = cleaned.trim_start_matches('0');
    let canonical = if without_zeros.is_empty() {
        "0"
    } else {
        without_zeros
    };
    Some(if negative && canonical != "0" {
        format!("-{canonical}")
    } else {
        canonical.to_string()
    })
}

/// `int(text)` as an `i64`, or `None` where Python raises **or** the value does
/// not fit. Use [`int_saturating`] when a huge value must not become a default.
pub fn int(text: &str) -> Option<i64> {
    int_text(text)?.parse::<i64>().ok()
}

/// `int(text)` clamped to the `i64` range.
///
/// Python would carry the exact value; the handlers that do arithmetic on it
/// (`--ttl`, `--keep`, `--rtmp-port`) saturate instead of falling back to a
/// default, which keeps the observable behaviour right for `--keep` (nothing is
/// deleted) and for `--rtmp-port` (still an out-of-range error).
pub fn int_saturating(text: &str) -> Option<i64> {
    let canonical = int_text(text)?;
    Some(match canonical.parse::<i64>() {
        Ok(value) => value,
        Err(_) if canonical.starts_with('-') => i64::MIN,
        Err(_) => i64::MAX,
    })
}

/// `float(text)`, or `None` where Python raises `ValueError`.
pub fn float(text: &str) -> Option<f64> {
    let trimmed = text.trim();
    if trimmed.is_empty() || trimmed.starts_with('_') || trimmed.ends_with('_') {
        return None;
    }
    let cleaned: String = trimmed.chars().filter(|ch| *ch != '_').collect();
    // `f64::from_str` accepts `inf`/`infinity`/`nan` case-insensitively, which
    // is what Python's `float()` accepts too.
    cleaned.parse::<f64>().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn int_matches_python() {
        assert_eq!(int("9000"), Some(9000));
        assert_eq!(int(" 9000 "), Some(9000));
        assert_eq!(int("+5"), Some(5));
        assert_eq!(int("-5"), Some(-5));
        assert_eq!(int("0900"), Some(900));
        assert_eq!(int("1_000"), Some(1000));
        assert_eq!(int("0"), Some(0));
        for bad in [
            "", "abc", "1__0", "_1", "1_", "0x10", "1.5", "1e3", "--1", "1 2",
        ] {
            assert_eq!(int(bad), None, "{bad:?} must not parse as an int");
        }
    }

    #[test]
    fn int_text_is_canonical_and_unbounded() {
        assert_eq!(int_text("0900").as_deref(), Some("900"));
        assert_eq!(int_text("+000_900").as_deref(), Some("900"));
        assert_eq!(int_text("-0").as_deref(), Some("0"));
        assert_eq!(int_text("-007").as_deref(), Some("-7"));
        assert_eq!(
            int_text("99999999999999999999").as_deref(),
            Some("99999999999999999999"),
            "Python ints are arbitrary precision"
        );
        assert_eq!(int_text("abc"), None);
    }

    #[test]
    fn int_saturating_clamps_instead_of_defaulting() {
        assert_eq!(int_saturating("42"), Some(42));
        assert_eq!(int_saturating("99999999999999999999"), Some(i64::MAX));
        assert_eq!(int_saturating("-99999999999999999999"), Some(i64::MIN));
        assert_eq!(int_saturating("abc"), None);
    }

    #[test]
    fn float_matches_python() {
        assert_eq!(float("1.5"), Some(1.5));
        assert_eq!(float(" 2 "), Some(2.0));
        assert_eq!(float("1_0.5"), Some(10.5));
        assert_eq!(float("-0.25"), Some(-0.25));
        assert!(float("inf").is_some_and(f64::is_infinite));
        assert!(float("NaN").is_some_and(f64::is_nan));
        for bad in ["", "abc", "_1", "1_", "1.2.3"] {
            assert_eq!(float(bad), None, "{bad:?} must not parse as a float");
        }
    }
}
