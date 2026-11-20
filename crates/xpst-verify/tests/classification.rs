//! The classification contract, exercised against fakes.
//!
//! No network: every provider answer below is a literal typed into the test, and
//! the only transport is a fake that records what it was asked to send. The
//! five cases the smoke has to get right are the five a human will actually
//! meet — nothing stored, an expired token, a refused token, a dead network,
//! and a working credential.
//!
//! One further rule is asserted here because it is the one that costs real
//! money when it breaks: exactly one request per platform. There is no retry,
//! so a fake that is asked twice fails the test.

use std::cell::RefCell;
use std::time::Duration;

use xpst_verify::{
    classify, plans, run, Attempt, Context, Plan, Platform, Request, Secret, Transport, Verdict,
};

/// A transport that answers from a script and counts what it was asked.
struct FakeTransport {
    answer: Attempt,
    sent: RefCell<Vec<String>>,
}

impl FakeTransport {
    fn answering(answer: Attempt) -> Self {
        Self {
            answer,
            sent: RefCell::new(Vec::new()),
        }
    }
}

impl Transport for FakeTransport {
    fn send(&self, request: &Request, _timeout: Duration) -> Attempt {
        self.sent
            .borrow_mut()
            .push(request.platform.slug().to_owned());
        self.answer.clone()
    }
}

fn call_plan(platform: Platform, token: &str) -> Plan {
    Plan::call(
        platform,
        Request::bearer(
            platform,
            "https://example.invalid/identity",
            Secret::new(token),
        ),
        false,
        None,
    )
}

#[test]
fn nothing_stored_is_no_credential() {
    let attempt = Attempt::NoCredential {
        detail: "accounts.instagram.graph_access_token is empty".to_owned(),
    };
    let verdict = classify(&attempt, Context::new(Platform::Instagram, false));
    assert_eq!(verdict.kind(), "no_credential");
    assert!(!verdict.proven_live());
    assert_eq!(
        verdict.detail(),
        "accounts.instagram.graph_access_token is empty"
    );
}

#[test]
fn a_401_with_a_refresh_token_is_expired_not_rejected() {
    // The shape Google actually returns for a stale access token.
    let attempt = Attempt::Status {
        code: 401,
        body: r#"{"error":{"code":401,"message":"Request had invalid authentication credentials.","status":"UNAUTHENTICATED"}}"#
            .to_owned(),
    };
    let verdict = classify(&attempt, Context::new(Platform::YouTube, true));
    assert_eq!(verdict.kind(), "expired");
    assert!(verdict.detail().contains("HTTP 401"));
    assert!(verdict
        .detail()
        .contains("invalid authentication credentials"));
    assert!(verdict.detail().contains("refresh available"));
    assert!(!verdict.proven_live());
}

#[test]
fn a_401_without_a_refresh_path_is_rejected() {
    // Same status, different fact: an instagrapi session cannot be renewed, so
    // calling it "expired" would promise a refresh that will never happen.
    let attempt = Attempt::Status {
        code: 401,
        body: r#"{"error":"invalid_token"}"#.to_owned(),
    };
    let verdict = classify(&attempt, Context::new(Platform::Instagram, false));
    assert_eq!(verdict.kind(), "rejected");
    assert!(verdict.detail().contains("no refresh path"));
}

#[test]
fn a_403_is_rejected_with_the_provider_reason() {
    let attempt = Attempt::Status {
        code: 403,
        body:
            r#"{"error":{"code":403,"message":"insufficient scope","status":"PERMISSION_DENIED"}}"#
                .to_owned(),
    };
    let verdict = classify(&attempt, Context::new(Platform::YouTube, true));
    assert_eq!(verdict.kind(), "rejected");
    assert!(verdict.detail().contains("insufficient scope"));
    assert!(verdict.detail().contains("call not permitted"));
}

#[test]
fn a_timeout_is_a_network_verdict_that_proves_nothing() {
    let verdict = classify(
        &Attempt::Timeout { millis: 10_000 },
        Context::new(Platform::X, false),
    );
    assert_eq!(verdict.kind(), "network");
    assert!(verdict.detail().contains("10000 ms"));
    assert!(!verdict.proven_live());
}

#[test]
fn a_transport_failure_is_network_and_does_not_echo_a_url() {
    let verdict = classify(
        &Attempt::Transport {
            detail: "connection failed (DNS, TLS or refused)".to_owned(),
        },
        Context::new(Platform::Instagram, false),
    );
    assert_eq!(verdict.kind(), "network");
    assert!(!verdict.detail().contains("graph.facebook.com"));
}

#[test]
fn a_200_names_the_account() {
    let attempt = Attempt::Status {
        code: 200,
        body: r#"{"items":[{"id":"UC-not-printed","snippet":{"title":"Demo Channel"}}]}"#
            .to_owned(),
    };
    let verdict = classify(&attempt, Context::new(Platform::YouTube, true));
    assert_eq!(verdict.kind(), "authenticated");
    assert!(verdict.proven_live());
    assert_eq!(verdict.detail(), "Demo Channel");
}

