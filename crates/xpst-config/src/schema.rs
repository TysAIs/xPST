//! The YAML 1.1 implicit type resolver, as PyYAML implements it.
//!
//! The Python engine reads and writes `config.yaml` with `yaml.safe_load` and
//! `yaml.dump`, so **PyYAML's resolver** — not the YAML 1.2 core schema, and
//! not the YAML 1.1 specification — is the contract this crate has to meet.
//! The two differ in ways that change a config's meaning, and every one of
//! them has been observed on the real library rather than read off a spec:
//!
//! * `yes`/`Yes`/`YES`/`on`/`On`/`ON`/`no`/`No`/`NO`/`off`/`Off`/`OFF` are
//!   booleans, but a bare `y` or `n` is a **string** (the 1.1 spec makes them
//!   booleans; PyYAML deliberately does not).
//! * `007` is the integer 7 and `010` is 8 — a leading zero means octal — but
//!   `08` is a **string**, because 8 is not an octal digit.
//! * `0o17` is a **string** (the 1.1 spec has no `0o` prefix; only `0b`, `0x`
//!   and a bare leading `0`).
//! * `1_000` is 1000, and `1:30` is the sexagesimal integer 90.
//! * A float exponent must carry an explicit sign, so `1e3` is a **string**
//!   while `1e+3` is a float.
//! * `.inf`, `-.inf` and `.nan` are floats; `+.nan` is a string.
//! * `2026-01-01` is a date and `2026-01-01T00:00:00Z` is a datetime.
//!
//! Both directions use this module. The reader types a plain scalar with
//! [`resolve_plain`]; the writer asks the same function whether a string would
//! be re-read as something else, and therefore has to be quoted.

use crate::yaml::NonFinite;

/// What a plain (unquoted) scalar means under PyYAML's resolver.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Scalar {
    /// `~`, `null`, `Null`, `NULL`, or the empty scalar.
    Null,
    /// A YAML 1.1 boolean spelling.
    Bool(bool),
    /// An integer, including octal, hex, binary and sexagesimal spellings.
    Int(i64),
    /// A finite float.
    Float(f64),
    /// `.inf`, `-.inf` or `.nan`.
    NonFinite(NonFinite),
    /// A date or datetime, already rendered the way PyYAML dumps it.
    Timestamp(String),
    /// A scalar shaped like a timestamp whose date or clock is out of range.
    ///
    /// PyYAML's resolver tags it as a timestamp and its constructor then raises
    /// `ValueError`, so `yaml.safe_load` fails on it. The reader turns this
    /// into an error rather than silently keeping the text as a string.
    InvalidTimestamp,
    /// The merge key `<<`.
    Merge,
    /// The value key `=`.
    Value,
    /// A lone `!`, `&` or `*`.
    Yaml,
}

/// Resolve a plain scalar, or `None` when PyYAML would read it as a string.
///
/// The order is PyYAML's own resolver order — bool, float, int, merge, null,
/// timestamp, value, yaml — and the first match wins. It matters: `1:30` is an
/// int, not a float, and `null` is a null, not a string.
pub(crate) fn resolve_plain(text: &str) -> Option<Scalar> {
    if let Some(flag) = parse_bool(text) {
        return Some(Scalar::Bool(flag));
    }
    if let Some(value) = parse_float(text) {
        return Some(value);
    }
    if let Some(value) = parse_int(text) {
        return Some(Scalar::Int(value));
    }
    if text == "<<" {
        return Some(Scalar::Merge);
    }
    if matches!(text, "" | "~" | "null" | "Null" | "NULL") {
        return Some(Scalar::Null);
    }
    if let Some(stamp) = parse_timestamp(text) {
        return Some(match stamp {
            Ok(stamp) => Scalar::Timestamp(stamp),
            Err(()) => Scalar::InvalidTimestamp,
        });
    }
    if text == "=" {
        return Some(Scalar::Value);
    }
    if matches!(text, "!" | "&" | "*") {
        return Some(Scalar::Yaml);
    }
    None
}

