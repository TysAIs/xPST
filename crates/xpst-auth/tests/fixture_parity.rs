//! Parity between the Rust truth table and the captured Python fixture.
//!
//! `tests/fixtures/auth_truth_table.json` is written by
//! `scripts/capture_auth_truth_table.py`, which RUNS the Python engine against
//! throwaway config profiles and records what it observed. Nothing here calls a
//! provider API: the fixture is data, and the table is a pure function of it.
//!
//! Two independent checks:
//!
//! 1. [`every_captured_row_is_reproduced`] feeds each captured row's inputs
//!    through [`evaluate`] and requires an exact match on every asserted field.
//! 2. [`config_shaped_profiles_reproduce_fixture_rows`] builds the same inputs
//!    from a typed, config-shaped description instead of the captured JSON, so
//!    the table is exercised as a function of configuration rather than of the
//!    fixture's serialized shape.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::Deserialize;
use xpst_auth::{
    evaluate, Badge, BanRisk, CredentialFacts, Platform, PlatformInput, ProbeDetails, ProbeResult,
    ProviderState, Role, Severity, TokenState, Verdict,
};

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("tests")
        .join("fixtures")
        .join("auth_truth_table.json")
}

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    now_epoch: f64,
    providers: BTreeMap<String, FixtureProvider>,
    rows: Vec<Row>,
}

#[derive(Debug, Deserialize)]
struct FixtureProvider {
    roles: Vec<String>,
    auth_mode: String,
}

#[derive(Debug, Deserialize)]
struct Row {
    id: String,
    platform: String,
    provenance: String,
    inputs: RowInputs,
    expected: Expected,
}

#[derive(Debug, Deserialize)]
struct RowInputs {
    facts: CredentialFacts,
    live: Option<ProbeResult>,
    source_live: Option<ProbeResult>,
    destination_live: Option<ProbeResult>,
    checked_at: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct Expected {
    provider_state: ProviderState,
    role_states: BTreeMap<String, ProviderState>,
    auth_mode: String,
    token_state: Option<TokenState>,
    badge: Option<Badge>,
    auto_refresh: Option<bool>,
    source_only: bool,
    can_post: bool,
    ban_risk: Option<ExpectedRisk>,
}

#[derive(Debug, Deserialize)]
struct ExpectedRisk {
    code: BanRisk,
    advisory_code: String,
    severity: Severity,
    enabled: bool,
    acknowledged: bool,
}

fn load_fixture() -> Fixture {
    let path = fixture_path();
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("cannot parse {}: {error}", path.display()))
}

/// Compare one row against the Rust verdict, naming every disagreement.
fn assert_row(row: &Row, verdict: &Verdict) {
    let mut mismatches: Vec<String> = Vec::new();
    if verdict.provider_state != row.expected.provider_state {
        mismatches.push(format!(
            "provider_state: rust {} != python {}",
            verdict.provider_state, row.expected.provider_state
        ));
    }
    if verdict.auth_mode != row.expected.auth_mode {
        mismatches.push(format!(
            "auth_mode: rust {} != python {}",
            verdict.auth_mode, row.expected.auth_mode
        ));
    }
    if verdict.token_state != row.expected.token_state {
        mismatches.push(format!(
            "token_state: rust {:?} != python {:?}",
            verdict.token_state, row.expected.token_state
        ));
    }
    if verdict.badge != row.expected.badge {
        mismatches.push(format!(
            "badge: rust {:?} != python {:?}",
            verdict.badge, row.expected.badge
        ));
    }
    if verdict.auto_refresh != row.expected.auto_refresh {
        mismatches.push(format!(
            "auto_refresh: rust {:?} != python {:?}",
            verdict.auto_refresh, row.expected.auto_refresh
        ));
    }
    if verdict.source_only != row.expected.source_only {
        mismatches.push(format!(
            "source_only: rust {} != python {}",
            verdict.source_only, row.expected.source_only
        ));
    }
    if verdict.can_post != row.expected.can_post {
        mismatches.push(format!(
            "can_post: rust {} != python {}",
            verdict.can_post, row.expected.can_post
        ));
    }

    let actual_roles: BTreeMap<String, ProviderState> = verdict
        .role_states
        .iter()
        .map(|(role, state)| (role.as_str().to_owned(), *state))
        .collect();
    if actual_roles != row.expected.role_states {
        mismatches.push(format!(
            "role_states: rust {actual_roles:?} != python {:?}",
            row.expected.role_states
        ));
    }

    match (&verdict.ban_risk, &row.expected.ban_risk) {
        (None, None) => {}
        (Some(actual), Some(expected)) => {
            if actual.code != expected.code
                || actual.advisory_code != expected.advisory_code
                || actual.severity != expected.severity
                || actual.enabled != expected.enabled
                || actual.acknowledged != expected.acknowledged
            {
                mismatches.push(format!("ban_risk: rust {actual:?} != python {expected:?}"));
            }
        }
        (actual, expected) => mismatches.push(format!(
            "ban_risk presence: rust {actual:?} != python {expected:?}"
        )),
    }

    assert!(
        mismatches.is_empty(),
        "row {} ({} / {}) disagrees with the captured Python fixture:\n  {}",
        row.id,
        row.platform,
        row.provenance,
        mismatches.join("\n  ")
    );
}

