//! Parity against the Python dashboard surface.
//!
//! Three separate claims, because they fail for three different reasons:
//!
//! 1. **Route set.** Every `(method, path)` the Python app answers is a row in
//!    this crate's registry, classified implemented or not; no row claims a
//!    Python path that Python does not serve; nothing is silently missing.
//! 2. **Payload shape.** For every route this surface claims, the response has
//!    the same key paths as Python's — deeply where the field set is Rust's
//!    own, at the top level where the nested objects come from a different
//!    engine source (each such case is named in `parity::SHAPE_LEVELS`).
//! 3. **Refusals and catalog metadata.** Pairing refusal codes/messages and the
//!    provider catalog are compared to captures, not to a transcription.

mod common;

use std::collections::BTreeSet;
use std::sync::Arc;

use axum::body::Body;
use axum::http::Request;
use common::{sandbox, send, state};
use serde_json::{json, Value};
use xpst_api::parity::{self, Shape};
use xpst_api::{AppState, RouteStatus, ROUTES};

fn is_extension(id: &str) -> bool {
    id.starts_with("api.") || id == "ui_static"
}

#[test]
fn every_python_route_is_a_row_in_the_registry() {
    let mut missing = Vec::new();
    for route in parity::python_routes() {
        let found = ROUTES
            .iter()
            .any(|spec| spec.path == route.path && spec.method.as_str() == route.method);
        if !found {
            missing.push(format!(
                "{} {} ({}, {})",
                route.method, route.path, route.name, route.kind
            ));
        }
    }
    assert!(
        missing.is_empty(),
        "routes the Python app answers but this registry does not: {missing:#?}"
    );
}

#[test]
fn no_row_claims_a_python_path_that_python_does_not_serve() {
    let python: BTreeSet<(String, String)> = parity::python_routes()
        .into_iter()
        .map(|route| (route.method, route.path))
        .collect();
    let mut extra = Vec::new();
    for spec in ROUTES {
        if is_extension(spec.id) {
            continue;
        }
        let key = (spec.method.as_str().to_string(), spec.path.to_string());
        if !python.contains(&key) {
            extra.push(format!(
                "{} {} ({})",
                spec.method.as_str(),
                spec.path,
                spec.id
            ));
        }
    }
    assert!(
        extra.is_empty(),
        "rows this registry claims that the Python app does not serve: {extra:#?}"
    );
}

#[test]
fn every_python_route_is_classified_and_the_counts_add_up() {
    let python = parity::python_routes();
    let classified = python
        .iter()
        .filter(|route| {
            ROUTES
                .iter()
                .any(|spec| spec.path == route.path && spec.method.as_str() == route.method)
        })
        .count();
    assert_eq!(
        classified,
        python.len(),
        "every Python route must be implemented or explicitly unimplemented"
    );
    assert_eq!(python.len(), 57);
    assert_eq!(
        python.len() + xpst_api::registry::DEV_DOC_ROUTES.len(),
        61,
        "the app's whole surface is 57 product routes plus 4 framework dev-doc routes"
    );
}

#[test]
fn the_implemented_set_is_the_documented_one() {
    let mut implemented: Vec<&str> = ROUTES
        .iter()
        .filter(|spec| spec.status.is_implemented())
        .map(|spec| spec.id)
        .collect();
    implemented.sort_unstable();
    let expected = [
        "api.rate_limits",
        "api_capabilities",
        "api_device_revoke",
        "api_devices",
        "api_health_status",
        "api_pair",
        "api_plan_confirm",
        "api_plan_create",
        "api_plan_get",
        "api_plan_publish",
        "api_plan_readback",
        "api_preflight",
        "api_providers",
        "api_schedule_cancel",
        "api_schedules",
        "api_settings",
        "dashboard_index",
        "health_check",
        "ui_static",
    ];
    assert_eq!(
        implemented, expected,
        "the implemented route set changed; update the documented list deliberately"
    );
}

#[test]
fn every_unimplemented_route_carries_a_reason_and_no_handler() {
    for spec in ROUTES {
        if let RouteStatus::NotImplemented(reason) = spec.status {
            assert!(reason.len() > 20, "{}: reason is not usable", spec.id);
        }
    }
}

/// Requests for the captured samples, so the shape comparison asks the same
/// question of both surfaces.
fn sample_request(sample: &str) -> Option<(String, String, Option<Value>)> {
    Some(match sample {
        "health" => ("GET".into(), "/health".into(), None),
        "api.providers" => ("GET".into(), "/api/providers".into(), None),
        "api.capabilities" => ("GET".into(), "/api/capabilities".into(), None),
        "api.settings" => ("GET".into(), "/api/settings".into(), None),
        "api.health_status" => ("GET".into(), "/api/health-status".into(), None),
        "api.schedules" => ("GET".into(), "/api/schedules".into(), None),
        "api.devices" => ("GET".into(), "/api/devices".into(), None),
        "api.pair.missing_code" => ("POST".into(), "/api/pair".into(), Some(json!({"code": ""}))),
        "api.preflight" => (
            "POST".into(),
            "/api/preflight".into(),
            Some(json!({"media_paths": [], "caption": "hi", "platforms": ["youtube"]})),
        ),
        "api.plans.create_error" => (
            "POST".into(),
            "/api/plans".into(),
            Some(json!({"caption": "hi"})),
        ),
        "api.plans.get_error" => ("GET".into(), "/api/plans/plan_missing".into(), None),
        _ => return None,
    })
}

