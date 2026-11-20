//! The CSRF `state` check, against both redirect-shaped flows.

use xpst_authflow::{
    AppPasswordFlow, AuthFlowError, AuthMode, AuthPlatform, Begin, CallbackInput, CredentialFlow,
    FlowConfig, FlowOutcome, HeadlessPasteFlow, LoopbackRedirectFlow,
};

fn x_flow() -> HeadlessPasteFlow {
    let config = FlowConfig::new(
        AuthPlatform::X,
        "fic-x-client-id",
        "https://fic.example.invalid/callback",
        AuthMode::ApiV2,
    )
    .with_scopes(["tweet.read", "tweet.write"]);
    HeadlessPasteFlow::new(config).expect("X paste flow is supported")
}

fn youtube_flow() -> LoopbackRedirectFlow {
    let config = FlowConfig::new(
        AuthPlatform::YouTube,
        "fic-google-client-id.apps.googleusercontent.com",
        "http://127.0.0.1:8085/callback",
        AuthMode::OauthLoopback,
    );
    LoopbackRedirectFlow::new(config).expect("YouTube loopback flow is supported")
}

fn redirect(request: &xpst_authflow::AuthorizeRequest) -> Option<&str> {
    if request.url.is_empty() {
        None
    } else {
        Some(request.redirect_uri.as_str())
    }
}

#[test]
fn headless_flow_rejects_a_mismatched_state() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("a paste flow must begin with a redirect");
    };
    let redirect_uri = redirect(&request).expect("redirect URI");
    let forged = format!("{redirect_uri}?code=fic-code&state=forged-state");

    let error = flow
        .complete(CallbackInput::RedirectUrl(&forged))
        .expect_err("mismatched state must be rejected");
    assert!(matches!(error, AuthFlowError::StateMismatch));

    // The flow stays usable for the real redirect: a typo must not poison it.
    let real = format!("{redirect_uri}?code=fic-code&state={}", request.state);
    let outcome = flow
        .complete(CallbackInput::RedirectUrl(&real))
        .expect("the correct state must be accepted");
    match outcome {
        FlowOutcome::AuthorizationCode(code) => {
            assert_eq!(code.code.expose(), "fic-code");
            assert_eq!(code.platform, AuthPlatform::X);
        }
        FlowOutcome::StaticCredential(_) => panic!("redirect flow returned a static credential"),
    }
}

#[test]
fn headless_flow_rejects_a_missing_state() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("a paste flow must begin with a redirect");
    };
    let no_state = format!("{}?code=fic-code", request.redirect_uri);
    assert!(matches!(
        flow.complete(CallbackInput::RedirectUrl(&no_state)),
        Err(AuthFlowError::StateMismatch)
    ));
}

#[test]
fn loopback_flow_rejects_a_mismatched_state() {
    let mut flow = youtube_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("a loopback flow must begin with a redirect");
    };
    let forged = format!(
        "{}?code=fic-google-code&state=not-{}",
        request.redirect_uri, request.state
    );
    assert!(matches!(
        flow.complete(CallbackInput::RedirectUrl(&forged)),
        Err(AuthFlowError::StateMismatch)
    ));
}

#[test]
fn redirect_to_another_host_is_rejected_before_the_state_is_read() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("a paste flow must begin with a redirect");
    };
    let elsewhere = format!(
        "https://attacker.example.invalid/callback?code=fic-code&state={}",
        request.state
    );
    assert!(matches!(
        flow.complete(CallbackInput::RedirectUrl(&elsewhere)),
        Err(AuthFlowError::RedirectUriMismatch { .. })
    ));
}

#[test]
fn provider_error_is_surfaced_after_state_verification() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("a paste flow must begin with a redirect");
    };
    let denied = format!(
        "{}?error=access_denied&error_description=User%20refused&state={}",
        request.redirect_uri, request.state
    );
    match flow.complete(CallbackInput::RedirectUrl(&denied)) {
        Err(AuthFlowError::ProviderError { code, description }) => {
            assert_eq!(code, "access_denied");
            assert_eq!(description, "User refused");
        }
        other => panic!("expected ProviderError, got {other:?}"),
    }
}

#[test]
fn static_flow_rejects_a_redirect_url() {
    let config = FlowConfig::new(AuthPlatform::Bluesky, "", "", AuthMode::AppPassword);
    let mut flow = AppPasswordFlow::new(config).expect("bluesky static flow");
    flow.begin().expect("begin");
    assert!(matches!(
        flow.complete(CallbackInput::RedirectUrl(
            "https://fic.example.invalid/callback?code=fic"
        )),
        Err(AuthFlowError::WrongInputSecret)
    ));
}

#[test]
fn flows_are_single_use() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("redirect expected");
    };
    assert!(matches!(flow.begin(), Err(AuthFlowError::AlreadyStarted)));
    let redirect = format!(
        "{}?code=fic-code&state={}",
        request.redirect_uri, request.state
    );
    flow.complete(CallbackInput::RedirectUrl(&redirect))
        .expect("first completion");
    assert!(matches!(
        flow.complete(CallbackInput::RedirectUrl(&redirect)),
        Err(AuthFlowError::AlreadyCompleted)
    ));
}
