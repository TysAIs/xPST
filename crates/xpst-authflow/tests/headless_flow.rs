//! The headless (paste-the-redirect) flow and the static-secret flow: the
//! request they assemble, the platform refusals, and secret redaction.

use xpst_authflow::{
    s256_challenge, AppPasswordFlow, AuthFlowError, AuthMode, AuthPlatform, Begin, CallbackInput,
    CredentialFlow, FlowConfig, FlowOutcome, HeadlessPasteFlow, SecretString,
};

fn x_flow() -> HeadlessPasteFlow {
    let config = FlowConfig::new(
        AuthPlatform::X,
        "fic-x-client-id",
        "https://fic.example.invalid/callback",
        AuthMode::ApiV2,
    )
    .with_scopes(["tweet.read", "tweet.write", "users.read", "offline.access"])
    .with_param("prompt", "consent");
    HeadlessPasteFlow::new(config).expect("X supports the paste flow")
}

#[test]
fn headless_flow_assembles_the_rfc6749_authorize_url() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("a paste flow must begin with a redirect");
    };

    assert!(request.url.starts_with("https://x.com/i/oauth2/authorize?"));
    assert_eq!(request.flow, xpst_authflow::FlowKind::HeadlessPaste);
    assert_eq!(request.param("response_type").as_deref(), Some("code"));
    assert_eq!(
        request.param("client_id").as_deref(),
        Some("fic-x-client-id")
    );
    assert_eq!(
        request.param("redirect_uri").as_deref(),
        Some("https://fic.example.invalid/callback")
    );
    assert_eq!(
        request.param("scope").as_deref(),
        Some("tweet.read tweet.write users.read offline.access")
    );
    assert_eq!(
        request.param("code_challenge_method").as_deref(),
        Some("S256")
    );
    assert_eq!(
        request.param("state").as_deref(),
        Some(request.state.as_str())
    );
    assert_eq!(
        request.param("prompt").as_deref(),
        Some("consent"),
        "extra provider parameters must survive"
    );
    let challenge = request.param("code_challenge").expect("challenge");
    assert_eq!(challenge.len(), 43);
    assert!(!challenge.contains('='));
}

#[test]
fn completed_paste_yields_a_verifier_matching_the_url_challenge() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("redirect expected");
    };
    let challenge = request.param("code_challenge").expect("challenge");
    let redirect = format!(
        "{}?code=fic-authorization-code&state={}",
        request.redirect_uri, request.state
    );
    let FlowOutcome::AuthorizationCode(code) = flow
        .complete(CallbackInput::RedirectUrl(&redirect))
        .expect("correct state")
    else {
        panic!("redirect flow must yield an authorization code");
    };
    assert_eq!(code.code.expose(), "fic-authorization-code");
    assert_eq!(s256_challenge(code.code_verifier.expose()), challenge);

    let form = code.token_request_params("fic-x-client-id", None);
    let field = |name: &str| {
        form.iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    };
    assert_eq!(field("grant_type"), Some("authorization_code"));
    assert_eq!(field("code"), Some("fic-authorization-code"));
    assert_eq!(
        field("redirect_uri"),
        Some("https://fic.example.invalid/callback")
    );
    assert_eq!(
        field("code_verifier"),
        Some(code.code_verifier.expose()),
        "the exchange must carry the verifier"
    );
    assert_eq!(field("client_id"), Some("fic-x-client-id"));
    assert_eq!(field("state"), None, "state is not a token-endpoint field");
}

#[test]
fn secrets_are_redacted_from_debug_output() {
    let mut flow = x_flow();
    let Begin::Redirect(request) = flow.begin().expect("begin") else {
        panic!("redirect expected");
    };
    let redirect = format!(
        "{}?code=fic-secret-code&state={}",
        request.redirect_uri, request.state
    );
    let outcome = flow
        .complete(CallbackInput::RedirectUrl(&redirect))
        .expect("correct state");
    let rendered = format!("{outcome:?}");
    assert!(!rendered.contains("fic-secret-code"));
    assert!(rendered.contains("redacted"));
    assert!(!format!("{request:?}").contains(&request.state));
}

#[test]
fn x_cannot_be_configured_for_loopback() {
    let config = FlowConfig::new(
        AuthPlatform::X,
        "fic-x-client-id",
        "http://127.0.0.1:8085/callback",
        AuthMode::ApiV2,
    );
    match xpst_authflow::LoopbackRedirectFlow::new(config) {
        Err(AuthFlowError::UnsupportedFlow { reason, .. }) => {
            assert!(reason.contains("registered callback"));
        }
        Ok(_) => panic!("X must not be configurable as a loopback flow"),
        Err(other) => panic!("expected UnsupportedFlow, got {other}"),
    }
}

