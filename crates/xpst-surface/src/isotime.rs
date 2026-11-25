//! `datetime.fromisoformat` — Python's lenient ISO-8601 reader.
//!
//! `xpst_schedule_add` passes its `scheduled_time` argument straight into
//! `datetime.fromisoformat` and catches only `ValueError`, so the parser's
//! accept/reject set *and* its error sentences are part of the wire contract: a
//! value this port accepts and Python rejects (or the reverse) is a different
//! answer to an agent, and a different `Bad scheduled_time (need ISO-8601): …`
//! body.
//!
//! The grammar below is Python 3.13's, captured by
//! `xtask/scripts/capture_schedule_time.py` and pinned by
//! `tests/schedule_time.rs` — never transcribed from the documentation:
//!
//! * calendar `YYYY-MM-DD` / `YYYYMMDD`, and ISO week `YYYY-Www[-D]` /
//!   `YYYYWww[D]`. Ordinal dates (`YYYY-DDD`, `YYYYDDD`) are **rejected** by
//!   3.13 even though ISO-8601 defines them;
//! * `T`, `t` or a space separator, then `HH`, `HH:MM`, `HH:MM:SS` (or the
//!   basic `HHMM` / `HHMMSS` forms);
//! * a fraction after `.` or `,`, truncated to six digits (never rounded);
//! * exactly one offset: `Z` (uppercase only) or `±HH[:MM[:SS[.ffffff]]]` /
//!   `±HHMM[SS[.ffffff]]`.
//!
//! Two error families, and they are different documents:
//!
//! * a shape failure is `Invalid isoformat string: '<raw>'` — the raw text,
//!   `repr`-quoted;
//! * a range failure is the `datetime` constructor's own sentence
//!   (`month must be in 1..12`, `hour must be in 0..23`, …), and an
//!   out-of-range offset names the `timedelta` it built.

use jiff::civil::DateTime as CivilDateTime;

use xpst_config::output::python_repr;

/// The offset a caller wrote, at second + microsecond precision.
///
/// Python's `timezone` accepts a `timedelta`, so `+05:30:15.5` is a legal
/// offset; jiff's `Offset` is second-granular, so the sub-second part is kept
/// here and applied when the instant is computed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Offset {
    pub seconds: i64,
    pub micros: i32,
}

/// A parsed `datetime.fromisoformat` value: the wall clock plus the offset the
/// caller wrote (`None` = naive, to be read in the caller's zone).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ParsedStamp {
    pub civil: CivilDateTime,
    pub offset: Option<Offset>,
}

impl ParsedStamp {
    /// `datetime.isoformat()` — the rendering the capture fixture compares.
    ///
    /// The fraction is six digits when the microsecond field is non-zero and
    /// absent when it is zero; the offset is `±HH:MM`, gaining `:SS` and then
    /// `.ffffff` only when those parts are non-zero.
    #[must_use]
    pub fn isoformat(&self) -> String {
        let mut out = self.civil.strftime("%Y-%m-%dT%H:%M:%S").to_string();
        let micros = self.civil.subsec_nanosecond() / 1_000;
        if micros != 0 {
            out.push_str(&format!(".{micros:06}"));
        }
        if let Some(offset) = self.offset {
            let sign = if offset.seconds < 0 || offset.micros < 0 {
                '-'
            } else {
                '+'
            };
            let total = offset.seconds.abs();
            let micros = offset.micros.abs();
            out.push_str(&format!(
                "{sign}{:02}:{:02}",
                total / 3600,
                (total % 3600) / 60
            ));
            if total % 60 != 0 || micros != 0 {
                out.push_str(&format!(":{:02}", total % 60));
            }
            if micros != 0 {
                out.push_str(&format!(".{micros:06}"));
            }
        }
        out
    }
}

