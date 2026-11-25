//! Best-time-to-post analysis over the analytics ledger.
//!
//! Parity target: `src/xpst/best_time.py::BestTimeAnalyzer` — the hour-of-week
//! bucket analysis behind `xpst best-time`. It reads `metric_snapshots` and
//! nothing else: no schema is created, no migration runs, no row is written.
//!
//! The reader is deliberately as tolerant as Python's. `sqlite3.connect()`
//! creates an empty file when the ledger is absent, and every failure after
//! that (no table, no `collection_status` column, a timestamp that will not
//! parse) is swallowed and answered with an empty list — `best-time` on a fresh
//! profile prints `{"best_times": {}}` rather than failing.
//!
//! Two Python details that change the answer:
//!
//! * The first snapshot per `(platform, captured_at)` is the one that counts
//!   (`seen_posts`), and a row whose `collection_status` is `failed` is not a
//!   measurement at all.
//! * `round(avg, 1)` is Python's round-half-to-even on the exact binary value,
//!   and the *rounded* number is what the results are sorted by. Sorting the
//!   unrounded average reorders ties.

use std::path::Path;

use rusqlite::Connection;

/// `DAY_NAMES` — Python's `weekday()` index (Monday is 0).
pub const DAY_NAMES: [&str; 7] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
];

/// One recommended slot, in the order `analyze()` emits the dict keys.
#[derive(Debug, Clone, PartialEq)]
pub struct Slot {
    pub platform: String,
    pub day_of_week: u8,
    pub hour: u8,
    pub avg_engagement_rate: f64,
    pub post_count: usize,
}

impl Slot {
    /// `DAY_NAMES[day_of_week]`.
    #[must_use]
    pub fn day_name(&self) -> &'static str {
        DAY_NAMES[usize::from(self.day_of_week)]
    }
}

/// `round(value, 1)` — correctly-rounded decimal, ties to even.
fn round_one(value: f64) -> f64 {
    if !value.is_finite() {
        return value;
    }
    format!("{value:.1}").parse::<f64>().unwrap_or(value)
}

/// Days since 1970-01-01 for a proleptic Gregorian date (Hinnant's algorithm).
fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month = i64::from(month);
    let day = i64::from(day);
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// `datetime.fromisoformat(...).weekday()` and `.hour`, without a timezone
/// conversion: Python keeps the wall-clock fields of an aware timestamp.
///
/// Accepts the shapes `fromisoformat` accepts in practice — `YYYY-MM-DD`,
/// `YYYY-MM-DDTHH:MM[:SS[.ffffff]]`, the same with a space separator, a
/// trailing `Z`/offset (ignored, as Python ignores it for `.hour`), and the
/// basic `YYYYMMDDTHHMMSS` form.
fn parse_captured_at(text: &str) -> Option<(u8, u8)> {
    let text = text.trim();
    let (date_part, time_part) = match text.find(['T', 't', ' ']) {
        Some(index) => (&text[..index], Some(&text[index + 1..])),
        None => (text, None),
    };

    let (year, month, day) =
        if date_part.len() == 8 && date_part.chars().all(|c| c.is_ascii_digit()) {
            (
                date_part[0..4].parse::<i64>().ok()?,
                date_part[4..6].parse::<u32>().ok()?,
                date_part[6..8].parse::<u32>().ok()?,
            )
        } else {
            let mut pieces = date_part.split('-');
            let year = pieces.next()?.parse::<i64>().ok()?;
            let month = pieces.next()?.parse::<u32>().ok()?;
            let day = pieces.next()?.parse::<u32>().ok()?;
            if pieces.next().is_some() {
                return None;
            }
            (year, month, day)
        };
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }

    let hour = match time_part {
        None => 0,
        Some(time) => {
            // Strip a trailing offset (`+00:00`, `-05:00`, `Z`) — the wall
            // clock is what Python reports.
            let time = time.trim_end_matches(['Z', 'z']);
            let time = match time.find(['+']) {
                Some(index) => &time[..index],
                None => time,
            };
            // A `-` after the first two characters is an offset, not a date.
            let time = match time[2..].find('-') {
                Some(index) => &time[..index + 2],
                None => time,
            };
            let time = time.split('.').next().unwrap_or(time);
            if time.len() == 6 && time.chars().all(|c| c.is_ascii_digit()) {
                time[0..2].parse::<u8>().ok()?
            } else {
                let mut pieces = time.split(':');
                let hour = pieces.next()?.parse::<u8>().ok()?;
                let _minute = pieces.next()?.parse::<u8>().ok()?;
                if let Some(second) = pieces.next() {
                    second.parse::<u8>().ok()?;
                }
                hour
            }
        }
    };
    if hour > 23 {
        return None;
    }

    let days = days_from_civil(year, month, day);
    // 1970-01-01 was a Thursday, which is index 3 in Python's Monday-first week.
    let weekday = ((days % 7) + 7 + 3) % 7;
    Some((weekday as u8, hour))
}