#[test]
fn meta_requires_an_https_redirect_uri() {
    let http = FlowConfig::new(
        AuthPlatform::Instagram,
        "fic-meta-client",
        "http://127.0.0.1:8085/callback",
        AuthMode::GraphApi,
    );
    assert!(matches!(
        HeadlessPasteFlow::new(http),
        Err(AuthFlowError::InvalidConfig(_))
    ));

    let https = FlowConfig::new(
        AuthPlatform::Instagram,
        "fic-meta-client",
        "https://fic.example.invalid/callback",
        AuthMode::GraphApi,
    );
    let flow = HeadlessPasteFlow::new(https).expect("HTTPS hosted redirect is allowed");
    assert_eq!(flow.platform(), AuthPlatform::Instagram);
    assert_eq!(flow.kind(), xpst_authflow::FlowKind::HeadlessPaste);
}

#[test]
fn bluesky_has_no_redirect_shape() {
    let config = FlowConfig::new(
        AuthPlatform::Bluesky,
        "",
        "https://fic.example.invalid/callback",
        AuthMode::AppPassword,
    );
    assert!(matches!(
        HeadlessPasteFlow::new(config),
        Err(AuthFlowError::UnsupportedFlow { .. })
    ));
}

#[test]
fn bluesky_prompt_names_the_app_password_settings_page() {
    let config = FlowConfig::new(AuthPlatform::Bluesky, "", "", AuthMode::AppPassword);
    let mut flow = AppPasswordFlow::new(config).expect("static flow");
    let Begin::CollectSecret(prompt) = flow.begin().expect("begin") else {
        panic!("static flow must collect a secret");
    };
    assert_eq!(prompt.secret_label, "App password");
    assert_eq!(prompt.help_url, "https://bsky.app/settings/app-passwords");
    assert!(prompt.identifier_required);

    let outcome = flow
        .complete(CallbackInput::Secret {
            identifier: Some("fic-handle.bsky.social"),
            secret: "fic-app-password",
        })
        .expect("complete");
    let FlowOutcome::StaticCredential(credential) = outcome else {
        panic!("static flow must yield a credential");
    };
    assert_eq!(credential.platform, AuthPlatform::Bluesky);
    assert_eq!(credential.auth_mode, AuthMode::AppPassword);
    assert_eq!(
        credential.identifier.as_deref(),
        Some("fic-handle.bsky.social")
    );
    assert_eq!(credential.access_token.expose(), "fic-app-password");
    assert!(!format!("{credential:?}").contains("fic-app-password"));
}

#[test]
fn mastodon_uses_a_personal_access_token() {
    let config = FlowConfig::new(
        AuthPlatform::Mastodon,
        "",
        "",
        AuthMode::PersonalAccessToken,
    );
    let mut flow = AppPasswordFlow::new(config).expect("static flow");
    let Begin::CollectSecret(prompt) = flow.begin().expect("begin") else {
        panic!("static flow must collect a secret");
    };
    assert_eq!(prompt.secret_label, "Personal access token");
    let outcome = flow
        .complete(CallbackInput::Secret {
            identifier: Some("https://fic.mastodon.invalid"),
            secret: "fic-access-token",
        })
        .expect("complete");
    assert!(matches!(outcome, FlowOutcome::StaticCredential(_)));
}

#[test]
fn static_flow_requires_its_identifier_and_secret() {
    let config = FlowConfig::new(AuthPlatform::Bluesky, "", "", AuthMode::AppPassword);
    let mut flow = AppPasswordFlow::new(config).expect("static flow");
    flow.begin().expect("begin");
    assert!(matches!(
        flow.complete(CallbackInput::Secret {
            identifier: None,
            secret: "fic-app-password"
        }),
        Err(AuthFlowError::InvalidInput(_))
    ));
    assert!(matches!(
        flow.complete(CallbackInput::Secret {
            identifier: Some("fic-handle.bsky.social"),
            secret: "   "
        }),
        Err(AuthFlowError::InvalidInput(_))
    ));
}

#[test]
fn secret_type_keeps_the_value_out_of_debug() {
    let secret = SecretString::new("fic-not-for-logs");
    assert!(!format!("{secret:?}").contains("fic-not-for-logs"));
}