/// PyYAML's boolean spellings, exactly — no `y`/`n`, and case-sensitive.
fn parse_bool(text: &str) -> Option<bool> {
    match text {
        "yes" | "Yes" | "YES" | "true" | "True" | "TRUE" | "on" | "On" | "ON" => Some(true),
        "no" | "No" | "NO" | "false" | "False" | "FALSE" | "off" | "Off" | "OFF" => Some(false),
        _ => None,
    }
}

/// PyYAML's float pattern:
///
/// ```text
/// [-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
/// |\.[0-9_]+(?:[eE][-+][0-9]+)?
/// |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
/// |[-+]?\.(?:inf|Inf|INF)
/// |\.(?:nan|NaN|NAN)
/// ```
fn parse_float(text: &str) -> Option<Scalar> {
    let (negative, body) = split_sign(text);
    let signed = body.len() != text.len();
    match body {
        ".inf" | ".Inf" | ".INF" => {
            return Some(Scalar::NonFinite(if negative {
                NonFinite::NegInfinity
            } else {
                NonFinite::Infinity
            }));
        }
        // Note: no sign is allowed on `.nan` — `+.nan` and `-.nan` are strings.
        ".nan" | ".NaN" | ".NAN" if !signed => return Some(Scalar::NonFinite(NonFinite::Nan)),
        _ => {}
    }
    // Sexagesimal float: `1:30.5`. The dot is required — without it the scalar
    // is a sexagesimal *integer*, which the int resolver claims first.
    if body.contains(':') {
        let mut total = parse_sexagesimal_float(body)?;
        if negative {
            total = -total;
        }
        return Some(Scalar::Float(total));
    }
    let (mantissa, exponent) = match body.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (mantissa, Some(exponent)),
        None => (body, None),
    };
    if let Some(exponent) = exponent {
        // PyYAML requires the sign: `1.5e3` is a string, `1.5e+3` is a float.
        let digits = exponent.strip_prefix(['-', '+'])?;
        if digits.is_empty() || !digits.chars().all(|ch| ch.is_ascii_digit()) {
            return None;
        }
    }
    let (whole, fraction) = mantissa.split_once('.')?;
    if !whole.chars().all(|ch| ch.is_ascii_digit() || ch == '_')
        || !fraction.chars().all(|ch| ch.is_ascii_digit() || ch == '_')
    {
        return None;
    }
    if whole.is_empty() && fraction.is_empty() {
        return None;
    }
    let cleaned: String = text.chars().filter(|ch| *ch != '_').collect();
    let value: f64 = cleaned.parse().ok()?;
    Some(Scalar::Float(value))
}

/// `1:30.5` → 90.5. The last part carries the fraction, so it is one number
/// (`30.5`), not a base-60 digit followed by a decimal one.
fn parse_sexagesimal_float(body: &str) -> Option<f64> {
    let (head, tail) = body.rsplit_once(':')?;
    let (last, fraction) = tail.split_once('.')?;
    if fraction.is_empty() || !fraction.chars().all(|ch| ch.is_ascii_digit() || ch == '_') {
        return None;
    }
    let mut parts: Vec<f64> = Vec::new();
    for part in head.split(':') {
        if part.is_empty() || !part.chars().all(|ch| ch.is_ascii_digit() || ch == '_') {
            return None;
        }
        parts.push(part.replace('_', "").parse().ok()?);
    }
    if last.is_empty() || !last.chars().all(|ch| ch.is_ascii_digit() || ch == '_') {
        return None;
    }
    parts.push(
        format!("{}.{}", last.replace('_', ""), fraction.replace('_', ""))
            .parse()
            .ok()?,
    );
    // Every part but the first is a base-60 digit: `[0-5]?[0-9]`.
    if parts.iter().skip(1).any(|part| *part > 59.0) {
        return None;
    }
    let mut total = 0.0;
    let mut base = 1.0;
    for part in parts.iter().rev() {
        total += part * base;
        base *= 60.0;
    }
    Some(total)
}