async fn call_sample(state: Arc<AppState>, sample: &str) -> Value {
    let (method, path, body) = sample_request(sample).expect("sample has a request");
    let builder = Request::builder()
        .method(method.as_str())
        .uri(path)
        .header("authorization", "Bearer test-token")
        .header("content-type", "application/json");
    let request = builder
        .body(body.map_or_else(Body::empty, |value| Body::from(value.to_string())))
        .expect("request builds");
    let (_, bytes) = send(state, request).await;
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

#[tokio::test]
async fn every_claimed_route_has_the_python_payload_shape() {
    let samples = parity::payload_samples();
    let dir = sandbox("shape");
    let state = state(&dir);
    let mut checked = 0;
    for (sample, captured) in &samples {
        let Some(expected) = captured.get("body") else {
            continue;
        };
        if sample_request(sample).is_none() {
            continue;
        }
        let actual = call_sample(state.clone(), sample).await;
        assert!(!actual.is_null(), "{sample} returned no JSON");
        let difference = match parity::shape_level(sample) {
            Shape::Deep => {
                parity::shape_difference(&parity::key_paths(expected), &parity::key_paths(&actual))
            }
            Shape::TopLevel(_) => parity::shape_difference(
                &parity::top_level_keys(expected),
                &parity::top_level_keys(&actual),
            ),
        };
        assert!(difference.is_none(), "{sample}: {difference:?}");
        checked += 1;
    }
    assert!(
        checked >= 11,
        "expected the captured samples, checked {checked}"
    );
}

#[tokio::test]
async fn the_pairing_refusals_are_pythons_codes_and_messages() {
    let expected = parity::pairing_refusals();
    let dir = sandbox("pairing-codes");

    let unknown =
        xpst_api::devices::redeem_pairing_code(&dir, "ZZZZZZZZ", "", "", None).unwrap_err();
    assert_eq!(unknown.code, expected["unknown"]["code"].as_str().unwrap());
    assert_eq!(
        unknown.message,
        expected["unknown"]["message"].as_str().unwrap()
    );
    assert_eq!(
        unknown.status_code,
        expected["unknown"]["status_code"].as_u64().unwrap() as u16
    );

    let short =
        xpst_api::devices::create_pairing_code(&dir, "", Some(600), Some("ABC"), None).unwrap_err();
    assert_eq!(short.code, expected["shape"]["code"].as_str().unwrap());
    assert_eq!(
        short.message,
        expected["shape"]["message"].as_str().unwrap()
    );

    // Expired, spent and budget-exhausted, each against the capture.
    let spend_dir = sandbox("pairing-spent");
    xpst_api::devices::create_pairing_code(
        &spend_dir,
        "",
        Some(600),
        Some("EXPIRE22"),
        Some(1_000.0),
    )
    .unwrap();
    let expired =
        xpst_api::devices::redeem_pairing_code(&spend_dir, "EXPIRE22", "", "", Some(2_000.0))
            .unwrap_err();
    assert_eq!(expired.code, expected["expired"]["code"].as_str().unwrap());
    assert_eq!(
        expired.message,
        expected["expired"]["message"].as_str().unwrap()
    );

    let spent_dir = sandbox("pairing-spent2");
    xpst_api::devices::create_pairing_code(
        &spent_dir,
        "",
        Some(600),
        Some("SPENT222"),
        Some(1_000.0),
    )
    .unwrap();
    let _ = xpst_api::devices::redeem_pairing_code(&spent_dir, "SPENT222", "", "", Some(1_001.0));
    let spent =
        xpst_api::devices::redeem_pairing_code(&spent_dir, "SPENT222", "", "", Some(1_002.0))
            .unwrap_err();
    assert_eq!(spent.code, expected["spent"]["code"].as_str().unwrap());
    assert_eq!(
        spent.message,
        expected["spent"]["message"].as_str().unwrap()
    );

    let budget_dir = sandbox("pairing-budget");
    xpst_api::devices::create_pairing_code(
        &budget_dir,
        "",
        Some(600),
        Some("BUDGET22"),
        Some(1_000.0),
    )
    .unwrap();
    for index in 0..5 {
        let _ = xpst_api::devices::redeem_pairing_code(
            &budget_dir,
            &format!("WRONG{index:03}"),
            "",
            "",
            Some(1_001.0),
        );
    }
    let blocked =
        xpst_api::devices::redeem_pairing_code(&budget_dir, "BUDGET22", "", "", Some(1_002.0))
            .unwrap_err();
    assert_eq!(
        blocked.code,
        expected["too_many_attempts"]["code"].as_str().unwrap()
    );
    assert_eq!(
        blocked.message,
        expected["too_many_attempts"]["message"].as_str().unwrap()
    );
    assert_eq!(
        blocked.status_code,
        expected["too_many_attempts"]["status_code"]
            .as_u64()
            .unwrap() as u16
    );
}

#[tokio::test]
async fn the_provider_catalog_matches_the_captured_one() {
    let samples = parity::payload_samples();
    let captured = samples["api.providers"]["body"]["by_name"]
        .as_object()
        .expect("captured catalog");
    let runtime = xpst_api::handlers::reads::provider_table();
    assert_eq!(runtime.len(), captured.len(), "provider count changed");
    for row in runtime {
        let name = row["name"].as_str().unwrap();
        let python = captured
            .get(name)
            .unwrap_or_else(|| panic!("{name} is not in Python's catalog"));
        for field in [
            "display_name",
            "roles",
            "capabilities",
            "is_official_api",
            "docs_url",
        ] {
            assert_eq!(
                row[field], python[field],
                "{name}.{field} differs from the captured Python catalog"
            );
        }
    }
}

#[test]
fn the_divergences_are_declared() {
    assert!(
        !parity::DIVERGENCES.is_empty(),
        "a port that deviates must say where"
    );
    for (surface, _, _) in parity::DIVERGENCES {
        assert!(!surface.is_empty());
    }
}
