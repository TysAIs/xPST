//! The two timestamp shapes this surface reads, and the one it renders.
//!
//! Both stores (`schedule.json`, `rate_cooldowns.json`) are written by the
//! Python engine as `datetime.isoformat()` values: `YYYY-MM-DDTHH:MM:SS` with
//! an optional fraction, and — for the aware ones — a numeric offset.
//!
//! The shape check is explicit rather than trusting the time library's parser,
//! which is deliberately more lenient than these stores are: it accepts a bare
//! date, an `HH:MM`, and a datetime with a trailing offset that it then
//! *silently discards*. For a display field that difference is a confidently
//! wrong timestamp instead of the `null` that says "cannot tell", so the width
//! and the separators are pinned here once, for both tools.
//!
//! Rendering is pinned the other way: `scheduled_time_local` mirrors Python's
//! `datetime.isoformat()`, which is the contract the MCP surface was ported
//! against — `2026-11-20T04:00:00-05:00`, and with six fractional digits only
//! when the microsecond field is non-zero.

use jiff::civil::DateTime as CivilDateTime;
use jiff::Timestamp;

/// `YYYY-MM-DDTHH:MM:SS` — the fixed-width part every stored stamp shares.
const TEMPLATE: &[u8] = b"dddd-dd-ddTdd:dd:dd";

/// True when the first nineteen bytes are the stamp's fixed part.
fn fixed_part(text: &str) -> bool {
    text.len() >= TEMPLATE.len()
        && text
            .as_bytes()
            .iter()
            .zip(TEMPLATE)
            .all(|(byte, expected)| {
                if *expected == b'd' {
                    byte.is_ascii_digit()
                } else {
                    byte == expected
                }
            })
}

/// The fraction after the seconds, if any: `.` followed by one to nine digits.
///
/// Returns `None` when the remainder is neither empty nor a fraction, which is
/// what keeps a trailing offset or trailing garbage from being read as one.
fn fraction(rest: &str) -> Option<()> {
    if rest.is_empty() {
        return Some(());
    }
    let digits = rest.strip_prefix('.')?;
    if digits.is_empty() || digits.len() > 9 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some(())
}

/// Parse a naive wall-clock stamp (`YYYY-MM-DDTHH:MM:SS[.fraction]`).
pub(super) fn parse_naive(text: &str) -> Option<CivilDateTime> {
    if !fixed_part(text) {
        return None;
    }
    fraction(&text[TEMPLATE.len()..])?;
    text.parse::<CivilDateTime>().ok()
}

/// Parse an aware stamp: the same fixed part, a mandatory offset, nothing else.
pub(super) fn parse_instant(text: &str) -> Option<Timestamp> {
    if !fixed_part(text) {
        return None;
    }
    let rest = &text[TEMPLATE.len()..];
    let tail = match rest.strip_prefix('.') {
        Some(after_dot) => {
            let digits = after_dot.bytes().take_while(u8::is_ascii_digit).count();
            if digits == 0 || digits > 9 {
                return None;
            }
            &after_dot[digits..]
        }
        None => rest,
    };
    if tail.is_empty() {
        // No offset: this is a naive wall clock, not an instant.
        return None;
    }
    text.parse::<Timestamp>().ok()
}

/// A zoned value as `YYYY-MM-DDTHH:MM:SS[.ffffff]±HH:MM`.
pub(super) fn render(value: &jiff::ZonedDateTime) -> String {
    // A nanosecond-only instant is 0 µs, and `isoformat()` drops the fraction
    // in that case; anything with microseconds renders all six digits.
    if value.subsec_nanosecond() / 1_000 == 0 {
        value.strftime("%Y-%m-%dT%H:%M:%S%:z").to_string()
    } else {
        value.strftime("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_fixed_part_is_required() {
        assert!(parse_naive("2026-11-20T09:00:00").is_some());
        // A bare date and an hour:minute are what the time library accepts and
        // this surface must not.
        assert!(parse_naive("2026-11-20").is_none());
        assert!(parse_naive("2026-11-20T09:00").is_none());
        assert!(parse_naive("2026-11-20 09:00:00").is_none());
        assert!(parse_naive("not-a-time").is_none());
        assert!(parse_naive("").is_none());
    }

    #[test]
    fn fractions_are_bounded() {
        assert!(parse_naive("2026-11-20T09:00:00.5").is_some());
        assert!(parse_naive("2026-11-20T09:00:00.123456789").is_some());
        assert!(parse_naive("2026-11-20T09:00:00.1234567890").is_none());
        assert!(parse_naive("2026-11-20T09:00:00.").is_none());
        assert!(parse_naive("2026-11-20T09:00:00.abc").is_none());
    }

    #[test]
    fn an_instant_needs_an_offset() {
        assert!(parse_instant("2026-11-20T09:00:00+00:00").is_some());
        assert!(parse_instant("2026-11-20T09:00:00Z").is_some());
        assert!(parse_instant("2026-11-20T04:00:00-05:00").is_some());
        assert!(parse_instant("2026-11-20T09:00:00").is_none());
        assert!(parse_instant("2026-11-20T09:00Z").is_none());
        assert!(parse_instant("2026-11-20T09:00:00Zx").is_none());
        assert!(parse_instant("2026-11-20").is_none());
    }
}
