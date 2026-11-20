//! The auth matrix: what answers anonymously, what needs a credential, and the
//! enumeration that decides it.
//!
//! Each row is one of the four cases the ports must agree on:
//!
//! | request | loopback, no login | non-loopback |
//! |---|---|---|
//! | public-by-design read (`/health`) | 200 | 200 |
//! | `/api` read without a credential | 200 | 401 |
//! | mutation without a credential | 401 | 401 |
//! | mutation with a token / login / device token | 200 / 501 | 200 / 501 |

mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use common::{get, post_json, sandbox, send, state};
use serde_json::json;
use std::sync::Arc;
use xpst_api::{AppState, AuthConfig};

fn exposed(dir: &std::path::Path) -> Arc<AppState> {
    Arc::new(
        AppState::new(dir).with_auth(
            AuthConfig::new()
                .with_token("test-token")
                .with_config_dir(dir)
                .with_read_auth(true),
        ),
    )
}

#[tokio::test]
async fn a_loopback_read_is_open_and_a_loopback_mutation_is_not() {
    let dir = sandbox("loopback");
    let state = state(&dir);

    let (status, _) = get(state.clone(), "/api/providers").await;
    assert_eq!(
        status,
        StatusCode::OK,
        "a loopback read needs no credential"
    );

    let request = Request::builder()
        .method("POST")
        .uri("/api/preflight")
        .header("content-type", "application/json")
        .body(Body::from(json!({"platforms": ["youtube"]}).to_string()))
        .expect("request builds");
    let (status, body) = send(state, request).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert!(body_string(&body).contains("detail"));
}

#[tokio::test]
async fn api_reads_need_a_credential_off_loopback_but_the_shell_does_not() {
    let dir = sandbox("exposed");
    let state = exposed(&dir);

    let (status, _) = get(state.clone(), "/api/providers").await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    let (status, _) = get(state.clone(), "/health").await;
    assert_eq!(status, StatusCode::OK, "/health is a probe");

    let (status, _) = get(state.clone(), "/api/capabilities").await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    // The shell must stay public or an unpaired phone cannot pair.
    let request = Request::builder()
        .uri("/")
        .body(Body::empty())
        .expect("request builds");
    let (status, _) = send(state, request).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn an_api_read_succeeds_with_the_token_off_loopback() {
    let dir = sandbox("exposed-token");
    let state = exposed(&dir);
    let request = Request::builder()
        .uri("/api/providers")
        .header("x-api-token", "test-token")
        .body(Body::empty())
        .expect("request builds");
    let (status, _) = send(state, request).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn a_mutation_with_the_token_is_served() {
    let dir = sandbox("mutation-token");
    let state = state(&dir);
    let (status, body) = post_json(
        state,
        "/api/preflight",
        json!({"platforms": ["youtube"], "caption": "hello"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.get("ok").is_some());
}

#[tokio::test]
async fn basic_auth_is_required_for_reads_once_it_is_configured() {
    let dir = sandbox("basic");
    let hash = xpst_api::auth::legacy_hash("hunter2");
    let state = Arc::new(
        AppState::new(&dir).with_auth(
            AuthConfig::new()
                .with_basic("operator", hash)
                .with_config_dir(&dir),
        ),
    );

    let (status, _) = get(state.clone(), "/api/providers").await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    let header = format!(
        "Basic {}",
        xpst_api::auth::base64_encode(b"operator:hunter2")
    );
    let request = Request::builder()
        .uri("/api/providers")
        .header("authorization", &header)
        .body(Body::empty())
        .expect("request builds");
    let (status, _) = send(state.clone(), request).await;
    assert_eq!(status, StatusCode::OK);

    let request = Request::builder()
        .uri("/api/providers")
        .header(
            "authorization",
            format!("Basic {}", xpst_api::auth::base64_encode(b"operator:wrong")),
        )
        .body(Body::empty())
        .expect("request builds");
    let (status, _) = send(state, request).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn a_paired_device_token_authenticates_a_read_off_loopback() {
    let dir = sandbox("device");
    xpst_api::devices::create_pairing_code(&dir, "phone", Some(600), Some("PAIR2345"), None)
        .expect("code issued");
    let grant = xpst_api::devices::redeem_pairing_code(&dir, "PAIR-2345", "", "test", None)
        .expect("paired");

    let state = exposed(&dir);
    let request = Request::builder()
        .uri("/api/providers")
        .header("authorization", format!("Bearer {}", grant.token))
        .body(Body::empty())
        .expect("request builds");
    let (status, _) = send(state, request).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "a paired phone must be able to read"
    );
}

#[tokio::test]
async fn the_pairing_exchange_is_reachable_anonymously_and_everything_else_mutating_is_not() {
    let dir = sandbox("pair-public");
    let state = exposed(&dir);

    // Public by design: the phone has no credential yet.
    let request = Request::builder()
        .method("POST")
        .uri("/api/pair")
        .header("content-type", "application/json")
        .body(Body::from(json!({"code": ""}).to_string()))
        .expect("request builds");
    let (status, _) = send(state.clone(), request).await;
    assert_eq!(
        status,
        StatusCode::BAD_REQUEST,
        "the route answers, anonymously"
    );

    for path in [
        "/api/post",
        "/api/schedules/x/cancel",
        "/api/plans/p/confirm",
    ] {
        let request = Request::builder()
            .method("POST")
            .uri(path)
            .header("content-type", "application/json")
            .body(Body::from("{}"))
            .expect("request builds");
        let (status, _) = send(state.clone(), request).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{path} must be gated");
    }

    let request = Request::builder()
        .method("DELETE")
        .uri("/api/devices/dev_x")
        .body(Body::empty())
        .expect("request builds");
    let (status, _) = send(state, request).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "a DELETE must be gated");
}

#[tokio::test]
async fn every_documented_public_path_answers_anonymously_off_loopback() {
    let dir = sandbox("public-set");
    let state = exposed(&dir);
    // The registry's public rows are asserted equal to the documented list in
    // the crate's own unit test; here the *enforcement* is what is checked, per
    // the method the row actually answers.
    for spec in xpst_api::ROUTES {
        if !spec.access.is_public() {
            continue;
        }
        if spec.path.starts_with("/{") {
            continue;
        }
        let request = Request::builder()
            .method(spec.method.as_str())
            .uri(spec.path)
            .header("content-type", "application/json")
            .body(Body::from("{}"))
            .expect("request builds");
        let (status, _) = send(state.clone(), request).await;
        assert_ne!(
            status,
            StatusCode::UNAUTHORIZED,
            "{} {} is documented public but refused anonymously",
            spec.method.as_str(),
            spec.path
        );
    }
}

#[tokio::test]
async fn an_unknown_api_path_is_gated_before_it_is_404() {
    let dir = sandbox("unknown-gated");
    let state = exposed(&dir);
    let (status, _) = get(state, "/api/does-not-exist").await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "the surface must not be discoverable by probing"
    );
}

#[tokio::test]
async fn a_wrong_method_is_405_with_an_allow_header() {
    let dir = sandbox("method");
    let state = state(&dir);
    let request = Request::builder()
        .method("DELETE")
        .uri("/api/providers")
        .header("authorization", "Bearer test-token")
        .body(Body::empty())
        .expect("request builds");
    let response = tower::ServiceExt::oneshot(xpst_api::build_router(state), request)
        .await
        .expect("router answers");
    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(
        response
            .headers()
            .get("allow")
            .and_then(|value| value.to_str().ok()),
        Some("GET")
    );
}

fn body_string(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).to_string()
}
