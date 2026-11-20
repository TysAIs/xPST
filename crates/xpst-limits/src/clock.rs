//! Time arithmetic the policy needs, with no third-party dependency.
//!
//! Two things live here:
//!
//! * [`now_ms`] — the process clock, milliseconds since the Unix epoch.
//! * [`next_pacific_midnight_ms`] — the YouTube quota row says "stop until
//!   midnight PT", and that is a calendar instant, not a duration. Encoding it
//!   as "8 hours" would be a number nobody can defend across a DST boundary, so
//!   the US Pacific offset is computed from the federal DST rule instead:
//!   daylight time runs from the second Sunday in March to the first Sunday in
//!   November.
//!
//! All timestamps in this crate are `i64` milliseconds since the Unix epoch.
//! They come from whatever clock the caller passes in, which means they can go
//! backwards; every consumer of a stored timestamp is written to treat that as
//! "less time has passed than it claims", never as "more".

use std::time::{SystemTime, UNIX_EPOCH};

pub const SECOND_MS: i64 = 1_000;
pub const MINUTE_MS: i64 = 60 * SECOND_MS;
pub const HOUR_MS: i64 = 60 * MINUTE_MS;
pub const DAY_MS: i64 = 24 * HOUR_MS;

/// Milliseconds since the Unix epoch. Saturates at `0` on a clock set before
/// 1970 instead of wrapping around into the future.
#[must_use]
pub fn now_ms() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(elapsed) => i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX),
        Err(_) => 0,
    }
}

/// The next midnight on the US Pacific clock at or after `utc_ms`.
///
/// Used by the YouTube quota row: a `quotaExceeded` 403 clears at midnight
/// Pacific, so a cooldown that ends before then is a cooldown that ends too
/// early. The returned instant is strictly greater than `utc_ms`.
#[must_use]
pub fn next_pacific_midnight_ms(utc_ms: i64) -> i64 {
    let offset = pacific_offset_ms(utc_ms);
    let local_ms = utc_ms.saturating_add(offset);
    let local_day = local_ms.div_euclid(DAY_MS);
    let next_local_midnight = local_day.saturating_add(1).saturating_mul(DAY_MS);
    // Transitions happen at 02:00 local, never at midnight, so the offset at
    // the next midnight is the offset that date already has. Solve once, then
    // confirm.
    let candidate = next_local_midnight.saturating_sub(offset);
    let confirmed = next_local_midnight.saturating_sub(pacific_offset_ms(candidate));
    if confirmed > utc_ms {
        confirmed
    } else {
        candidate
    }
}

/// The UTC offset in milliseconds that the US Pacific zone has at `utc_ms`:
/// −7 h under daylight time, −8 h under standard time.
#[must_use]
pub fn pacific_offset_ms(utc_ms: i64) -> i64 {
    if is_pacific_daylight(utc_ms) {
        -7 * HOUR_MS
    } else {
        -8 * HOUR_MS
    }
}

/// US daylight time: from 02:00 local standard on the second Sunday in March
/// (10:00 UTC) to 02:00 local daylight on the first Sunday in November
/// (09:00 UTC).
#[must_use]
pub fn is_pacific_daylight(utc_ms: i64) -> bool {
    let year = civil_from_days(utc_ms.div_euclid(DAY_MS)).0;
    let start = days_from_civil(year, 3, nth_weekday_of_month(year, 3, 0, 2))
        .saturating_mul(DAY_MS)
        .saturating_add(10 * HOUR_MS);
    let end = days_from_civil(year, 11, nth_weekday_of_month(year, 11, 0, 1))
        .saturating_mul(DAY_MS)
        .saturating_add(9 * HOUR_MS);
    utc_ms >= start && utc_ms < end
}

/// Day of the month holding the `nth` (1-based) `weekday` (0 = Sunday).
fn nth_weekday_of_month(year: i64, month: u32, weekday: u32, nth: u32) -> u32 {
    let first = weekday_from_days(days_from_civil(year, month, 1));
    let offset = (weekday + 7 - first) % 7;
    1 + offset + (nth - 1) * 7
}

