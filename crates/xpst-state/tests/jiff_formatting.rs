//! Pin the exact time formatting the schedule listing emits.
//!
//! The listing's `scheduled_time_local` must be the string Python's
//! `datetime.isoformat()` produces: `%Y-%m-%dT%H:%M:%S`, fractional seconds
//! only when they are non-zero, and a colon-separated offset. `jiff`'s
//! `%.f` and `%:z` do that, but the behaviour is easy to get wrong in a way no
//! other test would catch — a wrong pattern still yields a plausible-looking
//! timestamp, and only a byte comparison against Python's rendering shows it.
//!
//! These are the same strings the Python engine produced for the
//! corresponding instants (captured in `xpst-cli/tests/fixtures/local_surface.json`).

use jiff::civil::DateTime;
use jiff::tz::TimeZone;

fn render(civil: &str, zone: TimeZone) -> String {
    let datetime: DateTime = civil.parse().expect("civil datetime");
    datetime
        .to_zoned_datetime(zone)
        .expect("wall clock resolves")
        .strftime("%Y-%m-%dT%H:%M:%S%.f%:z")
        .to_string()
}

#[test]
fn isoformat_shape_matches_python() {
    assert_eq!(
        render("2026-01-15T09:30:00", TimeZone::UTC),
        "2026-01-15T09:30:00+00:00"
    );
    // Python prints microseconds only when they are non-zero, and trims
    // trailing zeros: 500 ms is `.5`, not `.500000`.
    assert_eq!(
        render("2026-01-15T09:30:00.500", TimeZone::UTC),
        "2026-01-15T09:30:00.5+00:00"
    );
    assert_eq!(
        render("2026-01-15T09:30:00.123456", TimeZone::UTC),
        "2026-01-15T09:30:00.123456+00:00"
    );
    let new_york = TimeZone::get("America/New_York").expect("zone database");
    assert_eq!(
        render("2026-07-15T09:30:00", new_york.clone()),
        "2026-07-15T09:30:00-04:00"
    );
    assert_eq!(
        render("2026-01-15T09:30:00", new_york),
        "2026-01-15T09:30:00-05:00"
    );
}

#[test]
fn an_offset_bearing_instant_keeps_its_instant_when_re_zoned() {
    // Python's `local_time()` converts the stored instant into the entry's
    // zone; it does not relabel the wall clock.
    let instant: jiff::Timestamp = "2026-02-01T12:00:00+00:00".parse().expect("timestamp");
    assert_eq!(
        instant
            .to_zoned_datetime(TimeZone::get("America/New_York").unwrap())
            .strftime("%Y-%m-%dT%H:%M:%S%.f%:z")
            .to_string(),
        "2026-02-01T07:00:00-05:00"
    );
}
