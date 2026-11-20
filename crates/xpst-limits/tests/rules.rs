//! Every rule of the safe operating envelope, at its boundary.
//!
//! These are the tests that would fail if the crate stopped protecting an
//! account: each one names the rule it covers, and each one asserts the exact
//! refusal code rather than "not allowed", so a rule cannot be silently swapped
//! for a stricter or looser neighbour.

mod support;

use support::{TempDir, BASE_MS, DAY, HOUR, MINUTE};

use xpst_limits::{
    evaluate, AccountView, Decision, LimitsStore, Platform, PlatformResponse, RefusalCode, Request,
    Rule, View,
};

fn request(platform: Platform, account: &str, at_ms: i64) -> Request<'_> {
    Request::new(platform, account, at_ms).with_asset("fic-content-hash-0001")
}

fn open(dir: &TempDir) -> LimitsStore {
    LimitsStore::open_at(dir.path(), BASE_MS).expect("open ledger")
}

/// A store that has already published once, so the interval rule is armed.
fn store_with_one_publish(dir: &TempDir) -> LimitsStore {
    let mut store = open(dir);
    let allow = store
        .claim(&request(Platform::X, "acct-a", BASE_MS))
        .unwrap();
    assert!(allow.is_allowed(), "first publish must be allowed: {allow}");
    store
}

#[test]
fn a_fresh_account_is_allowed() {
    let dir = TempDir::new("fresh");
    let mut store = open(&dir);
    let decision = store
        .claim(&request(Platform::YouTube, "acct-a", BASE_MS))
        .unwrap();
    assert!(decision.is_allowed(), "{decision}");
}

#[test]
fn the_minimum_interval_defers_instead_of_refusing() {
    let dir = TempDir::new("interval");
    let mut store = store_with_one_publish(&dir);

    let decision = store
        .claim(&request(Platform::X, "acct-a", BASE_MS + 5 * MINUTE))
        .unwrap();
    let Decision::Defer {
        until_ms,
        retry_after_ms,
        rule,
    } = decision
    else {
        panic!("five minutes after a twenty-minute floor must defer, got {decision}");
    };
    assert_eq!(rule, Rule::JitteredMinimumInterval);
    assert_eq!(until_ms, BASE_MS + 5 * MINUTE + retry_after_ms);

    // The instant is inside the platform's band: at least the documented floor,
    // at most the top of the jitter band.
    let envelope = xpst_limits::envelope(Platform::X);
    let gap = until_ms - BASE_MS;
    assert!(
        (envelope.min_interval_ms()..=envelope.max_interval_ms()).contains(&gap),
        "gap {gap} ms outside [{}, {}]",
        envelope.min_interval_ms(),
        envelope.max_interval_ms()
    );
}

#[test]
fn a_deferral_does_not_drift_between_polls() {
    let dir = TempDir::new("defer-stable");
    let mut store = store_with_one_publish(&dir);
    let first = store
        .claim(&request(Platform::X, "acct-a", BASE_MS + MINUTE))
        .unwrap();
    let second = store
        .claim(&request(Platform::X, "acct-a", BASE_MS + MINUTE))
        .unwrap();
    assert_eq!(
        first, second,
        "the same question at the same instant must give the same answer"
    );
}

#[test]
fn the_daily_cap_refuses_and_reports_when_it_frees() {
    let dir = TempDir::new("daily-cap");
    let mut store = open(&dir);
    let cap = xpst_limits::envelope(Platform::YouTube).daily_cap;

    for step in 0..cap {
        let decision = store
            .claim(&request(
                Platform::YouTube,
                "acct-a",
                BASE_MS + i64::from(step) * 2 * HOUR,
            ))
            .unwrap();
        assert!(
            decision.is_allowed(),
            "post {} of {cap}: {decision}",
            step + 1
        );
    }

    let over = BASE_MS + i64::from(cap) * 2 * HOUR;
    let decision = store
        .claim(&request(Platform::YouTube, "acct-a", over))
        .unwrap();
    let Decision::Refuse {
        code,
        rule,
        retry_after_ms,
        ..
    } = decision
    else {
        panic!("the {cap}th post of the day must be refused, got {decision}");
    };
    assert_eq!(code, RefusalCode::DailyCapReached);
    assert_eq!(rule, Rule::DailyCap);
    let wait = retry_after_ms.expect("a daily cap knows when it lifts");
    assert_eq!(
        wait,
        BASE_MS + DAY - over,
        "the refusal points at the instant the oldest post leaves the window"
    );
}