#[test]
fn a_200_without_a_label_is_still_authenticated() {
    let verdict = classify(
        &Attempt::Status {
            code: 200,
            body: r#"{"items":[]}"#.to_owned(),
        },
        Context::new(Platform::YouTube, true),
    );
    assert_eq!(verdict.kind(), "authenticated");
    assert!(verdict.detail().contains("no account label"));
}

#[test]
fn exactly_one_request_per_platform_even_when_it_fails() {
    let transport = FakeTransport::answering(Attempt::Timeout { millis: 5 });
    let report = run(
        vec![
            call_plan(Platform::YouTube, "t"),
            call_plan(Platform::Bluesky, "t"),
        ],
        &transport,
        Duration::from_millis(5),
    );
    assert_eq!(transport.sent.borrow().len(), 2, "one attempt per platform");
    assert_eq!(report.results.len(), 2);
    assert_eq!(report.proven(), 0);
    assert!(report.results.iter().all(|r| r.verdict.kind() == "network"));
}

#[test]
fn a_decided_plan_never_reaches_the_network() {
    let transport = FakeTransport::answering(Attempt::Timeout { millis: 5 });
    let report = run(
        vec![Plan::decided(
            Platform::X,
            Verdict::Unsupported {
                detail: "cookie session".to_owned(),
            },
        )],
        &transport,
        Duration::from_millis(5),
    );
    assert!(transport.sent.borrow().is_empty(), "nothing was sent");
    assert_eq!(report.results[0].verdict.kind(), "unsupported");
}

#[test]
fn no_secret_reaches_the_rendered_report() {
    const CREDENTIAL: &str = "tok_live_9f3c-secret-value";
    let transport = FakeTransport::answering(Attempt::Status {
        code: 200,
        body: r#"{"handle":"someone.bsky.social"}"#.to_owned(),
    });
    let report = run(
        vec![call_plan(Platform::Bluesky, CREDENTIAL)],
        &transport,
        Duration::from_millis(5),
    );
    let text = report.to_text();
    let json = report.to_json().to_string();
    for rendered in [text, json] {
        assert!(
            !rendered.contains(CREDENTIAL),
            "credential leaked into output"
        );
        assert!(!rendered.contains("tok_live"), "credential fragment leaked");
    }
    assert!(report.to_text().contains("someone.bsky.social"));
}

#[test]
fn an_unreadable_config_is_an_error_not_a_verdict() {
    let missing = std::path::Path::new("/nonexistent/xpst-profile");
    assert!(plans(missing).is_err());
}

#[test]
fn a_synthetic_profile_produces_one_call_plan_and_keeps_the_token_out_of_output() {
    // The whole auth path, driven from a directory that looks like an install:
    // config.yaml + the token file it points at. No network, no real profile.
    let dir = std::env::temp_dir().join(format!("xpst-verify-fixture-{}", std::process::id()));
    let credentials = dir.join("credentials");
    std::fs::create_dir_all(&credentials).expect("fixture dir");
    let token_file = credentials.join("youtube_token.json");
    std::fs::write(
        &token_file,
        r#"{"token":"fixture-access-token","refresh_token":"fixture-refresh","expiry":"2099-01-01T00:00:00Z"}"#,
    )
    .expect("fixture token");
    std::fs::write(
        dir.join("config.yaml"),
        format!(
            "version: 4\naccounts:\n  youtube:\n    enabled: true\n    token_file: \"{}\"\n  instagram:\n    enabled: false\n",
            token_file.display()
        ),
    )
    .expect("fixture config");

    let planned = plans(&dir).expect("plans from fixture");
    assert_eq!(planned.len(), 5, "one plan per platform");

    let youtube = planned
        .iter()
        .find(|p| p.platform == Platform::YouTube)
        .expect("youtube plan");
    match &youtube.action {
        xpst_verify::Action::Call { refreshable, .. } => {
            assert!(
                *refreshable,
                "a refresh token means a 401 is expired, not rejected"
            )
        }
        other => panic!("expected a call plan, got {other:?}"),
    }
    // The plan's Debug output is what a `dbg!` would print: it must not carry
    // the token.
    assert!(!format!("{youtube:?}").contains("fixture-access-token"));

    let transport = FakeTransport::answering(Attempt::Status {
        code: 200,
        body: r#"{"items":[{"id":"UCfixture","snippet":{"title":"Fixture Channel"}}]}"#.to_owned(),
    });
    let report = run(planned, &transport, Duration::from_millis(5));
    let rendered = report.to_text();
    assert!(!rendered.contains("fixture-access-token"));
    assert!(
        !rendered.contains("UCfixture"),
        "no account id in the report"
    );
    assert!(rendered.contains("Fixture Channel"));

    let youtube_row = report
        .results
        .iter()
        .find(|r| r.platform == Platform::YouTube)
        .expect("youtube row");
    assert_eq!(youtube_row.verdict.kind(), "authenticated");
    // Only YouTube was callable; the other four were decided locally.
    assert_eq!(transport.sent.borrow().len(), 1);

    std::fs::remove_dir_all(&dir).ok();
}