/// 0 = Sunday … 6 = Saturday. 1970-01-01 was a Thursday.
fn weekday_from_days(days: i64) -> u32 {
    u32::try_from((days % 7 + 7 + 4) % 7).unwrap_or(0)
}

/// Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's
/// `days_from_civil`).
pub(crate) fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = (if year >= 0 { year } else { year - 399 }) / 400;
    let year_of_era = year - era * 400;
    let month = i64::from(month);
    let day_of_year = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + i64::from(day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// Inverse of [`days_from_civil`].
pub(crate) fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let days = days + 719_468;
    let era = (if days >= 0 { days } else { days - 146_096 }) / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let mp = (5 * day_of_year + 2) / 153;
    let day = u32::try_from(day_of_year - (153 * mp + 2) / 5 + 1).unwrap_or(1);
    let month = u32::try_from(if mp < 10 { mp + 3 } else { mp - 9 }).unwrap_or(1);
    (if month <= 2 { year + 1 } else { year }, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(year: i64, month: u32, day: u32, hour: i64, minute: i64) -> i64 {
        days_from_civil(year, month, day) * DAY_MS + hour * HOUR_MS + minute * MINUTE_MS
    }

    #[test]
    fn civil_round_trip() {
        for (year, month, day) in [
            (1970, 1, 1),
            (2000, 2, 29),
            (2026, 11, 17),
            (2026, 3, 8),
            (2026, 11, 1),
            (2100, 12, 31),
        ] {
            let days = days_from_civil(year, month, day);
            assert_eq!(civil_from_days(days), (year, month, day));
        }
        assert_eq!(weekday_from_days(days_from_civil(1970, 1, 1)), 4);
        assert_eq!(weekday_from_days(days_from_civil(2026, 3, 8)), 0);
        assert_eq!(weekday_from_days(days_from_civil(2026, 11, 1)), 0);
    }

    #[test]
    fn dst_transitions_are_the_federal_ones() {
        // 2026: DST starts 2026-03-08 10:00 UTC, ends 2026-11-01 09:00 UTC.
        assert!(!is_pacific_daylight(at(2026, 3, 8, 9, 59)));
        assert!(is_pacific_daylight(at(2026, 3, 8, 10, 0)));
        assert!(is_pacific_daylight(at(2026, 11, 1, 8, 59)));
        assert!(!is_pacific_daylight(at(2026, 11, 1, 9, 0)));
        // Winter and summer sanity checks on the offset itself.
        assert_eq!(pacific_offset_ms(at(2026, 1, 15, 12, 0)), -8 * HOUR_MS);
        assert_eq!(pacific_offset_ms(at(2026, 7, 15, 12, 0)), -7 * HOUR_MS);
    }

    #[test]
    fn next_midnight_is_the_next_pacific_midnight() {
        // 2026-01-15 12:00 UTC is 04:00 PST -> 2026-01-16 08:00 UTC.
        assert_eq!(
            next_pacific_midnight_ms(at(2026, 1, 15, 12, 0)),
            at(2026, 1, 16, 8, 0)
        );
        // 2026-07-15 12:00 UTC is 05:00 PDT -> 2026-07-16 07:00 UTC.
        assert_eq!(
            next_pacific_midnight_ms(at(2026, 7, 15, 12, 0)),
            at(2026, 7, 16, 7, 0)
        );
        // The morning of the spring-forward day: midnight was 08:00 UTC.
        assert_eq!(
            next_pacific_midnight_ms(at(2026, 3, 8, 12, 0)),
            at(2026, 3, 9, 7, 0)
        );
    }

    #[test]
    fn next_midnight_is_always_in_the_future_and_within_a_day() {
        let mut instant = at(2026, 1, 1, 0, 0);
        while instant < at(2026, 12, 31, 0, 0) {
            let midnight = next_pacific_midnight_ms(instant);
            assert!(midnight > instant, "midnight must be in the future");
            assert!(
                midnight - instant <= DAY_MS + HOUR_MS,
                "midnight must be within a day (got {} ms)",
                midnight - instant
            );
            instant += 3 * HOUR_MS + 7 * MINUTE_MS;
        }
    }
}