#[test]
fn the_same_asset_is_refused_on_a_second_account() {
    let dir = TempDir::new("same-asset");
    let mut store = open(&dir);

    let first = store
        .claim(&request(Platform::X, "acct-a", BASE_MS))
        .unwrap();
    assert!(first.is_allowed(), "{first}");

    let second = store
        .claim(&request(Platform::X, "acct-b", BASE_MS + HOUR))
        .unwrap();
    let Decision::Refuse {
        code, rule, detail, ..
    } = second
    else {
        panic!("the same asset on a second account must be refused, got {second}");
    };
    assert_eq!(code, RefusalCode::SameAssetAcrossAccounts);
    assert_eq!(rule, Rule::SameAssetAcrossAccounts);
    assert!(
        detail.contains("acct-a"),
        "the refusal must name the account that already published it: {detail}"
    );

    // The same account may repeat its own asset — rule 1 is about two accounts.
    let again = store
        .claim(&request(Platform::X, "acct-a", BASE_MS + 2 * HOUR))
        .unwrap();
    assert!(again.is_allowed(), "own-account repost: {again}");

    // And the record is queryable for a reporter.
    assert_eq!(
        store.asset_accounts(Platform::X, "fic-content-hash-0001"),
        vec!["acct-a".to_owned()]
    );
}

#[test]
fn cross_platform_reuse_of_an_asset_is_fine() {
    let dir = TempDir::new("cross-platform");
    let mut store = open(&dir);
    assert!(store
        .claim(&request(Platform::YouTube, "acct-a", BASE_MS))
        .unwrap()
        .is_allowed());
    let other = store
        .claim(&request(Platform::X, "acct-a", BASE_MS + MINUTE))
        .unwrap();
    assert!(
        other.is_allowed(),
        "the same video on YouTube and X is explicitly allowed: {other}"
    );
}

#[test]
fn publishing_without_an_asset_id_is_refused() {
    let dir = TempDir::new("no-asset");
    let mut store = open(&dir);
    let anonymous = Request::new(Platform::X, "acct-a", BASE_MS);
    let decision = store.claim(&anonymous).unwrap();
    assert_eq!(
        decision.refusal_code(),
        Some(RefusalCode::UnidentifiedAsset),
        "rule 1 cannot be enforced without an asset id: {decision}"
    );
    assert!(
        decision
            .to_string()
            .contains(Rule::SameAssetAcrossAccounts.id()),
        "{decision}"
    );
}

#[test]
fn an_active_cooldown_is_a_refusal_not_a_deferral() {
    let dir = TempDir::new("cooldown-refuse");
    let mut store = open(&dir);
    store
        .record_hit(
            Platform::X,
            "acct-a",
            BASE_MS,
            PlatformResponse::Throttled {
                retry_after_ms: None,
                code: Some("429".to_owned()),
            },
        )
        .unwrap();

    let decision = store
        .claim(&request(Platform::X, "acct-a", BASE_MS + 30_000))
        .unwrap();
    let Decision::Refuse {
        code,
        rule,
        retry_after_ms,
        ..
    } = decision
    else {
        panic!("a cooldown must refuse, never defer: {decision}");
    };
    assert_eq!(code, RefusalCode::CooldownActive);
    assert_eq!(rule, Rule::ChallengeHardStop);
    assert_eq!(retry_after_ms, Some(30_000));

    // At the deadline it lifts without anything else happening.
    let at_deadline = store
        .claim(&request(Platform::X, "acct-a", BASE_MS + 60_000))
        .unwrap();
    assert!(at_deadline.is_allowed(), "{at_deadline}");
}

