//! Device pairing and the paired-device list.
//!
//! `/api/pair` is public by design: a brand-new phone has no credential, and the
//! single-use, expiring code in the body *is* the credential for that one call.
//! Everything it and the device routes return is a port of
//! `src/xpst/dashboard/devices.py`, refusal codes included.

use axum::http::StatusCode;
use axum::response::Response;
use serde_json::{json, Value};

use super::{api_error, json_response};
use crate::app::RequestContext;
use crate::devices;

fn config_dir(context: &RequestContext<'_>) -> std::path::PathBuf {
    context.state.config_dir().to_path_buf()
}

/// `POST /api/pair` — exchange a one-time code for a device token.
pub fn pair(context: &RequestContext<'_>) -> Response {
    let body = match context.json_body() {
        Ok(body) => body,
        Err(response) => return response,
    };
    let code = body
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if code.is_empty() {
        let message = "Send the pairing code: {\"code\": \"ABCD-2345\"}.";
        return json_response(
            StatusCode::BAD_REQUEST,
            json!({
                "ok": false,
                "code": "PAIR_CODE_MISSING",
                "error": { "code": "PAIR_CODE_MISSING", "message": message },
                "message": message,
            }),
        );
    }
    let label = body
        .get("label")
        .or_else(|| body.get("device_label"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let client = body.get("client").and_then(Value::as_str).unwrap_or("");

    match devices::redeem_pairing_code(&config_dir(context), &code, label, client, None) {
        Ok(grant) => json_response(
            StatusCode::OK,
            json!({
                "ok": true,
                "device": grant.device,
                "token": grant.token,
                "message": format!(
                    "Paired '{}'. This token is shown once; store it on the device.",
                    grant
                        .device
                        .get("label")
                        .and_then(Value::as_str)
                        .unwrap_or("phone")
                ),
            }),
        ),
        Err(error) => {
            let status = StatusCode::from_u16(error.status_code).unwrap_or(StatusCode::BAD_REQUEST);
            json_response(status, error.to_payload())
        }
    }
}

/// `GET /api/devices` — labels and last-seen only.
pub fn devices(context: &RequestContext<'_>) -> Response {
    json_response(
        StatusCode::OK,
        json!({
            "ok": true,
            "devices": devices::list_devices(&config_dir(context)),
        }),
    )
}

/// `DELETE /api/devices/{device_id}` — revoke one device.
pub fn revoke(context: &RequestContext<'_>) -> Response {
    let device_id = context.param("device_id").unwrap_or_default().to_string();
    match devices::revoke_device(&config_dir(context), &device_id, None) {
        Ok(true) => json_response(
            StatusCode::OK,
            json!({ "ok": true, "revoked": true, "device_id": device_id }),
        ),
        Ok(false) => api_error(
            StatusCode::NOT_FOUND,
            "DEVICE_UNKNOWN",
            format!("No live device with id {device_id}."),
        ),
        Err(error) => {
            let status = StatusCode::from_u16(error.status_code).unwrap_or(StatusCode::BAD_REQUEST);
            json_response(status, error.to_payload())
        }
    }
}