#[test]
fn fixture_is_the_expected_schema() {
    let fixture = load_fixture();
    assert_eq!(fixture.schema_version, 1);
    assert!(
        fixture.rows.len() >= 20,
        "the fixture must cover the state space, not a sample of it"
    );
    assert!(
        fixture
            .rows
            .iter()
            .any(|row| row.provenance == "unverified_no_credentials"),
        "rows needing credentials must be marked, not silently invented"
    );
}

#[test]
fn rust_roster_matches_the_captured_catalog() {
    let fixture = load_fixture();
    assert_eq!(
        fixture.providers.len(),
        Platform::ALL.len(),
        "the Rust roster must cover every captured provider"
    );
    for platform in Platform::ALL {
        let captured = fixture
            .providers
            .get(platform.as_str())
            .unwrap_or_else(|| panic!("{} missing from the fixture", platform.as_str()));
        let roles: Vec<&str> = platform.roles().iter().map(|role| role.as_str()).collect();
        assert_eq!(
            roles,
            captured.roles,
            "role roster for {}",
            platform.as_str()
        );
        assert_eq!(
            platform.default_auth_mode(),
            captured.auth_mode,
            "catalog auth mode for {}",
            platform.as_str()
        );
    }
}

#[test]
fn every_captured_row_is_reproduced() {
    let fixture = load_fixture();
    for row in &fixture.rows {
        let platform = Platform::parse(&row.platform)
            .unwrap_or_else(|| panic!("row {} names an unknown platform", row.id));
        let input = PlatformInput {
            platform,
            facts: row.inputs.facts.clone(),
            live: row.inputs.live.clone(),
            source_live: row.inputs.source_live.clone(),
            destination_live: row.inputs.destination_live.clone(),
            checked_at: row.inputs.checked_at,
            refresh_failed: false,
        };
        let verdict = evaluate(&input, fixture.now_epoch);
        assert_row(row, &verdict);
    }
}

/// Build inputs the way a config loader would: from presence flags and a
/// configured mode, never from a captured payload.
struct Profile {
    row: &'static str,
    platform: Platform,
    facts: fn(&mut CredentialFacts),
    probe: fn(&mut PlatformInput),
}

fn profile(
    row: &'static str,
    platform: Platform,
    facts: fn(&mut CredentialFacts),
    probe: fn(&mut PlatformInput),
) -> Profile {
    Profile {
        row,
        platform,
        facts,
        probe,
    }
}

fn no_probe(_: &mut PlatformInput) {}

fn youtube_token(facts: &mut CredentialFacts) {
    facts.enabled = true;
    facts.token_file_exists = true;
    facts.token_document_present = true;
    facts.token_configured = true;
}

fn youtube_token_with_refresh(facts: &mut CredentialFacts) {
    youtube_token(facts);
    facts.has_refresh_token = true;
}

fn live_ok(input: &mut PlatformInput) {
    input.live = Some(ProbeResult {
        authenticated: true,
        session_valid: Some(true),
        live_checked: true,
        ..ProbeResult::default()
    });
    input.checked_at = Some(FIXTURE_NOW);
}

