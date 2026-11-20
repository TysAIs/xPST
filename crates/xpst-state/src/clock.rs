//! Naive-UTC ISO-8601 timestamps in the shape the Python engine persists.
//!
//! `state_manager._utc_now_iso()` returns
//! `datetime.now(timezone.utc).replace(tzinfo=None).isoformat()` — a *naive*
//! UTC string with microsecond precision and no offset. Everything downstream
//! (desktop weekly counts, analytics relative times, the monitor's age checks)
//! compares against naive `datetime.now()` values, so an offset-bearing string
//! or a re-based clock would silently change behaviour. This module therefore
//! formats UTC itself, with no date crate: the conversion is a fixed civil
//! calendar algorithm, and a dependency that could change its mind about
//! formatting is a worse trade than twenty lines of arithmetic.

use std::time::{SystemTime, UNIX_EPOCH};

/// Current UTC instant as `YYYY-MM-DDTHH:MM:SS.ffffff` (no offset).
pub fn utc_now_iso() -> String {
    let (secs, nanos) = match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(elapsed) => (elapsed.as_secs() as i64, elapsed.subsec_nanos()),
        // A clock before the epoch is not a reason to fail a write; clamp.
        Err(_) => (0, 0),
    };
    iso_from_unix(secs, nanos)
}

/// Format a Unix instant as the naive-UTC ISO-8601 string Python writes.
///
/// Kept public so tests can pin the exact shape against values Python produced
/// rather than against this module's own opinion.
pub fn iso_from_unix(secs: i64, nanos: u32) -> String {
    let micros = i64::from(nanos / 1_000);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = rem / 3_600;
    let minute = (rem % 3_600) / 60;
    let second = rem % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{micros:06}")
}

/// Days-since-epoch to a proleptic Gregorian date (Howard Hinnant's algorithm,
/// the same one every C library's `gmtime` implements).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m as u32, d as u32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_the_same_instants_python_writes() {
        // 2026-11-17T19:15:24.259096Z — produced by
        // `datetime.now(timezone.utc).replace(tzinfo=None).isoformat()`.
        assert_eq!(
            iso_from_unix(1_794_942_924, 259_096_000),
            "2026-11-17T19:15:24.259096"
        );
        // Epoch and a leap day, i.e. the two calendar edge cases that break a
        // hand-rolled converter.
        assert_eq!(iso_from_unix(0, 0), "1970-01-01T00:00:00.000000");
        assert_eq!(
            iso_from_unix(1_709_164_800, 1_000),
            "2024-02-29T00:00:00.000001"
        );
    }

    #[test]
    fn now_has_the_persisted_shape() {
        let now = utc_now_iso();
        assert_eq!(now.len(), 26, "unexpected timestamp shape: {now}");
        assert_eq!(now.as_bytes()[10], b'T');
        assert_eq!(now.as_bytes()[19], b'.');
        assert!(!now.ends_with("+00:00"), "state timestamps are naive UTC");
        assert!(!now.ends_with('Z'), "state timestamps carry no suffix");
    }
}
