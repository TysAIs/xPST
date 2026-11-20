//! The durable cooldown ledger: escalation, honouring a declared wait, recovery,
//! family sharing, and failing closed when the file cannot be trusted.

mod support;

use std::fs;

use support::{TempDir, BASE_MS, DAY, HOUR, MINUTE};

use xpst_limits::{
    Decision, HitOutcome, LimitsStore, Platform, PlatformResponse, RefusalCode, Request,
    LADDER_CAP_MS, LADDER_MS, STATE_FILE_NAME,
};

fn throttled(retry_after_ms: Option<i64>) -> PlatformResponse {
    PlatformResponse::Throttled {
        retry_after_ms,
        code: Some("429".to_owned()),
    }
}

fn open(dir: &TempDir) -> LimitsStore {
    LimitsStore::open_at(dir.path(), BASE_MS).expect("open ledger")
}

fn claim_refusal(
    store: &mut LimitsStore,
    platform: Platform,
    account: &str,
    at_ms: i64,
) -> Decision {
    store
        .claim(&Request::new(platform, account, at_ms).with_asset("fic-content-hash-0009"))
        .expect("claim")
}

#[test]
fn a_cooldown_survives_a_restart() {
    let dir = TempDir::new("restart");
    {
        let mut store = open(&dir);
        store
            .record_hit(Platform::X, "acct-a", BASE_MS, throttled(None))
            .unwrap();
    }
    // A new process, same directory: the pause must still be in force. This is
    // the failure the Python ledger was written for — a restart after a halt
    // firing the next upload straight back into the same wall.
    let mut store = open(&dir);
    let decision = claim_refusal(&mut store, Platform::X, "acct-a", BASE_MS + 30_000);
    assert_eq!(decision.refusal_code(), Some(RefusalCode::CooldownActive));
    // And it lifts on its own.
    let later = claim_refusal(&mut store, Platform::X, "acct-a", BASE_MS + 61_000);
    assert!(later.is_allowed(), "{later}");
}

#[test]
fn the_ladder_doubles_from_one_minute_and_caps() {
    let dir = TempDir::new("ladder");
    let mut store = open(&dir);
    let mut at = BASE_MS;

    for (index, expected) in LADDER_MS.iter().enumerate() {
        let outcome = store
            .record_hit(Platform::X, "acct-a", at, throttled(None))
            .unwrap();
        let HitOutcome::Cooling(applied) = outcome else {
            panic!("a throttle must cool: {outcome:?}");
        };
        assert_eq!(
            applied.pause_ms,
            *expected,
            "strike {} must pause {expected} ms",
            index + 1
        );
        assert_eq!(applied.strikes as usize, index + 1);
        at += expected;
    }

    // Past the top of the ladder the pause stays at the cap.
    let outcome = store
        .record_hit(Platform::X, "acct-a", at, throttled(None))
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert_eq!(applied.pause_ms, LADDER_CAP_MS);
    assert_eq!(applied.strikes as usize, LADDER_MS.len());
}

#[test]
fn a_declared_retry_after_is_honoured_even_past_the_ladder_cap() {
    let dir = TempDir::new("retry-after");
    let mut store = open(&dir);
    let declared = 72 * HOUR;

    let outcome = store
        .record_hit(Platform::X, "acct-a", BASE_MS, throttled(Some(declared)))
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert_eq!(applied.pause_ms, declared);
    assert_eq!(applied.honoured_retry_after_ms, Some(declared));
    assert_eq!(applied.until_ms, BASE_MS + declared);

    // The window is real: nothing is allowed inside it.
    let inside = claim_refusal(&mut store, Platform::X, "acct-a", BASE_MS + 24 * HOUR);
    assert_eq!(inside.refusal_code(), Some(RefusalCode::CooldownActive));
    let outside = claim_refusal(
        &mut store,
        Platform::X,
        "acct-a",
        BASE_MS + declared + MINUTE,
    );
    assert!(outside.is_allowed(), "{outside}");
}

#[test]
fn an_absurd_retry_after_is_bounded() {
    let dir = TempDir::new("retry-absurd");
    let mut store = open(&dir);
    let outcome = store
        .record_hit(Platform::X, "acct-a", BASE_MS, throttled(Some(365 * DAY)))
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert_eq!(applied.pause_ms, xpst_limits::MAX_DEFERRED_RETRY_MS);
    assert_ne!(
        applied.honoured_retry_after_ms,
        Some(365 * DAY),
        "a truncated wait is not an honoured wait"
    );
}