#[test]
fn a_human_stop_outranks_everything_else() {
    let dir = TempDir::new("human");
    let mut store = open(&dir);
    store
        .record_hit(
            Platform::Instagram,
            "acct-a",
            BASE_MS,
            PlatformResponse::Challenge {
                retry_after_ms: None,
                code: Some("ChallengeRequired".to_owned()),
            },
        )
        .unwrap();

    // Even with the cooldown window expired, the stop stands until a human
    // clears it.
    let decision = store
        .claim(&request(Platform::Instagram, "acct-a", BASE_MS + 3 * DAY))
        .unwrap();
    assert_eq!(
        decision.refusal_code(),
        Some(RefusalCode::HumanActionRequired),
        "{decision}"
    );
    assert_eq!(
        decision.refusal_code().map(RefusalCode::rule),
        Some(Rule::ChallengeHardStop)
    );

    assert!(store
        .clear_human_stop(Platform::Instagram, "acct-a", BASE_MS + 3 * DAY)
        .unwrap());
    let after = store
        .claim(&request(Platform::Instagram, "acct-a", BASE_MS + 3 * DAY))
        .unwrap();
    assert!(after.is_allowed(), "a cleared stop publishes: {after}");
}

#[test]
fn corrupt_state_refuses_every_platform() {
    let view = View {
        degraded_until_ms: BASE_MS + HOUR,
        account: AccountView::default(),
    };
    for platform in Platform::ALL {
        let decision = evaluate(&request(platform, "acct-a", BASE_MS), &view);
        assert_eq!(
            decision.refusal_code(),
            Some(RefusalCode::StateCorruptFailClosed),
            "{platform} must fail closed"
        );
        assert_eq!(
            decision.refusal_code().map(RefusalCode::rule),
            Some(Rule::StateIntegrity)
        );
    }
    // And once the fail-closed window has passed, it clears.
    let later = evaluate(&request(Platform::X, "acct-a", BASE_MS + HOUR), &view);
    assert!(later.is_allowed(), "{later}");
}

#[test]
fn a_backwards_clock_can_postpone_but_never_unlock() {
    let dir = TempDir::new("skew");
    let mut store = store_with_one_publish(&dir);

    // The clock now reads ten minutes *before* the recorded publish. Treating
    // that as "ten minutes ago" would unlock the account immediately.
    let skewed = store
        .claim(&request(Platform::X, "acct-a", BASE_MS - 10 * MINUTE))
        .unwrap();
    let Decision::Defer { until_ms, .. } = skewed else {
        panic!("a backwards clock must not unlock the interval: {skewed}");
    };
    assert!(until_ms > BASE_MS - 10 * MINUTE);

    // Once the clock catches up past the jittered interval, the account is due.
    let envelope = xpst_limits::envelope(Platform::X);
    let caught_up = store
        .claim(&request(
            Platform::X,
            "acct-a",
            BASE_MS + envelope.max_interval_ms(),
        ))
        .unwrap();
    assert!(caught_up.is_allowed(), "{caught_up}");
}

#[test]
fn a_cooldown_on_one_family_member_does_not_block_another_platform() {
    let dir = TempDir::new("isolation");
    let mut store = open(&dir);
    store
        .record_hit(
            Platform::Instagram,
            "acct-a",
            BASE_MS,
            PlatformResponse::Throttled {
                retry_after_ms: None,
                code: None,
            },
        )
        .unwrap();

    let youtube = store
        .claim(&request(Platform::YouTube, "acct-a", BASE_MS))
        .unwrap();
    assert!(
        youtube.is_allowed(),
        "one cooling destination must not block another: {youtube}"
    );
    let instagram = store
        .claim(&request(Platform::Instagram, "acct-a", BASE_MS))
        .unwrap();
    assert_eq!(instagram.refusal_code(), Some(RefusalCode::CooldownActive));
}

#[test]
fn refusal_messages_name_the_rule() {
    let dir = TempDir::new("display");
    let mut store = open(&dir);
    store
        .record_hit(
            Platform::TikTok,
            "acct-a",
            BASE_MS,
            PlatformResponse::Throttled {
                retry_after_ms: None,
                code: Some("spam_risk_too_many_posts".to_owned()),
            },
        )
        .unwrap();
    let decision = store
        .claim(&request(Platform::TikTok, "acct-a", BASE_MS + MINUTE))
        .unwrap();
    let rendered = decision.to_string();
    assert!(rendered.starts_with("REFUSE COOLDOWN_ACTIVE"), "{rendered}");
    assert!(
        rendered.contains("rule-3-challenge-hard-stop"),
        "a refusal that does not name its rule is unusable in a log: {rendered}"
    );
    assert!(rendered.contains("spam_risk_too_many_posts"), "{rendered}");
}
