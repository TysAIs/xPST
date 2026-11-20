//! Two real processes, one daily counter.
//!
//! The single-process version of this test cannot fail: the bug it guards
//! against is a lost update between a read and a write, which needs two
//! independent read-modify-write sequences. So the parent re-executes this test
//! binary as N child processes, releases them all at the same wall-clock
//! instant, and only then checks the total. If the advisory lock were missing
//! or scoped to one file handle, more than the cap would be allowed through.

mod support;

use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use support::{TempDir, BASE_MS, HOUR};

use xpst_limits::{LimitsStore, Platform, Request};

const CHILDREN: usize = 3;
const ATTEMPTS_PER_CHILD: i64 = 12;
/// Two hours apart: comfortably past the platform's jittered interval, and the
/// whole series stays inside one 24 h window so the daily cap is what binds.
const STEP_MS: i64 = 2 * HOUR;

fn wall_clock_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or(0)
}

/// The worker. Runs as a child process when the parent sets the environment,
/// and is a no-op (a trivially passing test) when the harness runs it normally.
#[test]
fn child_claim_worker() {
    let Ok(dir) = std::env::var("XPST_LIMITS_CHILD_DIR") else {
        return;
    };
    let base: i64 = std::env::var("XPST_LIMITS_CHILD_BASE_MS")
        .expect("base instant")
        .parse()
        .expect("base instant is an integer");
    let start_at: i64 = std::env::var("XPST_LIMITS_CHILD_START_MS")
        .expect("start instant")
        .parse()
        .expect("start instant is an integer");
    let tag = std::env::var("XPST_LIMITS_CHILD_TAG").expect("tag");

    // Everyone starts together, so the claims genuinely overlap.
    while wall_clock_ms() < start_at {
        std::thread::sleep(Duration::from_millis(1));
    }

    let mut store = LimitsStore::open_at(&dir, base).expect("open ledger");
    let mut allowed = 0;
    for step in 0..ATTEMPTS_PER_CHILD {
        let at_ms = base + step * STEP_MS;
        let asset = format!("fic-asset-{tag}-{step}");
        let request = Request::new(Platform::YouTube, "acct-race", at_ms).with_asset(&asset);
        if store.claim(&request).expect("claim").is_allowed() {
            allowed += 1;
        }
    }
    println!("CHILD_ALLOWS={allowed}");
}

#[test]
fn concurrent_processes_cannot_spend_more_than_the_daily_cap() {
    let dir = TempDir::new("cross-process");
    let exe = std::env::current_exe().expect("test binary path");
    let start_at = wall_clock_ms() + 750;

    let children: Vec<_> = (0..CHILDREN)
        .map(|tag| {
            Command::new(&exe)
                .args(["--exact", "child_claim_worker", "--nocapture"])
                .env("XPST_LIMITS_CHILD_DIR", dir.path())
                .env("XPST_LIMITS_CHILD_BASE_MS", BASE_MS.to_string())
                .env("XPST_LIMITS_CHILD_START_MS", start_at.to_string())
                .env("XPST_LIMITS_CHILD_TAG", format!("p{tag}"))
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .expect("spawn child")
        })
        .collect();

    let mut allowed_total = 0_usize;
    for child in children {
        let output = child.wait_with_output().expect("child finished");
        let stdout = String::from_utf8_lossy(&output.stdout);
        let Some(marker) = stdout
            .lines()
            .find(|line| line.starts_with("CHILD_ALLOWS="))
        else {
            panic!(
                "child produced no result marker.\nstdout:\n{stdout}\nstderr:\n{}",
                String::from_utf8_lossy(&output.stderr)
            );
        };
        allowed_total += marker["CHILD_ALLOWS=".len()..]
            .trim()
            .parse::<usize>()
            .expect("child count");
    }

    let cap = xpst_limits::envelope(Platform::YouTube).daily_cap as usize;
    assert_eq!(
        allowed_total, cap,
        "{CHILDREN} processes each asking {ATTEMPTS_PER_CHILD} times allowed \
         {allowed_total} publishes, cap is {cap}: a read-modify-write race is \
         losing counts"
    );

    let store = LimitsStore::open_at(dir.path(), BASE_MS).expect("reopen");
    let status = store
        .account_status(Platform::YouTube, "acct-race", BASE_MS)
        .expect("the raced account must be recorded");
    assert_eq!(
        status.publishes_24h, cap,
        "the ledger records exactly the publishes that were allowed"
    );
}