#[test]
fn a_hit_never_shortens_an_open_window() {
    let dir = TempDir::new("no-shorten");
    let mut store = open(&dir);
    // A 15-minute declared wait.
    store
        .record_hit(Platform::X, "acct-a", BASE_MS, throttled(Some(15 * MINUTE)))
        .unwrap();
    // Eight minutes later a second hit lands; its ladder rung is two minutes.
    let outcome = store
        .record_hit(Platform::X, "acct-a", BASE_MS + 8 * MINUTE, throttled(None))
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert_eq!(
        applied.until_ms,
        BASE_MS + 15 * MINUTE,
        "the earlier 15-minute window must not be cut short"
    );
}

#[test]
fn a_clean_publish_resets_the_ladder() {
    let dir = TempDir::new("reset");
    let mut store = open(&dir);
    store
        .record_hit(Platform::X, "acct-a", BASE_MS, throttled(None))
        .unwrap();
    store
        .record_hit(Platform::X, "acct-a", BASE_MS + MINUTE, throttled(None))
        .unwrap();
    assert!(store
        .record_success(Platform::X, "acct-a", BASE_MS + 2 * MINUTE)
        .unwrap());

    let outcome = store
        .record_hit(Platform::X, "acct-a", BASE_MS + 3 * MINUTE, throttled(None))
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert_eq!(
        applied.strikes, 1,
        "a success returns the ladder to the floor"
    );
    assert_eq!(applied.pause_ms, 60_000);
}

#[test]
fn stale_strikes_do_not_escalate_a_new_episode() {
    let dir = TempDir::new("stale");
    let mut store = open(&dir);
    for _ in 0..4 {
        store
            .record_hit(Platform::X, "acct-a", BASE_MS, throttled(None))
            .unwrap();
    }
    // Two days later: quiet long enough that this is a new episode.
    let outcome = store
        .record_hit(
            Platform::X,
            "acct-a",
            BASE_MS + xpst_limits::STRIKE_DECAY_MS + HOUR,
            throttled(None),
        )
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert_eq!(applied.strikes, 1);
}

#[test]
fn a_meta_throttle_cools_the_whole_family() {
    let dir = TempDir::new("family");
    let mut store = open(&dir);
    let outcome = store
        .record_hit(Platform::Instagram, "acct-a", BASE_MS, throttled(None))
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    for sibling in [Platform::Facebook, Platform::Threads] {
        assert!(
            applied.family_extended.contains(&sibling),
            "a Meta throttle must extend {sibling}"
        );
        let decision = claim_refusal(&mut store, sibling, "acct-a", BASE_MS + MINUTE);
        assert_eq!(
            decision.refusal_code(),
            Some(RefusalCode::CooldownActive),
            "{sibling} must be cooling too"
        );
    }
    // The session path does not share Meta's app pool, so it is untouched.
    let session = claim_refusal(
        &mut store,
        Platform::InstagramSession,
        "acct-a",
        BASE_MS + MINUTE,
    );
    assert!(
        session.is_allowed(),
        "the direct/session path has its own budget: {session}"
    );
    // Only the account that took the hit, not every account on the platform.
    let other_account = claim_refusal(&mut store, Platform::Facebook, "acct-b", BASE_MS + MINUTE);
    assert!(
        other_account.is_allowed(),
        "family sharing is per account, not per platform: {other_account}"
    );
}

#[test]
fn a_short_sibling_strike_does_not_shorten_a_longer_window() {
    let dir = TempDir::new("family-no-shorten");
    let mut store = open(&dir);
    store
        .record_hit(
            Platform::Facebook,
            "acct-a",
            BASE_MS,
            throttled(Some(60 * MINUTE)),
        )
        .unwrap();
    // A one-minute strike on Threads must not pull Facebook's hour back in.
    store
        .record_hit(
            Platform::Threads,
            "acct-a",
            BASE_MS + MINUTE,
            throttled(None),
        )
        .unwrap();
    let facebook = claim_refusal(
        &mut store,
        Platform::Facebook,
        "acct-a",
        BASE_MS + 10 * MINUTE,
    );
    assert_eq!(facebook.refusal_code(), Some(RefusalCode::CooldownActive));
    let until = facebook.to_string();
    assert!(until.contains("retry_after_ms"), "{until}");
}

#[test]
fn a_transient_failure_records_nothing() {
    let dir = TempDir::new("transient");
    let mut store = open(&dir);
    let outcome = store
        .record_hit(
            Platform::X,
            "acct-a",
            BASE_MS,
            PlatformResponse::Transient {
                code: Some("503".to_owned()),
            },
        )
        .unwrap();
    assert!(!outcome.is_cooling());
    let decision = claim_refusal(&mut store, Platform::X, "acct-a", BASE_MS + MINUTE);
    assert!(
        decision.is_allowed(),
        "a 503 is not a rate limit: {decision}"
    );
}

