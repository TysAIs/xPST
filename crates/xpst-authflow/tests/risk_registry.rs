//! The ban-risk registry must never let an unofficial mode read as safe, even
//! after an operator acknowledges it.

use tempfile::TempDir;
use xpst_auth::{BanRisk, Severity};
use xpst_authflow::risk::{
    classify, evaluate, record_acknowledgement, Acknowledgements, AuthPathVerdict, RiskError,
    ADVISORY_CODE_UNCLASSIFIED, UNOFFICIAL_AUTH_PATHS,
};
use xpst_authflow::{AuthMode, AuthPlatform};

#[test]
fn every_registered_unofficial_mode_is_unsafe_and_carries_an_advisory() {
    assert!(!UNOFFICIAL_AUTH_PATHS.is_empty());
    for path in UNOFFICIAL_AUTH_PATHS {
        let verdict = classify(path.platform, path.auth_mode);
        assert!(
            !verdict.is_safe(),
            "{} {} read as safe",
            path.platform,
            path.auth_mode
        );
        assert!(verdict.requires_acknowledgement());
        let advisory = verdict
            .advisory()
            .expect("unofficial path needs an advisory");
        assert_eq!(advisory.code, path.ban_risk.advisory_code());
        assert_eq!(advisory.severity, path.severity);
        assert!(advisory.message.contains(path.display_name));
        assert!(advisory.message.contains("Nothing has recorded"));
        assert!(!advisory.message.to_ascii_lowercase().contains("is safe"));
    }
}

#[test]
fn acknowledging_an_unofficial_mode_never_makes_it_safe() {
    let dir = TempDir::new().expect("tempdir");
    let acknowledgement = record_acknowledgement(
        dir.path(),
        AuthPlatform::Instagram,
        AuthMode::Session,
        "test-surface",
        "2026-11-17T00:00:00Z",
    )
    .expect("record");
    assert!(acknowledgement.operator_choice);

    let stored = Acknowledgements::load(dir.path());
    let verdict = evaluate(AuthPlatform::Instagram, AuthMode::Session, &stored);
    assert!(!verdict.is_safe());
    assert!(verdict.requires_acknowledgement());
    let advisory = verdict.advisory().expect("still an advisory");
    assert!(advisory
        .message
        .contains("acknowledged this path on 2026-11-17T00:00:00Z"));
    assert_eq!(
        verdict
            .acknowledgement()
            .map(|entry| entry.auth_mode.as_str()),
        Some("session")
    );
}

#[test]
fn an_acknowledgement_covers_only_the_mode_it_was_given_for() {
    let dir = TempDir::new().expect("tempdir");
    record_acknowledgement(
        dir.path(),
        AuthPlatform::Instagram,
        AuthMode::Session,
        "test-surface",
        "2026-11-17T00:00:00Z",
    )
    .expect("record");

    let stored = Acknowledgements::load(dir.path());
    assert!(
        stored
            .covering(AuthPlatform::Instagram, AuthMode::GraphApi)
            .is_none(),
        "accepting session must not accept graph_api"
    );
    assert!(stored
        .covering(AuthPlatform::Instagram, AuthMode::Session)
        .is_some());
    let official = evaluate(AuthPlatform::Instagram, AuthMode::GraphApi, &stored);
    assert!(official.is_safe());
}

#[test]
fn recording_an_acknowledgement_refuses_a_mode_that_is_not_the_unofficial_one() {
    let dir = TempDir::new().expect("tempdir");
    assert!(matches!(
        record_acknowledgement(
            dir_path(&dir),
            AuthPlatform::Instagram,
            AuthMode::GraphApi,
            "test-surface",
            "2026-11-17T00:00:00Z",
        ),
        Err(RiskError::NotUnofficialMode { .. })
    ));
    assert!(matches!(
        record_acknowledgement(
            dir_path(&dir),
            AuthPlatform::Bluesky,
            AuthMode::AppPassword,
            "test-surface",
            "2026-11-17T00:00:00Z",
        ),
        Err(RiskError::UnknownPlatform { .. })
    ));
    assert!(Acknowledgements::load(dir.path()).is_empty());
}

#[test]
fn official_modes_are_safe_and_unregistered_platforms_are_official() {
    let safe = [
        (AuthPlatform::Instagram, AuthMode::GraphApi),
        (AuthPlatform::X, AuthMode::ApiV2),
        (AuthPlatform::Bluesky, AuthMode::AppPassword),
        (AuthPlatform::Mastodon, AuthMode::PersonalAccessToken),
        (AuthPlatform::YouTube, AuthMode::OauthLoopback),
    ];
    for (platform, mode) in safe {
        let verdict = classify(platform, mode);
        assert!(verdict.is_safe(), "{platform} {mode} should be safe");
        assert!(verdict.advisory().is_none());
        assert!(!verdict.requires_acknowledgement());
    }
}

#[test]
fn an_unknown_mode_on_a_risk_platform_fails_closed() {
    let verdict = classify(AuthPlatform::Instagram, AuthMode::AppPassword);
    assert!(matches!(verdict, AuthPathVerdict::Unclassified { .. }));
    assert!(!verdict.is_safe());
    let advisory = verdict.advisory().expect("unclassified needs an advisory");
    assert_eq!(advisory.code, ADVISORY_CODE_UNCLASSIFIED);
    assert_eq!(advisory.severity, Severity::High);
}

#[test]
fn the_registry_matches_the_python_codes_and_severities() {
    let instagram = UNOFFICIAL_AUTH_PATHS
        .iter()
        .find(|path| path.platform == AuthPlatform::Instagram)
        .expect("instagram entry");
    assert_eq!(instagram.auth_mode, AuthMode::Session);
    assert_eq!(instagram.ban_risk, BanRisk::UnofficialPrivateApi);
    assert_eq!(instagram.ban_risk.advisory_code(), "UNOFFICIAL_PRIVATE_API");
    assert_eq!(instagram.severity, Severity::High);

    let x = UNOFFICIAL_AUTH_PATHS
        .iter()
        .find(|path| path.platform == AuthPlatform::X)
        .expect("x entry");
    assert_eq!(x.auth_mode, AuthMode::Cookies);
    assert_eq!(x.ban_risk, BanRisk::UnofficialWebClient);
    assert_eq!(x.ban_risk.advisory_code(), "UNOFFICIAL_WEB_CLIENT");
    assert_eq!(x.severity, Severity::Advisory);
}

#[test]
fn a_corrupt_acknowledgement_file_reads_as_unacknowledged_not_as_a_crash() {
    let dir = TempDir::new().expect("tempdir");
    std::fs::write(
        dir.path().join("ban_risk_acknowledgements.json"),
        b"{not json",
    )
    .expect("write corrupt file");
    let stored = Acknowledgements::load(dir.path());
    assert!(stored.is_empty());
    let verdict = evaluate(AuthPlatform::X, AuthMode::Cookies, &stored);
    assert!(!verdict.is_safe());
    assert!(verdict
        .advisory()
        .expect("advisory")
        .message
        .contains("Nothing has recorded"));
}

fn dir_path(dir: &TempDir) -> &std::path::Path {
    dir.path()
}
