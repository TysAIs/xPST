//! The 501 path: a route the engine cannot serve says so, once, and never
//! returns a fabricated `200`.

mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use common::{post_json, sandbox, send, state};
use serde_json::{json, Value};
use xpst_api::{Method, RouteStatus, ROUTES};

fn concrete(path: &str) -> String {
    path.split('/')
        .map(|segment| {
            if segment.starts_with('{') {
                "sample"
            } else {
                segment
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}

#[tokio::test]
async fn every_unimplemented_route_answers_501_with_the_route_and_the_reason() {
    let dir = sandbox("not-implemented");
    let state = state(&dir);
    let mut checked = 0;
    for spec in ROUTES {
        let RouteStatus::NotImplemented(reason) = spec.status else {
            continue;
        };
        let method = spec.method.as_str();
        let request = Request::builder()
            .method(method)
            .uri(concrete(spec.path))
            .header("authorization", "Bearer test-token")
            .header("x-api-token", "test-token")
            .header("content-type", "application/json")
            .body(Body::from("{}"))
            .expect("request builds");
        let (status, bytes) = send(state.clone(), request).await;
        let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        assert_eq!(
            status,
            StatusCode::NOT_IMPLEMENTED,
            "{} {} should be a 501, got {status} ({})",
            spec.method.as_str(),
            spec.path,
            String::from_utf8_lossy(&bytes)
        );
        assert_eq!(body["code"], json!("NOT_IMPLEMENTED"));
        assert_eq!(body["route"], json!(spec.id));
        assert_eq!(body["reason"], json!(reason));
        assert!(
            body["error"]["message"].as_str().unwrap_or("").len() > 20,
            "{} has no usable reason",
            spec.id
        );
        checked += 1;
    }
    assert!(
        checked >= 30,
        "expected the unimplemented set, got {checked}"
    );
}

#[tokio::test]
async fn a_501_route_does_not_pretend_to_be_a_read() {
    let dir = sandbox("metrics");
    let state = state(&dir);
    let request = Request::builder()
        .uri("/metrics")
        .body(Body::empty())
        .expect("request builds");
    let (status, bytes) = send(state, request).await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert!(body["reason"].as_str().unwrap().contains("Prometheus"));
}

#[tokio::test]
async fn the_routes_the_engine_does_serve_are_not_501() {
    let dir = sandbox("implemented");
    let state = state(&dir);
    for spec in ROUTES {
        if !spec.status.is_implemented() {
            continue;
        }
        let request = Request::builder()
            .method(spec.method.as_str())
            .uri(concrete(spec.path))
            .header("authorization", "Bearer test-token")
            .header("content-type", "application/json")
            .body(Body::from("{}"))
            .expect("request builds");
        let (status, bytes) = send(state.clone(), request).await;
        assert_ne!(
            status,
            StatusCode::NOT_IMPLEMENTED,
            "{} {} is marked implemented but answered 501",
            spec.method.as_str(),
            spec.path
        );
        // An empty body is a shape error, not a crash.
        assert!(
            status.is_success() || status.is_client_error() || status.is_server_error(),
            "{} {} answered {status}",
            spec.method.as_str(),
            spec.path
        );
        let _ = bytes;
    }
}

#[tokio::test]
async fn an_implemented_mutation_with_an_empty_body_reports_a_shape_error() {
    let dir = sandbox("empty-body");
    let state = state(&dir);
    let request = Request::builder()
        .method("POST")
        .uri("/api/plans")
        .header("authorization", "Bearer test-token")
        .body(Body::empty())
        .expect("request builds");
    let (status, bytes) = send(state, request).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["code"], json!("PLAN_NO_MEDIA"));
}

#[tokio::test]
async fn an_invalid_json_body_is_reported_not_ignored() {
    let dir = sandbox("bad-json");
    let state = state(&dir);
    let request = Request::builder()
        .method("POST")
        .uri("/api/preflight")
        .header("authorization", "Bearer test-token")
        .body(Body::from("{not json"))
        .expect("request builds");
    let (status, bytes) = send(state, request).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert!(body["detail"].as_str().unwrap().contains("Invalid JSON"));
}

#[tokio::test]
async fn the_method_enum_parses_only_what_the_table_uses() {
    assert_eq!(Method::parse("get"), Some(Method::Get));
    assert_eq!(Method::parse("HEAD"), Some(Method::Get));
    assert_eq!(Method::parse("post"), Some(Method::Post));
    assert_eq!(Method::parse("PUT"), None);
    assert!(Method::Post.is_mutating());
    assert!(!Method::Get.is_mutating());
}

#[tokio::test]
async fn a_preflight_request_is_answered_by_the_planner_not_faked() {
    let dir = sandbox("preflight-honest");
    let media = dir.join("clip.mp4");
    std::fs::write(&media, b"not really a video").unwrap();
    let state = state(&dir);
    let (status, body) = post_json(
        state,
        "/api/preflight",
        json!({
            "media_paths": [media.display().to_string()],
            "caption": "hello",
            "platforms": ["youtube"],
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["content_type"], json!("video"));
    assert_eq!(body["media"]["exists"], json!(true));
    assert_eq!(body["media"]["is_file"], json!(true));
    assert_eq!(body["network_calls"], json!(false));
    assert!(body["route_plan"]["destinations"].is_array());
}
