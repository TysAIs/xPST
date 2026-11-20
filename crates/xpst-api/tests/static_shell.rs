//! The static shell: `ui/dist` when it exists, the string index when it does
//! not, and no traversal either way.

mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use common::{sandbox, state};
use http_body_util::BodyExt;
use std::sync::Arc;
use xpst_api::{AppState, AuthConfig};

async fn body_of(state: Arc<AppState>, path: &str) -> (StatusCode, String) {
    let request = Request::builder()
        .uri(path)
        .body(Body::empty())
        .expect("request builds");
    let response = tower::ServiceExt::oneshot(xpst_api::build_router(state), request)
        .await
        .expect("router answers");
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).to_string())
}

#[tokio::test]
async fn without_a_ui_build_the_string_index_is_served() {
    let dir = sandbox("shell-fallback");
    let state = state(&dir);
    let (status, body) = body_of(state.clone(), "/").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("xPST"), "the fallback page names the product");
    assert!(body.contains("<!doctype html>"));
    assert!(body.contains("web UI build is not installed"));

    // An asset request with no build is a 404, never an empty 200.
    let (status, _) = body_of(state, "/assets/app.js").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn with_a_ui_build_the_index_and_assets_are_served() {
    let dir = sandbox("shell-build");
    let dist = dir.join("ui-dist");
    std::fs::create_dir_all(dist.join("assets")).unwrap();
    std::fs::write(
        dist.join("index.html"),
        "<!doctype html><title>built</title>",
    )
    .unwrap();
    std::fs::write(dist.join("assets/app.js"), "console.log('built');").unwrap();

    let state = Arc::new(
        AppState::new(&dir)
            .with_auth(
                AuthConfig::new()
                    .with_token("test-token")
                    .with_config_dir(&dir),
            )
            .with_ui_dist(&dist),
    );

    let (status, body) = body_of(state.clone(), "/").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("built"));

    let request = Request::builder()
        .uri("/assets/app.js")
        .body(Body::empty())
        .expect("request builds");
    let response = tower::ServiceExt::oneshot(xpst_api::build_router(state.clone()), request)
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response
            .headers()
            .get("content-type")
            .and_then(|value| value.to_str().ok()),
        Some("text/javascript; charset=utf-8")
    );

    let (status, _) = body_of(state, "/assets/missing.js").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn a_traversal_attempt_is_refused() {
    let dir = sandbox("shell-traversal");
    let dist = dir.join("ui-dist");
    std::fs::create_dir_all(&dist).unwrap();
    std::fs::write(dist.join("index.html"), "<html></html>").unwrap();
    std::fs::write(dir.join("secret.txt"), "do not serve me").unwrap();

    let state = Arc::new(AppState::new(&dir).with_ui_dist(&dist));
    for path in [
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/assets/../../secret.txt",
    ] {
        let (status, body) = body_of(state.clone(), path).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{path} leaked: {body}");
        assert!(!body.contains("do not serve me"));
    }
}

#[tokio::test]
async fn the_shell_stays_public_off_loopback_while_the_api_does_not() {
    let dir = sandbox("shell-public");
    let dist = dir.join("ui-dist");
    std::fs::create_dir_all(&dist).unwrap();
    std::fs::write(dist.join("index.html"), "<html>pairing screen</html>").unwrap();
    let state = Arc::new(
        AppState::new(&dir)
            .with_auth(
                AuthConfig::new()
                    .with_token("test-token")
                    .with_config_dir(&dir)
                    .with_read_auth(true),
            )
            .with_ui_dist(&dist),
    );

    let (status, body) = body_of(state.clone(), "/").await;
    assert_eq!(
        status,
        StatusCode::OK,
        "an unpaired phone must load the shell"
    );
    assert!(body.contains("pairing screen"));

    let (status, _) = body_of(state, "/api/providers").await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn the_fallback_page_is_served_with_the_security_headers() {
    let dir = sandbox("shell-headers");
    let state = state(&dir);
    let request = Request::builder()
        .uri("/")
        .body(Body::empty())
        .expect("request builds");
    let response = tower::ServiceExt::oneshot(xpst_api::build_router(state), request)
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let headers = response.headers();
    assert!(headers
        .get("content-security-policy")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.contains("script-src 'none'")));
    assert_eq!(
        headers
            .get("x-content-type-options")
            .and_then(|value| value.to_str().ok()),
        Some("nosniff")
    );
}
