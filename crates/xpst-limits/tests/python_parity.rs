//! Parity with the shipped Python state machine.
//!
//! The fixture is not hand-written: `scripts/capture_cooldown_parity.py` imports
//! `src/xpst/utils/cooldown_store.py` and records the escalation ladder, the
//! rate-pool family map, `pause_for_strikes` across the ladder and past its end,
//! and the applied pause for a scripted hit sequence. Re-running it must
//! reproduce the file byte for byte.
//!
//! What this test protects is the part that has to *match* Python. The two
//! places where the Rust port deliberately differs are asserted too, so the
//! divergence is a decision on the record rather than drift nobody noticed.

mod support;

use serde_json::Value;
use support::{TempDir, BASE_MS, HOUR, MINUTE};

use xpst_limits::{
    ladder_pause_ms, Decision, HitOutcome, LimitsStore, Platform, PlatformResponse, Request,
    LADDER_CAP_MS, LADDER_MS, MAX_DEFERRED_RETRY_MS,
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/cooldown_ladder.json"))
        .expect("the captured Python fixture is valid JSON")
}

#[test]
fn the_escalation_ladder_is_the_python_ladder() {
    let captured = fixture();
    let python: Vec<i64> = captured["escalation_s"]
        .as_array()
        .expect("escalation_s is a list")
        .iter()
        .map(|value| value.as_i64().expect("seconds") * 1_000)
        .collect();
    assert_eq!(
        python.as_slice(),
        LADDER_MS.as_slice(),
        "the Rust ladder drifted from cooldown_store.DEFAULT_ESCALATION_S"
    );
}

#[test]
fn every_rung_of_pause_for_strikes_matches() {
    let captured = fixture();
    let grid = captured["pause_for_strikes"]
        .as_object()
        .expect("pause_for_strikes is a map");
    assert!(!grid.is_empty());
    for (strikes, seconds) in grid {
        let strikes: u32 = strikes.parse().expect("strike count");
        let expected = seconds.as_i64().expect("seconds") * 1_000;
        assert_eq!(
            ladder_pause_ms(strikes),
            expected,
            "pause_for_strikes({strikes}) drifted"
        );
    }
}

#[test]
fn the_rate_pool_family_map_matches() {
    let captured = fixture();
    let families = captured["family_by_platform"]
        .as_object()
        .expect("family_by_platform is a map");

    for platform in Platform::ALL {
        let Some(python) = families.get(platform.as_str()) else {
            continue;
        };
        let ours = platform.family().map(xpst_limits::Family::as_str);
        assert_eq!(
            ours,
            python.as_str(),
            "{platform} family membership drifted from Python"
        );
    }

    // Python also maps `messenger`, which has no publishing envelope in the
    // policy table (it is a messaging surface) so it is not a Platform here.
    let mut python_members: Vec<String> = captured["family_members"]["meta"]
        .as_array()
        .expect("meta members")
        .iter()
        .map(|value| value.as_str().expect("platform id").to_owned())
        .collect();
    python_members.retain(|member| member != "messenger");
    let mut ours: Vec<String> = Platform::Instagram
        .family_members()
        .iter()
        .map(|platform| platform.as_str().to_owned())
        .collect();
    python_members.sort();
    ours.sort();
    assert_eq!(ours, python_members, "Meta family membership drifted");
}

/// Python rewrites the deadline on every hit, so a short rung can end a longer
/// declared wait early — the scripted sequence in the fixture shows it: the
/// fourth instagram hit applies 480 s while the family window it had already
/// earned was 890 s. Rust keeps the longer window; this test proves the fixture
/// still shows Python's behaviour and that ours differs on purpose.
#[test]
fn a_short_rung_never_ends_a_long_declared_wait() {
    let captured = fixture();
    let hits = captured["hits"].as_array().expect("hits is a list");
    let shorten = &hits[3];
    assert_eq!(shorten["platform"], "instagram");
    assert_eq!(shorten["until_minus_now_s"].as_f64(), Some(480.0));
    assert_eq!(
        shorten["family_remaining_s"]["instagram"].as_f64(),
        Some(890.0),
        "the fixture no longer demonstrates Python shortening its own window"
    );

    let dir = TempDir::new("parity-no-shorten");
    let mut store = LimitsStore::open_at(dir.path(), BASE_MS).expect("open");
    // X has no challenge floor, so the declared wait is what sets the window —
    // which is exactly the shape Python's fourth instagram strike breaks.
    let first = store
        .record_hit(
            Platform::X,
            "acct-a",
            BASE_MS,
            PlatformResponse::Throttled {
                retry_after_ms: Some(15 * MINUTE),
                code: None,
            },
        )
        .unwrap();
    let HitOutcome::Cooling(first) = first else {
        panic!("expected cooling");
    };
    assert_eq!(first.pause_ms, 15 * MINUTE);

    let second = store
        .record_hit(
            Platform::X,
            "acct-a",
            BASE_MS + 8 * MINUTE,
            PlatformResponse::Throttled {
                retry_after_ms: None,
                code: None,
            },
        )
        .unwrap();
    let HitOutcome::Cooling(second) = second else {
        panic!("expected cooling");
    };
    assert_eq!(
        second.until_ms,
        BASE_MS + 15 * MINUTE,
        "Rust must not cut the 15-minute wait down to a 2-minute rung"
    );
}

/// Python clamps a declared wait to the 24 h ladder cap; Rust honours it, up to
/// the sanity bound. Shortening a window the platform asked for is the unsafe
/// direction, so this is the one place the port intentionally exceeds Python.
#[test]
fn a_declared_wait_beyond_the_ladder_cap_is_honoured() {
    // Compile-time, not run-time: if the bound ever drops to the ladder cap,
    // Python's clamp is back and this stops the build.
    const _: () = assert!(
        MAX_DEFERRED_RETRY_MS > LADDER_CAP_MS,
        "the honoured-wait bound must exceed the ladder cap, or Python's clamp is silently reintroduced"
    );
    let dir = TempDir::new("parity-retry");
    let mut store = LimitsStore::open_at(dir.path(), BASE_MS).expect("open");
    let declared = 48 * 60 * MINUTE;
    let outcome = store
        .record_hit(
            Platform::Threads,
            "acct-a",
            BASE_MS,
            PlatformResponse::Throttled {
                retry_after_ms: Some(declared),
                code: None,
            },
        )
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert_eq!(applied.pause_ms, declared);
    assert!(applied.pause_ms > LADDER_CAP_MS);

    // And the refusal that follows reports the platform's window, not the cap.
    let decision = store
        .claim(
            &Request::new(Platform::Threads, "acct-a", BASE_MS + 25 * HOUR)
                .with_asset("fic-hash-0004"),
        )
        .unwrap();
    let Decision::Refuse {
        retry_after_ms: Some(wait),
        ..
    } = decision
    else {
        panic!("the declared window must still be in force: {decision}");
    };
    assert_eq!(
        wait,
        declared - 25 * HOUR,
        "the refusal must report the platform's remaining window, not the cap"
    );
}
