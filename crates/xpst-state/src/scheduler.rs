//! The OS scheduler entry `xpst schedule install` writes.
//!
//! Parity target: `src/xpst/cli.py::_install_os_scheduler` /
//! `_uninstall_os_scheduler` / `_crontab_without_xpst`. Only the Linux branch is
//! portable here: macOS writes a LaunchAgent plist and Windows registers a
//! scheduled task, and neither can be exercised on this host (see the ledger's
//! deviations). What *is* portable is the part that decides what the crontab
//! should say, and that is where the behaviour lives:
//!
//! * the marker comment and the `*/N * * * * <bin> serve >> <log> 2>&1` line;
//! * the filter that removes a previous xPST entry before appending the new one.
//!
//! The filter has a quirk worth keeping: it swallows the line *after* a marker
//! or an xPST command, not the line before it. A crontab that lists the command
//! without a marker therefore loses whatever follows it. That is the shipped
//! behaviour, and [`crontab_without_xpst`] reproduces it rather than tidying it
//! up — a tidier filter would leave a stale entry behind on a real install.
//!
//! The subprocess calls stay in the CLI (`crates/xpst-cli/src/commands/schedule.rs`)
//! so this crate keeps owning documents, not processes.

/// The comment that marks the supervisor entry.
pub const MARKER: &str = "# xPST serve supervisor";

/// `_crontab_without_xpst`: drop prior xPST entries, then blank lines.
///
/// `skip_next` is Python's: a marker or an xPST command line consumes the line
/// that follows it, which is how the marker/command pair is removed as a unit.
pub fn crontab_without_xpst(lines: &[String]) -> Vec<String> {
    let mut filtered: Vec<String> = Vec::new();
    let mut skip_next = false;
    for line in lines {
        let markerish = line.contains("# xPST");
        let xpst_cmd = line.contains("xpst schedule run") || line.contains("xpst serve");
        if skip_next {
            skip_next = false;
            continue;
        }
        if markerish || xpst_cmd {
            skip_next = true;
            continue;
        }
        filtered.push(line.clone());
    }
    filtered
        .into_iter()
        .filter(|line| !line.trim().is_empty())
        .collect()
}

/// The supervisor line: `*/N * * * * <bin> serve >> <log> 2>&1`.
///
/// cron does not expand `~`, so the caller passes an absolute log path.
pub fn cron_line(interval: i64, xpst_bin: &str, cron_log: &str) -> String {
    format!("*/{interval} * * * * {xpst_bin} serve >> {cron_log} 2>&1")
}

/// The crontab to install: the surviving lines, the marker, then the new line.
pub fn install_table(existing: &[String], interval: i64, xpst_bin: &str, cron_log: &str) -> String {
    let mut filtered = crontab_without_xpst(existing);
    filtered.push(MARKER.to_string());
    filtered.push(cron_line(interval, xpst_bin, cron_log));
    format!("{}\n", filtered.join("\n"))
}

/// The crontab to leave behind on `--remove`.
///
/// `None` means "no lines survive", which Python answers with `crontab -r`
/// (remove the table entirely) rather than writing an empty one.
pub fn uninstall_table(existing: &[String]) -> Option<String> {
    let filtered = crontab_without_xpst(existing);
    if filtered.is_empty() {
        None
    } else {
        Some(format!("{}\n", filtered.join("\n")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(text: &str) -> Vec<String> {
        text.lines().map(str::to_string).collect()
    }

    #[test]
    fn a_marker_and_its_command_are_removed_together() {
        let table = lines(
            "# backup\n0 3 * * * /usr/bin/backup\n# xPST serve supervisor\n\
             */15 * * * * /old/xpst serve >> /old/cron.log 2>&1\n",
        );
        assert_eq!(
            crontab_without_xpst(&table),
            vec![
                "# backup".to_string(),
                "0 3 * * * /usr/bin/backup".to_string()
            ]
        );
    }

    #[test]
    fn a_command_without_a_marker_swallows_the_next_line() {
        // Python's `skip_next` fires on the command line itself, so the entry
        // after it goes too. Reproduced, not fixed: a real crontab written by
        // the shipped engine always carries the marker.
        let table =
            lines("*/15 * * * * /old/xpst serve >> /old.log 2>&1\n0 4 * * * /usr/bin/other\n");
        assert!(crontab_without_xpst(&table).is_empty());
    }

    #[test]
    fn the_legacy_schedule_run_tick_is_removed() {
        let table = lines(
            "# xPST scheduler\n*/15 * * * * /old/xpst schedule run\n0 5 * * * /usr/bin/keep\n",
        );
        assert_eq!(
            crontab_without_xpst(&table),
            vec!["0 5 * * * /usr/bin/keep".to_string()]
        );
    }

    #[test]
    fn blank_lines_are_dropped() {
        let table = lines("0 1 * * * /a\n\n   \n0 2 * * * /b\n");
        assert_eq!(
            crontab_without_xpst(&table),
            vec!["0 1 * * * /a".to_string(), "0 2 * * * /b".to_string()]
        );
    }

    #[test]
    fn install_appends_the_marker_and_the_line() {
        let table = lines("0 3 * * * /usr/bin/backup\n");
        assert_eq!(
            install_table(&table, 5, "/opt/xpst", "/home/user/.xpst/logs/cron.log"),
            "0 3 * * * /usr/bin/backup\n# xPST serve supervisor\n\
             */5 * * * * /opt/xpst serve >> /home/user/.xpst/logs/cron.log 2>&1\n"
        );
    }

    #[test]
    fn uninstall_asks_for_a_removal_when_nothing_survives() {
        let table =
            lines("# xPST serve supervisor\n*/15 * * * * /old/xpst serve >> /old.log 2>&1\n");
        assert_eq!(uninstall_table(&table), None);
        let table = lines("# xPST serve supervisor\n*/15 * * * * /old/xpst serve >> /old.log 2>&1\n0 3 * * * /b\n");
        assert_eq!(uninstall_table(&table), Some("0 3 * * * /b\n".to_string()));
    }
}