/// PyYAML's integer pattern:
///
/// ```text
/// [-+]?0b[0-1_]+
/// |[-+]?0[0-7_]+
/// |[-+]?(?:0|[1-9][0-9_]*)
/// |[-+]?0x[0-9a-fA-F_]+
/// |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+
/// ```
fn parse_int(text: &str) -> Option<i64> {
    let (negative, body) = split_sign(text);
    if body.is_empty() {
        return None;
    }
    let magnitude = if let Some(rest) = body.strip_prefix("0b") {
        if rest.is_empty() || !rest.chars().all(|ch| matches!(ch, '0' | '1' | '_')) {
            return None;
        }
        parse_radix(rest, 2)?
    } else if let Some(rest) = body.strip_prefix("0x") {
        if rest.is_empty() || !rest.chars().all(|ch| ch.is_ascii_hexdigit() || ch == '_') {
            return None;
        }
        parse_radix(rest, 16)?
    } else if body.len() > 1
        && body.starts_with('0')
        && body
            .chars()
            .all(|ch| ('0'..='7').contains(&ch) || ch == '_')
    {
        // A leading zero means octal, so `007` is 7 and `010` is 8 — but `08`
        // fails this branch and falls through to a string.
        parse_radix(&body[1..], 8)?
    } else if body == "0" {
        0
    } else if body.starts_with(|ch: char| ('1'..='9').contains(&ch))
        && body.chars().all(|ch| ch.is_ascii_digit() || ch == '_')
    {
        parse_radix(body, 10)?
    } else if body.contains(':') {
        parse_sexagesimal_int(body)?
    } else {
        return None;
    };
    if negative {
        Some(-magnitude)
    } else {
        Some(magnitude)
    }
}

fn parse_sexagesimal_int(body: &str) -> Option<i64> {
    let mut parts = body.split(':');
    let first = parts.next()?;
    if first.is_empty()
        || !first.starts_with(|ch: char| ('1'..='9').contains(&ch))
        || !first.chars().all(|ch| ch.is_ascii_digit() || ch == '_')
    {
        return None;
    }
    let mut total = first.replace('_', "").parse::<i64>().ok()?;
    let mut count = 0usize;
    for part in parts {
        if part.is_empty() || part.len() > 2 || !part.chars().all(|ch| ch.is_ascii_digit()) {
            return None;
        }
        let value: i64 = part.parse().ok()?;
        if value > 59 {
            return None;
        }
        total = total.checked_mul(60)?.checked_add(value)?;
        count += 1;
    }
    (count > 0).then_some(total)
}

fn parse_radix(digits: &str, radix: u32) -> Option<i64> {
    let cleaned: String = digits.chars().filter(|ch| *ch != '_').collect();
    if cleaned.is_empty() {
        // `0_` is 0 in PyYAML: the underscores are stripped before the parse.
        return Some(0);
    }
    i64::from_str_radix(&cleaned, radix).ok()
}

fn split_sign(text: &str) -> (bool, &str) {
    match text.as_bytes().first() {
        Some(b'-') => (true, &text[1..]),
        Some(b'+') => (false, &text[1..]),
        _ => (false, text),
    }
}