fn live_expired(input: &mut PlatformInput) {
    input.live = Some(ProbeResult {
        authenticated: false,
        session_valid: Some(false),
        live_checked: true,
        error: Some("invalid_grant".to_owned()),
        ..ProbeResult::default()
    });
    input.checked_at = Some(FIXTURE_NOW);
}

/// The capture clock. Const so profiles can stamp the same instant the
/// fixture was captured at without reading the fixture's `now_epoch`.
const FIXTURE_NOW: f64 = 1_700_000_000.0;

#[test]
fn config_shaped_profiles_reproduce_fixture_rows() {
    let fixture = load_fixture();
    assert!((fixture.now_epoch - FIXTURE_NOW).abs() < f64::EPSILON);

    let profiles = vec![
        profile(
            "default/youtube",
            Platform::YouTube,
            |facts| facts.enabled = true,
            no_probe,
        ),
        profile(
            "default/bluesky",
            Platform::Bluesky,
            |facts| facts.enabled = false,
            no_probe,
        ),
        profile(
            "default/tiktok",
            Platform::TikTok,
            |facts| facts.enabled = true,
            no_probe,
        ),
        profile(
            "youtube-token-file-present",
            Platform::YouTube,
            youtube_token_with_refresh,
            no_probe,
        ),
        profile(
            "tiktok-source-username",
            Platform::TikTok,
            |facts| {
                facts.enabled = true;
                facts.username = true;
                facts.token_configured = true;
            },
            no_probe,
        ),
        profile(
            "tiktok-sandbox",
            Platform::TikTok,
            |facts| {
                facts.enabled = true;
                facts.sandbox = true;
                facts.client_key = true;
                facts.access_token = true;
                facts.token_configured = true;
            },
            no_probe,
        ),
        profile(
            "instagram-session-enabled-no-file",
            Platform::Instagram,
            |facts| {
                facts.enabled = true;
                facts.auth_mode = "session".to_owned();
            },
            no_probe,
        ),
        profile(
            "x-api-v2-bearer",
            Platform::X,
            |facts| {
                facts.enabled = true;
                facts.auth_mode = "api_v2".to_owned();
                facts.bearer_token = true;
                facts.token_configured = true;
            },
            no_probe,
        ),
        profile(
            "all-disabled/tiktok",
            Platform::TikTok,
            |facts| {
                facts.enabled = false;
                facts.username = true;
                facts.token_configured = true;
            },
            no_probe,
        ),
        profile(
            "youtube-live-ok",
            Platform::YouTube,
            youtube_token_with_refresh,
            live_ok,
        ),
        profile(
            "youtube-live-expired-refreshable",
            Platform::YouTube,
            youtube_token_with_refresh,
            live_expired,
        ),
        profile(
            "youtube-live-expired-no-refresh",
            Platform::YouTube,
            youtube_token,
            live_expired,
        ),
        profile(
            "x-live-unverified-probe",
            Platform::X,
            |facts| {
                facts.enabled = true;
                facts.auth_mode = "cookies".to_owned();
                facts.cookies_file_exists = true;
                facts.token_configured = true;
            },
            |input| {
                input.live = Some(ProbeResult {
                    authenticated: false,
                    session_valid: Some(false),
                    live_checked: true,
                    error: Some("http 429 rate limited".to_owned()),
                    details: ProbeDetails {
                        probe_class: Some("unverified".to_owned()),
                    },
                    ..ProbeResult::default()
                });
                input.checked_at = Some(FIXTURE_NOW);
            },
        ),
    ];

    for entry in profiles {
        let row = fixture
            .rows
            .iter()
            .find(|row| row.id == entry.row)
            .unwrap_or_else(|| panic!("fixture row {} is missing", entry.row));
        let mut input = PlatformInput::no_credentials(entry.platform);
        (entry.facts)(&mut input.facts);
        (entry.probe)(&mut input);
        let verdict = evaluate(&input, fixture.now_epoch);
        assert_row(row, &verdict);
        assert_eq!(
            verdict.role(Role::VideoDestination),
            row.expected.role_states.get("video_destination").copied(),
            "profile {} destination role",
            entry.row
        );
    }
}
