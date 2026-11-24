//! `datetime.fromisoformat`'s verdicts, held to a capture from the real Python.
//!
//! `xpst_schedule_add` passes `scheduled_time` straight into
//! `datetime.fromisoformat` and catches only `ValueError`, so the parser's
//! accept/reject set and its error sentences are the wire contract. The grid in
//! `fixtures/python_fromisoformat.json` comes from
//! `xtask/scripts/capture_schedule_time.py`; nothing here is transcribed by
//! hand.
//!
//! The offset range check is the handler's *second* step (`datetime.timezone`
//! refuses ±24h after the parse succeeds), so this test applies it the same way
//! and compares the combined verdict.

use serde_json::Value;
use xpst_surface::isotime::{fromisoformat, offset_range_error};

const FIXTURE: &str = include_str!("fixtures/python_fromisoformat.json");

/// A loop over an emptied array asserts nothing.
const MINIMUM_CASES: usize = 150;

fn cases() -> Vec<Value> {
    let document: Value = serde_json::from_str(FIXTURE).expect("fixture is valid JSON");
    let cases = document["cases"]
        .as_array()
        .expect("fixture has a cases array")
        .clone();
    assert!(
        cases.len() >= MINIMUM_CASES,
        "the fixture holds {} cases, not at least {MINIMUM_CASES}: the capture wrote an empty or \
         partial grid",
        cases.len()
    );
    cases
}

/// The verdict the handler acts on: the parse, then the offset range check.
fn verdict(input: &str) -> Result<String, String> {
    let stamp = fromisoformat(input)?;
    if let Some(offset) = stamp.offset {
        if let Some(message) = offset_range_error(offset) {
            return Err(message);
        }
    }
    Ok(stamp.isoformat())
}

#[test]
fn every_captured_case_matches_pythons_verdict() {
    let mut accepted = 0;
    let mut rejected = 0;
    for case in cases() {
        let input = case["input"].as_str().expect("case input");
        let expected_ok = case.get("ok").and_then(Value::as_str);
        let expected_error = case.get("error").and_then(Value::as_str);
        match (verdict(input), expected_ok, expected_error) {
            (Ok(actual), Some(expected), None) => {
                assert_eq!(actual, expected, "`{input}` parsed to a different value");
                accepted += 1;
            }
            (Err(actual), None, Some(expected)) => {
                assert_eq!(
                    actual, expected,
                    "`{input}` failed with a different message"
                );
                rejected += 1;
            }
            (actual, expected_ok, expected_error) => panic!(
                "`{input}`: the port answered {actual:?} where Python answered \
                 ok={expected_ok:?} error={expected_error:?}"
            ),
        }
    }
    // Both halves must be exercised: a grid that only accepts (or only rejects)
    // would pass while the other half of the grammar was wrong.
    assert!(accepted >= 100, "only {accepted} cases were accepted");
    assert!(rejected >= 40, "only {rejected} cases were rejected");
}

#[test]
fn the_ordinal_dates_iso_8601_defines_are_rejected() {
    // `fromisoformat` does not read `YYYY-DDD` / `YYYYDDD`, even though ISO-8601
    // defines them. A parser written from the standard accepts them and then
    // schedules a post on a date the shipped engine refuses.
    for input in ["2026-001", "2026001", "2026-366"] {
        assert_eq!(
            verdict(input),
            Err(format!("Invalid isoformat string: '{input}'")),
            "`{input}` must be refused"
        );
    }
}

#[test]
fn a_fraction_is_truncated_never_rounded() {
    assert_eq!(
        verdict("2026-12-01T09:30:00.9999999").as_deref(),
        Ok("2026-12-01T09:30:00.999999")
    );
    assert_eq!(
        verdict("2026-12-01T09:30:00.000000").as_deref(),
        Ok("2026-12-01T09:30:00")
    );
}

#[test]
fn an_offset_keeps_its_seconds_and_microseconds() {
    assert_eq!(
        verdict("2026-12-01T09:30:00+05:30:15.5").as_deref(),
        Ok("2026-12-01T09:30:00+05:30:15.500000")
    );
    // `Z` is uppercase only.
    assert_eq!(
        verdict("2026-12-01T09:30:00z"),
        Err("Invalid isoformat string: '2026-12-01T09:30:00z'".to_string())
    );
}