/// PyYAML's timestamp pattern, rendered the way `yaml.dump` writes it back.
///
/// A date stays `2026-01-01`; a datetime becomes `2026-01-01 00:00:00+00:00`
/// (`datetime.isoformat(' ')`), with microseconds only when they are non-zero.
///
/// `None` means "not a timestamp"; `Some(Err(()))` means "shaped like one, but
/// the date or clock is out of range", which PyYAML's constructor rejects.
fn parse_timestamp(text: &str) -> Option<Result<String, ()>> {
    let bytes = text.as_bytes();
    if bytes.len() < 10 || !bytes[..4].iter().all(u8::is_ascii_digit) || bytes[4] != b'-' {
        return None;
    }
    let year: i32 = text[..4].parse().ok()?;
    let mut index = 5;
    let (month, month_digits) = read_digits(bytes, &mut index, 2)?;
    if bytes.get(index) != Some(&b'-') {
        return None;
    }
    index += 1;
    let (day, day_digits) = read_digits(bytes, &mut index, 2)?;
    if index == bytes.len() {
        // The date-only alternative of PyYAML's pattern requires two digits.
        if month_digits != 2 || day_digits != 2 {
            return None;
        }
        return Some(if valid_date(year, month, day) {
            Ok(format!("{year:04}-{month:02}-{day:02}"))
        } else {
            Err(())
        });
    }
    // `T`, `t`, or a run of spaces/tabs separates the date from the time.
    let separator = index;
    while index < bytes.len() && matches!(bytes[index], b' ' | b'\t') {
        index += 1;
    }
    if index == separator {
        if matches!(bytes[index], b'T' | b't') {
            index += 1;
        } else {
            return None;
        }
    }
    let (hour, _) = read_digits(bytes, &mut index, 2)?;
    if bytes.get(index) != Some(&b':') {
        return None;
    }
    index += 1;
    let (minute, minute_digits) = read_digits(bytes, &mut index, 2)?;
    if minute_digits != 2 || bytes.get(index) != Some(&b':') {
        return None;
    }
    index += 1;
    let (second, second_digits) = read_digits(bytes, &mut index, 2)?;
    if second_digits != 2 {
        return None;
    }
    let mut micros = 0u32;
    if bytes.get(index) == Some(&b'.') {
        index += 1;
        let start = index;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        // PyYAML: `int(fraction[:6].ljust(6, '0'))`.
        let fraction = &text[start..index];
        let mut padded = fraction[..fraction.len().min(6)].to_string();
        while padded.len() < 6 {
            padded.push('0');
        }
        micros = padded.parse().ok()?;
    }
    while index < bytes.len() && matches!(bytes[index], b' ' | b'\t') {
        index += 1;
    }
    let mut offset: Option<(bool, u32, u32)> = None;
    if index < bytes.len() {
        match bytes[index] {
            b'Z' => {
                offset = Some((false, 0, 0));
                index += 1;
            }
            sign @ (b'+' | b'-') => {
                index += 1;
                let (hours, _) = read_digits(bytes, &mut index, 2)?;
                let mut minutes = 0;
                if bytes.get(index) == Some(&b':') {
                    index += 1;
                    let (value, digits) = read_digits(bytes, &mut index, 2)?;
                    if digits != 2 {
                        return None;
                    }
                    minutes = value;
                }
                offset = Some((sign == b'-', hours, minutes));
            }
            _ => return None,
        }
    }
    if index != bytes.len() {
        return None;
    }
    if !valid_date(year, month, day) || hour > 23 || minute > 59 || second > 59 {
        return Some(Err(()));
    }
    let mut out = format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}");
    if micros != 0 {
        out.push_str(&format!(".{micros:06}"));
    }
    if let Some((negative, hours, minutes)) = offset {
        out.push(if negative { '-' } else { '+' });
        out.push_str(&format!("{hours:02}:{minutes:02}"));
    }
    Some(Ok(out))
}

fn valid_date(year: i32, month: u32, day: u32) -> bool {
    if !(1..=12).contains(&month) || day == 0 {
        return false;
    }
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return false,
    };
    day <= days
}

