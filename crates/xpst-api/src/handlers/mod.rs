//! Handlers, one module per area, plus the response shapes they share.
//!
//! A handler receives a [`RequestContext`] and returns an axum `Response`. It
//! never decides whether the request is allowed (that is [`crate::auth`]) and
//! never decides whether the route exists (that is [`crate::registry`]).

pub mod pairing;
pub mod plans;
pub mod preflight;
pub mod reads;
pub mod statics;

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};

use crate::app::RequestContext;
use crate::registry::{Handler, Method, RouteSpec};

/// Dispatch one implemented route.
pub async fn handle(handler: Handler, context: RequestContext<'_>) -> Response {
    match handler {
        Handler::Health => reads::health(&context),
        Handler::Providers => reads::providers(&context),
        Handler::Capabilities => reads::capabilities(&context),
        Handler::Settings => reads::settings(&context),
        Handler::HealthStatus => reads::health_status(&context),
        Handler::Schedules => reads::schedules(&context),
        Handler::ScheduleCancel => reads::schedule_cancel(&context),
        Handler::RateLimits => reads::rate_limits(&context),
        Handler::Preflight => preflight::preflight(&context),
        Handler::PlanCreate => plans::create(&context),
        Handler::PlanGet => plans::get(&context),
        Handler::PlanConfirm => plans::confirm(&context),
        Handler::PlanPublish => plans::publish(&context).await,
        Handler::PlanReadback => plans::readback(&context),
        Handler::Pair => pairing::pair(&context),
        Handler::Devices => pairing::devices(&context),
        Handler::DeviceRevoke => pairing::revoke(&context),
        Handler::UiShell => statics::ui_shell(&context),
        Handler::StaticAsset => statics::asset(&context),
    }
}

/// A JSON response with an explicit status.
pub fn json_response(status: StatusCode, body: Value) -> Response {
    (status, axum::Json(body)).into_response()
}

/// A `200` JSON response.
pub fn ok(body: Value) -> Response {
    json_response(StatusCode::OK, body)
}

/// FastAPI's error shape: `{"detail": …}`. Used for 404/405/413/401, which are
/// framework-level answers in Python too.
pub fn http_error(status: StatusCode, detail: impl Into<String>) -> Response {
    json_response(status, json!({ "detail": detail.into() }))
}

/// The product error envelope: `{ok: false, code, error: {code, message}, message}`.
///
/// This is the shape Python's plan and pairing routes return, so a client that
/// branches on `error.code` works against both surfaces.
pub fn api_error(status: StatusCode, code: &str, message: impl Into<String>) -> Response {
    let message = message.into();
    json_response(
        status,
        json!({
            "ok": false,
            "code": code,
            "error": { "code": code, "message": message },
            "message": message,
        }),
    )
}

/// The same envelope plus Python's `blocked: true` marker, which the plan
/// routes carry.
pub fn blocked_error(status: StatusCode, code: &str, message: impl Into<String>) -> Response {
    let message = message.into();
    json_response(
        status,
        json!({
            "ok": false,
            "blocked": true,
            "code": code,
            "error": { "code": code, "message": message },
            "message": message,
        }),
    )
}

/// A typed `501`: the route exists, and the engine cannot answer it yet.
///
/// The body names the route and says what is missing. It is never a `200`.
pub fn not_implemented(spec: &'static RouteSpec, reason: &'static str) -> Response {
    json_response(
        StatusCode::NOT_IMPLEMENTED,
        json!({
            "ok": false,
            "code": "NOT_IMPLEMENTED",
            "route": spec.id,
            "method": spec.method.as_str(),
            "path": spec.path,
            "reason": reason,
            "error": { "code": "NOT_IMPLEMENTED", "message": reason },
        }),
    )
}

/// `405`, with the methods that would have worked.
pub fn method_not_allowed(allowed: &[Method]) -> Response {
    let mut response = http_error(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    let allow = allowed
        .iter()
        .map(|method| method.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    if let Ok(value) = axum::http::HeaderValue::from_str(&allow) {
        response.headers_mut().insert("allow", value);
    }
    response
}

/// Read `<config_dir>/state.json` as JSON, or an empty object.
///
/// No error is invented for a missing file: a paused install has no state yet,
/// and the routes that use it report zeroes rather than a failure.
pub fn state_document(config_dir: &std::path::Path) -> Value {
    let path = config_dir.join("state.json");
    match std::fs::read_to_string(path) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_else(|_| json!({})),
        Err(_) => json!({}),
    }
}

/// Read `<config_dir>/schedule.json` as a list of entries, verbatim.
pub fn schedule_entries(config_dir: &std::path::Path) -> Vec<Value> {
    let path = config_dir.join("schedule.json");
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Array(entries)) => entries,
        _ => Vec::new(),
    }
}

/// The raw configuration document, or `None` when there is none.
pub fn config_document(config_dir: &std::path::Path) -> Option<xpst_config::ConfigDocument> {
    xpst_config::ConfigDocument::read(&config_dir.join("config.yaml")).ok()
}

/// The masked configuration as JSON, or `None` when there is no config file.
pub fn masked_config(config_dir: &std::path::Path) -> Option<Value> {
    let document = config_document(config_dir)?;
    serde_json::to_value(document.masked()).ok()
}

/// The unmasked configuration as JSON (never returned to a caller).
pub fn raw_config(config_dir: &std::path::Path) -> Option<Value> {
    let document = config_document(config_dir)?;
    serde_json::to_value(document.raw()).ok()
}

/// Whether a platform's account is enabled.
///
/// A missing config file is answered from the engine's documented defaults
/// (the same ones Python's `XPSTConfig()` starts from), never from "enabled".
pub fn platform_enabled(config: Option<&Value>, platform: &str) -> bool {
    if platform == "local" {
        return true;
    }
    if let Some(section) = config
        .and_then(|document| document.get("accounts"))
        .and_then(|accounts| accounts.get(platform))
        .or_else(|| config.and_then(|document| document.get(platform)))
    {
        if let Some(enabled) = section.get("enabled").and_then(Value::as_bool) {
            return enabled;
        }
    }
    false
}