/// `datetime.fromisoformat(raw)`.
///
/// `Err` carries the `ValueError` message Python raises, so a handler can serve
/// `Bad scheduled_time (need ISO-8601): <message>` byte for byte.
pub fn fromisoformat(raw: &str) -> Result<ParsedStamp, String> {
    let invalid = || format!("Invalid isoformat string: {}", python_repr(raw));
    let text = raw.as_bytes();
    let (date, rest) = parse_date(text).ok_or_else(invalid)?;
    let (hour, minute, second, micro, offset) = match rest {
        None => (0, 0, 0, 0, None),
        Some(time) => parse_time(time).ok_or_else(invalid)?,
    };
    let civil = build_civil(date, hour, minute, second, micro)?;
    Ok(ParsedStamp { civil, offset })
}

/// `(year, month, day)` as the parser read them, before range validation.
type RawDate = (i16, i64, i64);

/// `(hour, minute, second, microsecond, offset)`.
type RawTime = (i64, i64, i64, i32, Option<Offset>);

/// Split the date off the front; the remainder is the time part (if any).
fn parse_date(text: &[u8]) -> Option<(RawDate, Option<&[u8]>)> {
    if text.len() < 7 {
        return None;
    }
    let year = number(text, 0, 4)? as i16;
    let (year, month, day, consumed) = match text[4] {
        b'-' if text.get(5) == Some(&b'W') => {
            // `YYYY-Www[-D]`
            let week = number(text, 6, 2)?;
            // A `-` introduces the day; anything else is the date/time
            // separator, so `2026-W01X09:30:00` is a valid stamp.
            let (day, consumed) = match text.get(8) {
                None => (1, 8),
                Some(b'-') => (number(text, 9, 1)?, 10),
                Some(_) => (1, 8),
            };
            let (year, month, day) = week_to_gregorian(year, week, day)?;
            (year, month, day, consumed)
        }
        b'-' => {
            // `YYYY-MM-DD`
            if text.get(7) != Some(&b'-') {
                return None;
            }
            (year, number(text, 5, 2)?, number(text, 8, 2)?, 10)
        }
        b'W' => {
            // `YYYYWww[D]`
            let week = number(text, 5, 2)?;
            // A digit is the day; anything else is the date/time separator.
            let (day, consumed) = match text.get(7) {
                None => (1, 7),
                Some(byte) if byte.is_ascii_digit() => (number(text, 7, 1)?, 8),
                Some(_) => (1, 7),
            };
            let (year, month, day) = week_to_gregorian(year, week, day)?;
            (year, month, day, consumed)
        }
        _ => {
            // `YYYYMMDD`
            (year, number(text, 4, 2)?, number(text, 6, 2)?, 8)
        }
    };
    let rest = &text[consumed..];
    match rest.first() {
        None => Some(((year, month, day), None)),
        // The date/time separator is ANY single character, not just `T`/`t`/` `:
        // the shipped parser consumes one byte and hands the rest to the time
        // parser, so `2026-12-01X09:30:00` is a valid stamp. A parser that
        // insists on `T` refuses a value the engine schedules.
        Some(_) => Some(((year, month, day), Some(&rest[1..]))),
    }
}

/// `_isoweek_to_gregorian`: an ISO week date to a calendar date.
///
/// Week 1 is the week holding January 4th, so the arithmetic is anchored there
/// rather than on January 1st. The week must EXIST in that year — 2025 has 52
/// weeks and 2026 has 53, so `2025-W53` is refused while `2026-W53` is not —
/// and a day past the last one rolls into the next year, exactly as Python's
/// ordinal arithmetic does.
fn week_to_gregorian(year: i16, week: i64, day: i64) -> Option<(i16, i64, i64)> {
    if !(1..=7).contains(&day) {
        return None;
    }
    let jan4 = ymd_to_ordinal(year, 1, 4)?;
    // Monday = 0, matching `(jan4 + 6) % 7` on a proleptic ordinal.
    let jan4_weekday = (jan4 + 6).rem_euclid(7);
    let week1_monday = jan4 - jan4_weekday;
    // The last ISO week is the one holding December 28th.
    let last_week = (ymd_to_ordinal(year, 12, 28)? - week1_monday) / 7 + 1;
    if !(1..=last_week).contains(&week) {
        return None;
    }
    let ordinal = week1_monday + (week - 1) * 7 + (day - 1);
    ordinal_to_ymd(ordinal)
}