/// Read one or two digits, returning the value and how many digits were read.
fn read_digits(bytes: &[u8], index: &mut usize, max: usize) -> Option<(u32, usize)> {
    let start = *index;
    let mut value = 0u32;
    while *index < bytes.len() && *index - start < max && bytes[*index].is_ascii_digit() {
        value = value * 10 + u32::from(bytes[*index] - b'0');
        *index += 1;
    }
    let count = *index - start;
    (count > 0).then_some((value, count))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kind(text: &str) -> &'static str {
        match resolve_plain(text) {
            None => "string",
            Some(Scalar::Null) => "null",
            Some(Scalar::Bool(_)) => "bool",
            Some(Scalar::Int(_)) => "int",
            Some(Scalar::Float(_)) => "float",
            Some(Scalar::NonFinite(_)) => "non-finite",
            Some(Scalar::Timestamp(_)) => "timestamp",
            Some(Scalar::InvalidTimestamp) => "invalid-timestamp",
            Some(Scalar::Merge) => "merge",
            Some(Scalar::Value) => "value",
            Some(Scalar::Yaml) => "yaml",
        }
    }

    #[test]
    fn booleans_are_yaml11_but_not_y_and_n() {
        for text in [
            "yes", "Yes", "YES", "on", "On", "ON", "true", "True", "TRUE",
        ] {
            assert_eq!(resolve_plain(text), Some(Scalar::Bool(true)), "{text}");
        }
        for text in [
            "no", "No", "NO", "off", "Off", "OFF", "false", "False", "FALSE",
        ] {
            assert_eq!(resolve_plain(text), Some(Scalar::Bool(false)), "{text}");
        }
        assert_eq!(kind("y"), "string");
        assert_eq!(kind("n"), "string");
        assert_eq!(kind("Y"), "string");
        assert_eq!(kind("N"), "string");
    }

    #[test]
    fn integers_follow_pyyaml_not_yaml12() {
        assert_eq!(resolve_plain("007"), Some(Scalar::Int(7)));
        assert_eq!(resolve_plain("010"), Some(Scalar::Int(8)));
        assert_eq!(resolve_plain("1_000"), Some(Scalar::Int(1000)));
        assert_eq!(resolve_plain("0x1f"), Some(Scalar::Int(31)));
        assert_eq!(resolve_plain("0b101"), Some(Scalar::Int(5)));
        assert_eq!(resolve_plain("-17"), Some(Scalar::Int(-17)));
        assert_eq!(resolve_plain("1:30"), Some(Scalar::Int(90)));
        assert_eq!(resolve_plain("1:30:30"), Some(Scalar::Int(5430)));
        // Not integers in PyYAML.
        assert_eq!(kind("08"), "string");
        assert_eq!(kind("0o17"), "string");
        assert_eq!(kind("0:30"), "string");
        assert_eq!(kind("1:60"), "string");
    }

    #[test]
    fn floats_need_a_dot_and_a_signed_exponent() {
        assert_eq!(resolve_plain("1.5"), Some(Scalar::Float(1.5)));
        assert_eq!(resolve_plain(".5"), Some(Scalar::Float(0.5)));
        assert_eq!(resolve_plain("1.5e+3"), Some(Scalar::Float(1500.0)));
        assert_eq!(resolve_plain("1:30.5"), Some(Scalar::Float(90.5)));
        assert_eq!(kind("1e3"), "string");
        assert_eq!(kind("1.5e3"), "string");
        assert_eq!(kind("1.5.5"), "string");
    }

    #[test]
    fn non_finite_floats_are_floats() {
        assert_eq!(
            resolve_plain(".inf"),
            Some(Scalar::NonFinite(NonFinite::Infinity))
        );
        assert_eq!(
            resolve_plain("-.inf"),
            Some(Scalar::NonFinite(NonFinite::NegInfinity))
        );
        assert_eq!(
            resolve_plain(".nan"),
            Some(Scalar::NonFinite(NonFinite::Nan))
        );
        assert_eq!(kind("+.nan"), "string");
    }

    #[test]
    fn timestamps_render_the_way_pyyaml_dumps_them() {
        assert_eq!(
            resolve_plain("2026-01-01"),
            Some(Scalar::Timestamp("2026-01-01".to_string()))
        );
        assert_eq!(
            resolve_plain("2026-01-01T00:00:00Z"),
            Some(Scalar::Timestamp("2026-01-01 00:00:00+00:00".to_string()))
        );
        assert_eq!(
            resolve_plain("2026-01-01 00:00:00+02:00"),
            Some(Scalar::Timestamp("2026-01-01 00:00:00+02:00".to_string()))
        );
        assert_eq!(
            resolve_plain("2026-01-01 00:00:00.5"),
            Some(Scalar::Timestamp("2026-01-01 00:00:00.500000".to_string()))
        );
        // A one-digit month/day needs a time part to be a timestamp.
        assert_eq!(kind("2026-1-1"), "string");
        assert_eq!(
            resolve_plain("2026-1-1 00:00:00"),
            Some(Scalar::Timestamp("2026-01-01 00:00:00".to_string()))
        );
        assert_eq!(kind("2026-13-01"), "invalid-timestamp");
    }

    #[test]
    fn nulls_merge_and_value_keys() {
        for text in ["", "~", "null", "Null", "NULL"] {
            assert_eq!(resolve_plain(text), Some(Scalar::Null), "{text:?}");
        }
        assert_eq!(resolve_plain("<<"), Some(Scalar::Merge));
        assert_eq!(resolve_plain("="), Some(Scalar::Value));
        assert_eq!(kind("nULL"), "string");
    }
}
