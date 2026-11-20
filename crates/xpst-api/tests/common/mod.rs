//! Shared harness: a sandbox config dir, a router, and one request helper.
//!
//! Each test binary uses a subset of these helpers, so dead-code warnings here
//! would be noise rather than a finding.
#![allow(dead_code)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::Value;
use xpst_api::{build_router, AppState};

static COUNTER: AtomicU32 = AtomicU32::new(0);

/// A fresh, empty config directory for one test.
pub fn sandbox(name: &str) -> PathBuf {
    let sequence = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!(
        "xpst-api-test-{name}-{}-{sequence}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("sandbox config dir");
    dir
}

/// The standard test state: no Basic login, one API token, loopback rules.
pub fn state(dir: &std::path::Path) -> Arc<AppState> {
    Arc::new(
        AppState::new(dir).with_auth(
            xpst_api::AuthConfig::new()
                .with_token("test-token")
                .with_config_dir(dir),
        ),
    )
}

/// Send one request through the real router.
pub async fn send(state: Arc<AppState>, request: Request<Body>) -> (StatusCode, Vec<u8>) {
    let response = tower::ServiceExt::oneshot(build_router(state), request)
        .await
        .expect("router answers");
    let status = response.status();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body collects")
        .to_bytes()
        .to_vec();
    (status, bytes)
}

/// GET with optional headers.
pub async fn get(state: Arc<AppState>, path: &str) -> (StatusCode, Value) {
    let request = Request::builder()
        .method("GET")
        .uri(path)
        .body(Body::empty())
        .expect("request builds");
    let (status, bytes) = send(state, request).await;
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// A mutation with the test token.
pub async fn post_json(state: Arc<AppState>, path: &str, body: Value) -> (StatusCode, Value) {
    let request = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .header("authorization", "Bearer test-token")
        .body(Body::from(body.to_string()))
        .expect("request builds");
    let (status, bytes) = send(state, request).await;
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