#[test]
fn a_suspension_stops_publishing_until_a_human_clears_it() {
    let dir = TempDir::new("suspended");
    let mut store = open(&dir);
    let outcome = store
        .record_hit(
            Platform::TikTok,
            "acct-a",
            BASE_MS,
            PlatformResponse::Suspended {
                code: Some("spam_risk_user_banned".to_owned()),
            },
        )
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    assert!(applied.human_required);

    for wait in [MINUTE, DAY, 30 * DAY] {
        let decision = claim_refusal(&mut store, Platform::TikTok, "acct-a", BASE_MS + wait);
        assert_eq!(
            decision.refusal_code(),
            Some(RefusalCode::HumanActionRequired),
            "time alone must not clear a suspension ({wait} ms later)"
        );
    }
}

#[test]
fn youtube_quota_stops_until_pacific_midnight() {
    let dir = TempDir::new("quota");
    let mut store = open(&dir);
    // BASE_MS is 22:13:20 UTC on 2023-11-14, i.e. 14:13 PST.
    let outcome = store
        .record_hit(
            Platform::YouTube,
            "acct-a",
            BASE_MS,
            PlatformResponse::QuotaExhausted {
                retry_after_ms: None,
                code: Some("quotaExceeded".to_owned()),
            },
        )
        .unwrap();
    let HitOutcome::Cooling(applied) = outcome else {
        panic!("expected cooling");
    };
    let midnight = xpst_limits::next_pacific_midnight_ms(BASE_MS);
    assert_eq!(applied.until_ms, midnight);
    assert!(
        midnight > BASE_MS + 9 * HOUR,
        "the stop must reach midnight, not the one-minute ladder rung"
    );
}

#[test]
fn a_corrupt_state_file_fails_closed_and_is_quarantined() {
    let dir = TempDir::new("corrupt");
    fs::write(dir.join(STATE_FILE_NAME), b"{ this is not json at all").unwrap();

    let mut store = open(&dir);
    let integrity = store.integrity().clone();
    assert!(
        integrity.is_degraded(),
        "corruption must degrade: {integrity:?}"
    );
    assert!(
        integrity.quarantined.is_some(),
        "the bad file must be kept for diagnosis"
    );
    assert!(
        integrity
            .quarantined
            .as_ref()
            .is_some_and(|path| path.exists()),
        "the quarantined file must exist"
    );
    assert!(
        integrity.persisted,
        "the fail-closed marker must be on disk, or a restart clears it"
    );

    let decision = claim_refusal(&mut store, Platform::X, "acct-a", BASE_MS + MINUTE);
    assert_eq!(
        decision.refusal_code(),
        Some(RefusalCode::StateCorruptFailClosed),
        "{decision}"
    );

    drop(store);
    // A restart must not clear it: the fail-closed marker is on disk.
    let mut reopened = open(&dir);
    assert!(reopened.integrity().is_degraded());
    let again = claim_refusal(&mut reopened, Platform::YouTube, "acct-a", BASE_MS + HOUR);
    assert_eq!(
        again.refusal_code(),
        Some(RefusalCode::StateCorruptFailClosed)
    );
}

#[test]
fn corruption_that_is_not_json_shaped_also_fails_closed() {
    let dir = TempDir::new("corrupt-shape");
    // Valid JSON, wrong shape: a plausible-looking file that is not a ledger.
    fs::write(
        dir.join(STATE_FILE_NAME),
        b"{\"version\":1,\"accounts\":[]}",
    )
    .unwrap();
    let store = open(&dir);
    assert!(store.integrity().is_degraded());
    assert!(store.integrity().detail.contains("accounts"));
}

#[test]
fn a_newer_schema_version_is_refused_without_being_deleted() {
    let dir = TempDir::new("future-version");
    let payload = br#"{"version":99,"accounts":{},"assets":{}}"#;
    fs::write(dir.join(STATE_FILE_NAME), payload).unwrap();
    let mut store = open(&dir);
    let integrity = store.integrity().clone();
    assert!(integrity.is_degraded());
    assert!(
        integrity.quarantined.is_none(),
        "another build's state must not be taken away from it"
    );

    // A write attempt must not replace the newer file either: refusing and
    // rewriting is data loss with a friendly message.
    let decision = claim_refusal(&mut store, Platform::X, "acct-a", BASE_MS + MINUTE);
    assert_eq!(
        decision.refusal_code(),
        Some(RefusalCode::StateCorruptFailClosed)
    );
    store
        .record_hit(Platform::X, "acct-a", BASE_MS, throttled(None))
        .unwrap();
    assert_eq!(
        fs::read(dir.join(STATE_FILE_NAME)).unwrap(),
        payload.to_vec(),
        "the file is left exactly as found"
    );
}