/// `BestTimeAnalyzer.analyze(platform)`.
///
/// Opens (creating if absent) the ledger at `db_path` and returns the slots
/// sorted by average engagement rate, best first. Every failure is an empty
/// answer, exactly like the Python method's `except Exception: return []`.
#[must_use]
pub fn analyze(db_path: &Path, platform: Option<&str>) -> Vec<Slot> {
    let Ok(conn) = Connection::open(db_path) else {
        return Vec::new();
    };
    analyze_connection(&conn, platform)
}

/// [`analyze`] against an already-open connection (used by the tests).
#[must_use]
pub fn analyze_connection(conn: &Connection, platform: Option<&str>) -> Vec<Slot> {
    let has_status = match columns(conn) {
        Some(columns) => columns.iter().any(|name| name == "collection_status"),
        None => return Vec::new(),
    };

    let sql = if has_status {
        "SELECT platform, captured_at, views, likes, comments, shares, \
         collection_status AS collection_status FROM metric_snapshots \
         WHERE (:platform IS NULL OR platform = :platform)"
    } else {
        "SELECT platform, captured_at, views, likes, comments, shares, \
         NULL AS collection_status FROM metric_snapshots \
         WHERE (:platform IS NULL OR platform = :platform)"
    };

    let Ok(mut statement) = conn.prepare(sql) else {
        return Vec::new();
    };
    let Ok(rows) = statement.query_map(rusqlite::params![platform], |row| {
        Ok(Row {
            platform: row.get::<_, String>(0)?,
            captured_at: row.get::<_, String>(1)?,
            views: row.get::<_, Option<i64>>(2)?,
            likes: row.get::<_, Option<i64>>(3)?,
            comments: row.get::<_, Option<i64>>(4)?,
            shares: row.get::<_, Option<i64>>(5)?,
            collection_status: row.get::<_, Option<String>>(6)?,
        })
    }) else {
        return Vec::new();
    };

    let mut seen: Vec<String> = Vec::new();
    let mut buckets: Vec<((String, u8, u8), Vec<f64>)> = Vec::new();
    for row in rows.flatten() {
        if row.collection_status.as_deref() == Some("failed") {
            continue;
        }
        let key = format!("{}:{}", row.platform, row.captured_at);
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);

        let views = row.views.unwrap_or(0);
        if views == 0 {
            continue;
        }
        let engagement =
            row.likes.unwrap_or(0) + row.comments.unwrap_or(0) + row.shares.unwrap_or(0);
        let rate = (engagement as f64 / views as f64) * 100.0;

        let Some((weekday, hour)) = parse_captured_at(&row.captured_at) else {
            continue;
        };
        let bucket_key = (row.platform, weekday, hour);
        match buckets.iter_mut().find(|(known, _)| *known == bucket_key) {
            Some((_, rates)) => rates.push(rate),
            None => buckets.push((bucket_key, vec![rate])),
        }
    }

    let mut results: Vec<Slot> = buckets
        .into_iter()
        .map(|((platform, day_of_week, hour), rates)| Slot {
            platform,
            day_of_week,
            hour,
            avg_engagement_rate: round_one(rates.iter().sum::<f64>() / rates.len() as f64),
            post_count: rates.len(),
        })
        .collect();

    // Stable, so equal averages keep first-appearance order — Python's
    // `list.sort(key=..., reverse=True)` is stable too.
    results.sort_by(|left, right| {
        right
            .avg_engagement_rate
            .partial_cmp(&left.avg_engagement_rate)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    results
}

struct Row {
    platform: String,
    captured_at: String,
    views: Option<i64>,
    likes: Option<i64>,
    comments: Option<i64>,
    shares: Option<i64>,
    collection_status: Option<String>,
}

/// `PRAGMA table_info(metric_snapshots)` column names, or `None` when the table
/// is absent (the pragma itself succeeds with zero rows).
fn columns(conn: &Connection) -> Option<Vec<String>> {
    let mut statement = conn.prepare("PRAGMA table_info(metric_snapshots)").ok()?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(1))
        .ok()?;
    let names: Vec<String> = rows.flatten().collect();
    // Python's `conn.execute(query)` then raises `no such table`, which the
    // method catches and answers with `[]`.
    if names.is_empty() {
        None
    } else {
        Some(names)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ledger() -> Connection {
        let conn = Connection::open_in_memory().expect("in-memory ledger");
        conn.execute_batch(
            "CREATE TABLE metric_snapshots (
                platform TEXT NOT NULL, post_id TEXT NOT NULL, captured_at TEXT NOT NULL,
                views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
                reposts INTEGER, saves INTEGER, extra TEXT,
                collection_status TEXT, collection_error TEXT,
                PRIMARY KEY (platform, post_id, captured_at));",
        )
        .expect("schema");
        conn
    }

    fn insert(conn: &Connection, platform: &str, captured_at: &str, views: i64, likes: i64) {
        insert_post(conn, platform, "p", captured_at, views, likes);
    }

    fn insert_post(
        conn: &Connection,
        platform: &str,
        post_id: &str,
        captured_at: &str,
        views: i64,
        likes: i64,
    ) {
        conn.execute(
            "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![platform, post_id, captured_at, views, likes],
        )
        .expect("insert");
    }

    #[test]
    fn an_absent_table_is_an_empty_answer() {
        let conn = Connection::open_in_memory().expect("ledger");
        assert!(analyze_connection(&conn, None).is_empty());
    }

    #[test]
    fn buckets_are_ranked_by_rounded_average() {
        let conn = ledger();
        // 2026-01-01 is a Thursday (weekday 3), 10:00.
        insert(&conn, "youtube", "2026-01-01T10:00:00+00:00", 100, 10);
        // A second post at the same instant: deduped by (platform, captured_at).
        insert_post(&conn, "youtube", "p2", "2026-01-01T10:00:00+00:00", 100, 10);
        insert(&conn, "x", "2026-01-02T11:00:00Z", 100, 50);
        let slots = analyze_connection(&conn, None);
        assert_eq!(slots.len(), 2);
        assert_eq!(slots[0].platform, "x");
        assert_eq!(slots[0].avg_engagement_rate, 50.0);
        assert_eq!(slots[0].day_of_week, 4);
        assert_eq!(slots[0].hour, 11);
        assert_eq!(slots[1].platform, "youtube");
        assert_eq!(slots[1].post_count, 1);
    }

    #[test]
    fn a_failed_collection_is_not_a_measurement() {
        let conn = ledger();
        conn.execute(
            "INSERT INTO metric_snapshots (platform, post_id, captured_at, views, likes, \
             collection_status) VALUES ('x', 'p', '2026-01-02T11:00:00', 100, 50, 'failed')",
            [],
        )
        .expect("insert");
        assert!(analyze_connection(&conn, None).is_empty());
    }

    #[test]
    fn the_platform_filter_is_a_bound_parameter() {
        let conn = ledger();
        insert(&conn, "youtube", "2026-01-01T10:00:00", 100, 10);
        insert(&conn, "x", "2026-01-02T11:00:00", 100, 50);
        let slots = analyze_connection(&conn, Some("x"));
        assert_eq!(slots.len(), 1);
        assert_eq!(slots[0].platform, "x");
    }

    #[test]
    fn timestamps_keep_their_wall_clock_fields() {
        assert_eq!(
            parse_captured_at("2026-01-01T10:00:00+00:00"),
            Some((3, 10))
        );
        assert_eq!(parse_captured_at("2026-01-01T10:00:00Z"), Some((3, 10)));
        assert_eq!(parse_captured_at("2026-01-01 10:00:00"), Some((3, 10)));
        assert_eq!(parse_captured_at("2026-01-01"), Some((3, 0)));
        assert_eq!(parse_captured_at("20260101T100000"), Some((3, 10)));
        assert_eq!(
            parse_captured_at("2026-01-01T10:00:00.123456-05:00"),
            Some((3, 10))
        );
        assert_eq!(parse_captured_at("not a timestamp"), None);
    }

    #[test]
    fn rounding_is_half_to_even_like_python() {
        assert_eq!(round_one(33.35), 33.4);
        assert_eq!(round_one(0.25), 0.2);
        assert_eq!(round_one(0.35), 0.3);
        assert_eq!(round_one(100.0), 100.0);
    }
}
