//! `_crontab_without_xpst` parity, row by row.
//!
//! The fixture is captured by `xtask/scripts/capture_crontab_filter.py`, which
//! calls the shipped filter — so this test holds the Rust port to what the
//! Python engine does, not to what the filter looks like it should do. The
//! quirk it pins: `skip_next` swallows the line *after* a marker or an xPST
//! command, so a command with no marker also removes the entry below it.
//!
//! Regenerate with:
//!
//! ```text
//! PYTHONPATH=src python xtask/scripts/capture_crontab_filter.py
//! ```

use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/crontab_filter.json");

/// A loop over an emptied array asserts nothing, so the count is pinned.
const EXPECTED_CASES: usize = 12;

fn document() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture is valid JSON")
}

fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("a list of lines")
        .iter()
        .map(|item| item.as_str().expect("a line is text").to_string())
        .collect()
}

#[test]
fn every_captured_case_matches_the_python_filter() {
    let document = document();
    let cases = document["cases"].as_array().expect("fixture has cases");
    assert_eq!(
        cases.len(),
        EXPECTED_CASES,
        "the fixture holds {} cases, not {EXPECTED_CASES}: the capture wrote an empty or \
         partial collection",
        cases.len()
    );
    assert_eq!(
        document["case_count"].as_u64().unwrap_or(0) as usize,
        EXPECTED_CASES,
        "case_count disagrees with the checked-in cases"
    );

    let mut checked = 0;
    for case in cases {
        let lines = strings(&case["lines"]);
        let expected = strings(&case["kept"]);
        let actual = xpst_state::scheduler::crontab_without_xpst(&lines);
        assert_eq!(actual, expected, "filter drifted for input {lines:?}");
        checked += 1;
    }
    assert_eq!(checked, EXPECTED_CASES);
}

#[test]
fn the_install_table_appends_the_marker_and_the_line() {
    let existing = vec![
        "# backup".to_string(),
        "0 3 * * * /usr/bin/backup".to_string(),
        "# xPST serve supervisor".to_string(),
        "*/15 * * * * /old/xpst serve >> /old/cron.log 2>&1".to_string(),
    ];
    let table = xpst_state::scheduler::install_table(
        &existing,
        15,
        "/opt/xpst",
        "/home/user/.xpst/logs/cron.log",
    );
    assert_eq!(
        table,
        "# backup\n0 3 * * * /usr/bin/backup\n# xPST serve supervisor\n\
         */15 * * * * /opt/xpst serve >> /home/user/.xpst/logs/cron.log 2>&1\n"
    );
}

#[test]
fn uninstall_asks_for_a_removal_only_when_nothing_survives() {
    let only_xpst = vec![
        "# xPST serve supervisor".to_string(),
        "*/15 * * * * /old/xpst serve >> /old/cron.log 2>&1".to_string(),
    ];
    assert_eq!(xpst_state::scheduler::uninstall_table(&only_xpst), None);
    let mixed = vec![
        "# xPST serve supervisor".to_string(),
        "*/15 * * * * /old/xpst serve >> /old/cron.log 2>&1".to_string(),
        "0 3 * * * /usr/bin/backup".to_string(),
    ];
    assert_eq!(
        xpst_state::scheduler::uninstall_table(&mixed),
        Some("0 3 * * * /usr/bin/backup\n".to_string())
    );
}