/// `_ymd2ord` — days since 0001-01-01 (proleptic Gregorian), 1-based.
fn ymd_to_ordinal(year: i16, month: i8, day: i8) -> Option<i64> {
    let year = i64::from(year);
    let month = i64::from(month);
    let day = i64::from(day);
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    let days_before_month = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
    let mut ordinal = (year - 1) * 365 + (year - 1) / 4 - (year - 1) / 100 + (year - 1) / 400;
    ordinal += days_before_month[(month - 1) as usize];
    if month > 2 && is_leap(year) {
        ordinal += 1;
    }
    ordinal += day;
    Some(ordinal)
}

/// `_ord2ymd` — the inverse of [`ymd_to_ordinal`], as `(year, month, day)`.
///
/// The year is part of the answer: an ISO week date near a year boundary lands
/// in the neighbouring year (`2026-W01` starts on 2025-12-29), so returning only
/// the month and day would silently keep the caller's year.
fn ordinal_to_ymd(ordinal: i64) -> Option<(i16, i64, i64)> {
    if ordinal < 1 {
        return None;
    }
    // 400-year cycles of 146097 days, then 100/4/1-year steps.
    let n = ordinal - 1;
    let cycles = n / 146_097;
    let mut rem = n % 146_097;
    let mut year = cycles * 400 + 1;
    for (span, step) in [(36524, 100), (1461, 4), (365, 1)] {
        let count = rem / span;
        rem %= span;
        year += count * step;
    }
    // `rem` is now the day-of-year offset inside `year`.
    let mut month = 1i64;
    let mut day = rem + 1;
    let lengths = month_lengths(year);
    for (index, length) in lengths.iter().enumerate() {
        if day <= *length {
            month = index as i64 + 1;
            break;
        }
        day -= *length;
    }
    Some((year as i16, month, day))
}

fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn month_lengths(year: i64) -> [i64; 12] {
    let february = if is_leap(year) { 29 } else { 28 };
    [31, february, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
}

/// Split the time part: `HH[:MM[:SS[.ffffff]]]` plus an optional offset.
fn parse_time(text: &[u8]) -> Option<RawTime> {
    let hour = number(text, 0, 2)?;
    let (minute, second, micro, rest) = match text.get(2) {
        None => (0, 0, 0, &text[2..]),
        Some(b':') => {
            let minute = number(text, 3, 2)?;
            match text.get(5) {
                None => (minute, 0, 0, &text[5..]),
                Some(b':') => {
                    let second = number(text, 6, 2)?;
                    let (micro, rest) = fraction(&text[8..], true)?;
                    (minute, second, micro, rest)
                }
                Some(_) => return None,
            }
        }
        Some(_) => {
            // Basic form: `HHMM[SS[.ffffff]]`.
            let minute = number(text, 2, 2)?;
            let (second, micro, rest) = match text.get(4) {
                None => (0, 0, &text[4..]),
                Some(_) => {
                    let second = number(text, 4, 2)?;
                    let (micro, rest) = fraction(&text[6..], false)?;
                    (second, micro, rest)
                }
            };
            (minute, second, micro, rest)
        }
    };
    let offset = parse_offset(rest)?;
    Some((hour, minute, second, micro, offset))
}

/// A fraction, truncated to six digits. Returns the microseconds and the
/// remainder after the digits.
///
/// The separators are `.`, `,` and — in the EXTENDED forms only — `:`, which is
/// what the shipped parser accepts: `09:30:00:5` is half a second, while
/// `093000:5` and `+053015:5` are refused. A hand-written parser that allows
/// only `.` rejects a value the engine schedules.
fn fraction(text: &[u8], allow_colon: bool) -> Option<(i32, &[u8])> {
    match text.first() {
        Some(b'.' | b',') => {}
        Some(b':') if allow_colon => {}
        _ => return Some((0, text)),
    }
    let digits = text[1..]
        .iter()
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    if digits == 0 {
        return None;
    }
    let mut micro = 0i32;
    for (index, byte) in text[1..1 + digits.min(6)].iter().enumerate() {
        micro += i32::from(byte - b'0') * 10i32.pow(5 - index as u32);
    }
    Some((micro, &text[1 + digits..]))
}

/// `Z` or `±HH[:MM[:SS[.ffffff]]]` / `±HHMM[SS[.ffffff]]`, and nothing else.
///
/// Each branch validates its own remainder, so there is no separate length
/// bookkeeping to get wrong: a trailing `Z`, a second offset group or a stray
/// separator all leave a non-empty remainder and are refused.
fn parse_offset(text: &[u8]) -> Option<Option<Offset>> {
    if text.is_empty() {
        return Some(None);
    }
    if text == b"Z" {
        return Some(Some(Offset {
            seconds: 0,
            micros: 0,
        }));
    }
    let sign = match text[0] {
        b'+' => 1i64,
        b'-' => -1i64,
        _ => return None,
    };
    let body = &text[1..];
    let (hours, minutes, seconds, micros) = match body.get(2) {
        None => (number(body, 0, 2)?, 0, 0, 0),
        Some(b':') => {
            let minutes = number(body, 3, 2)?;
            match body.get(5) {
                None => (number(body, 0, 2)?, minutes, 0, 0),
                Some(b':') => {
                    let seconds = number(body, 6, 2)?;
                    let (micros, rest) = fraction(&body[8..], true)?;
                    if !rest.is_empty() {
                        return None;
                    }
                    (number(body, 0, 2)?, minutes, seconds, micros)
                }
                Some(_) => return None,
            }
        }
        Some(_) => {
            let minutes = number(body, 2, 2)?;
            match body.get(4) {
                None => (number(body, 0, 2)?, minutes, 0, 0),
                Some(_) => {
                    let seconds = number(body, 4, 2)?;
                    let (micros, rest) = fraction(&body[6..], false)?;
                    if !rest.is_empty() {
                        return None;
                    }
                    (number(body, 0, 2)?, minutes, seconds, micros)
                }
            }
        }
    };
    let total = hours * 3600 + minutes * 60 + seconds;
    Some(Some(Offset {
        seconds: sign * total,
        micros: sign as i32 * micros,
    }))
}

/// `n` ASCII digits at `start`, as an integer.
fn number(text: &[u8], start: usize, width: usize) -> Option<i64> {
    let slice = text.get(start..start + width)?;
    let mut value = 0i64;
    for byte in slice {
        if !byte.is_ascii_digit() {
            return None;
        }
        value = value * 10 + i64::from(byte - b'0');
    }
    Some(value)
}

/// The `datetime` constructor: range checks in CPython's order, with its
/// sentences.
fn build_civil(
    date: RawDate,
    hour: i64,
    minute: i64,
    second: i64,
    micro: i32,
) -> Result<CivilDateTime, String> {
    let (year, month, day) = date;
    if !(1..=12).contains(&month) {
        return Err("month must be in 1..12".to_string());
    }
    let length = month_lengths(i64::from(year))[(month - 1) as usize];
    if day < 1 || day > length {
        return Err("day is out of range for month".to_string());
    }
    if !(0..=23).contains(&hour) {
        return Err("hour must be in 0..23".to_string());
    }
    if !(0..=59).contains(&minute) {
        return Err("minute must be in 0..59".to_string());
    }
    if !(0..=59).contains(&second) {
        return Err("second must be in 0..59".to_string());
    }
    CivilDateTime::new(
        year,
        month as i8,
        day as i8,
        hour as i8,
        minute as i8,
        second as i8,
        micro * 1_000,
    )
    .map_err(|_| "microsecond must be in 0..999999".to_string())
}

/// The `timezone(timedelta)` range check, with Python's `timedelta` repr.
///
/// `datetime.timezone` refuses an offset of exactly ±24h or more, and the
/// sentence names the `timedelta` it built — so the message is a function of the
/// offset, not a fixed string.
pub fn offset_range_error(offset: Offset) -> Option<String> {
    let total_micros = offset.seconds * 1_000_000 + i64::from(offset.micros);
    const DAY: i64 = 86_400 * 1_000_000;
    if total_micros > -DAY && total_micros < DAY {
        return None;
    }
    // `timedelta` normalises to days + 0..86399s + 0..999999µs.
    let days = total_micros.div_euclid(DAY);
    let rest = total_micros.rem_euclid(DAY);
    let seconds = rest / 1_000_000;
    let micros = rest % 1_000_000;
    let mut args: Vec<String> = Vec::new();
    if days != 0 {
        args.push(format!("days={days}"));
    }
    if seconds != 0 {
        args.push(format!("seconds={seconds}"));
    }
    if micros != 0 {
        args.push(format!("microseconds={micros}"));
    }
    if args.is_empty() {
        args.push("0".to_string());
    }
    Some(format!(
        "offset must be a timedelta strictly between -timedelta(hours=24) and \
         timedelta(hours=24), not datetime.timedelta({}).",
        args.join(", ")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn calendar_and_week_dates_parse() {
        assert_eq!(
            fromisoformat("2026-12-01").unwrap().civil.to_string(),
            "2026-12-01T00:00:00"
        );
        assert_eq!(
            fromisoformat("20261201").unwrap().civil.to_string(),
            "2026-12-01T00:00:00"
        );
        // 2026-W01 starts on 2025-12-29 (week 1 holds January 4th).
        assert_eq!(
            fromisoformat("2026-W01").unwrap().civil.to_string(),
            "2025-12-29T00:00:00"
        );
        assert_eq!(
            fromisoformat("2026W011").unwrap().civil.to_string(),
            "2025-12-29T00:00:00"
        );
        // Ordinal dates are ISO-8601 but not `fromisoformat`'s.
        assert!(fromisoformat("2026-001").is_err());
        assert!(fromisoformat("2026001").is_err());
    }

    #[test]
    fn fractions_truncate_and_offsets_keep_their_seconds() {
        let stamp = fromisoformat("2026-12-01T09:30:00.1234567+05:30:15.5").unwrap();
        assert_eq!(stamp.civil.to_string(), "2026-12-01T09:30:00.123456");
        assert_eq!(
            stamp.offset,
            Some(Offset {
                seconds: 19_815,
                micros: 500_000
            })
        );
    }

    #[test]
    fn range_failures_use_pythons_sentences() {
        assert_eq!(
            fromisoformat("2026-13-01T09:30:00").unwrap_err(),
            "month must be in 1..12"
        );
        assert_eq!(
            fromisoformat("2026-02-30T09:30:00").unwrap_err(),
            "day is out of range for month"
        );
        assert_eq!(
            fromisoformat("2026-12-01T24:00:00").unwrap_err(),
            "hour must be in 0..23"
        );
        assert_eq!(
            fromisoformat("not-a-time").unwrap_err(),
            "Invalid isoformat string: 'not-a-time'"
        );
    }

    #[test]
    fn an_out_of_range_offset_names_its_timedelta() {
        let stamp = fromisoformat("2026-12-01T09:30:00+24:00").unwrap();
        assert_eq!(
            offset_range_error(stamp.offset.unwrap()).as_deref(),
            Some(
                "offset must be a timedelta strictly between -timedelta(hours=24) and \
                 timedelta(hours=24), not datetime.timedelta(days=1)."
            )
        );
        let stamp = fromisoformat("2026-12-01T09:30:00+99:00").unwrap();
        assert_eq!(
            offset_range_error(stamp.offset.unwrap()).as_deref(),
            Some(
                "offset must be a timedelta strictly between -timedelta(hours=24) and \
                 timedelta(hours=24), not datetime.timedelta(days=4, seconds=10800)."
            )
        );
        let stamp = fromisoformat("2026-12-01T09:30:00+23:59:59.999999").unwrap();
        assert!(offset_range_error(stamp.offset.unwrap()).is_none());
    }
}