#[test]
fn writes_are_atomic_and_leave_no_temporary_files() {
    let dir = TempDir::new("atomic");
    let mut store = open(&dir);
    for step in 0..5 {
        store
            .claim(
                &Request::new(Platform::X, "acct-a", BASE_MS + i64::from(step) * HOUR)
                    .with_asset("fic-content-hash-0001"),
            )
            .unwrap();
    }
    let leftovers: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.contains(".tmp-"))
        .collect();
    assert!(
        leftovers.is_empty(),
        "temporary files left behind: {leftovers:?}"
    );
    // The file itself is valid JSON ending with a newline.
    let text = fs::read_to_string(dir.join(STATE_FILE_NAME)).unwrap();
    assert!(text.ends_with('\n'));
    serde_json::from_str::<serde_json::Value>(&text).expect("state file is valid JSON");
}

#[test]
fn unknown_account_keys_from_another_build_are_preserved() {
    let dir = TempDir::new("retained");
    let payload = serde_json::json!({
        "version": 1,
        "degraded_until_ms": 0,
        "corrupt_detail": "",
        "accounts": {
            "x|acct-a": {"strikes": 1, "until_ms": BASE_MS + 60_000},
            "future_platform|acct-a": {"whatever": true}
        },
        "assets": {}
    });
    fs::write(
        dir.join(STATE_FILE_NAME),
        serde_json::to_vec(&payload).unwrap(),
    )
    .unwrap();

    let mut store = open(&dir);
    assert!(!store.integrity().is_degraded());
    store
        .claim(&Request::new(Platform::X, "acct-b", BASE_MS).with_asset("fic-hash-0002"))
        .unwrap();

    let written: serde_json::Value =
        serde_json::from_slice(&fs::read(dir.join(STATE_FILE_NAME)).unwrap()).unwrap();
    assert_eq!(
        written["accounts"]["future_platform|acct-a"],
        serde_json::json!({"whatever": true}),
        "an unknown platform's state must survive a write by this build"
    );
}

#[test]
fn an_absurd_persisted_deadline_is_clamped_to_the_honoured_bound() {
    let dir = TempDir::new("clamp");
    let payload = serde_json::json!({
        "version": 1,
        "degraded_until_ms": 0,
        "corrupt_detail": "",
        "accounts": {"x|acct-a": {"strikes": 3, "until_ms": BASE_MS + 1000 * DAY}},
        "assets": {}
    });
    fs::write(
        dir.join(STATE_FILE_NAME),
        serde_json::to_vec(&payload).unwrap(),
    )
    .unwrap();

    let mut store = open(&dir);
    let decision = claim_refusal(&mut store, Platform::X, "acct-a", BASE_MS);
    let Decision::Refuse { retry_after_ms, .. } = decision else {
        panic!("a decade-long deadline still refuses: {decision}");
    };
    assert_eq!(
        retry_after_ms,
        Some(xpst_limits::MAX_DEFERRED_RETRY_MS),
        "a corrupt deadline cools for the longest waitable window, not for a decade"
    );
}

#[test]
fn status_reports_every_account_and_its_remaining_window() {
    let dir = TempDir::new("status");
    let mut store = open(&dir);
    store
        .claim(&Request::new(Platform::X, "acct-a", BASE_MS).with_asset("fic-hash-0003"))
        .unwrap();
    store
        .record_hit(Platform::Facebook, "acct-b", BASE_MS, throttled(None))
        .unwrap();

    let statuses = store.status(BASE_MS + MINUTE);
    // x/acct-a published, facebook/acct-b took the hit, and the Meta family
    // extension opened records for instagram and threads.
    assert_eq!(statuses.len(), 4, "{statuses:?}");
    let x = statuses
        .iter()
        .find(|status| status.platform == Platform::X)
        .expect("the publish is recorded");
    assert_eq!(x.account, "acct-a");
    assert_eq!(x.publishes_24h, 1);
    assert!(x.last_publish_ms.is_some());

    let facebook = statuses
        .iter()
        .find(|status| status.platform == Platform::Facebook)
        .expect("the hit is recorded");
    assert_eq!(facebook.account, "acct-b");
    assert_eq!(
        facebook.cooling_until_ms,
        BASE_MS + DAY,
        "Facebook's policy floor is a 24 h stop"
    );
    assert_eq!(facebook.remaining_ms, DAY - MINUTE);
    assert!(facebook.last_reason.contains("throttled"), "{facebook:?}");

    let instagram = statuses
        .iter()
        .find(|status| status.platform == Platform::Instagram);
    assert!(
        instagram.is_some(),
        "the family window opens a record for each sibling"
    );
}

#[test]
fn invalid_identifiers_are_rejected() {
    let dir = TempDir::new("identifiers");
    let mut store = open(&dir);
    let bad = Request::new(Platform::X, "acct|a", BASE_MS);
    assert!(store.claim(&bad).is_err());
    let empty = Request::new(Platform::X, "", BASE_MS);
    assert!(store.claim(&empty).is_err());
}
