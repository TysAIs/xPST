//! `POST /api/preflight` — the side-effect-free publish decision.
//!
//! The decision itself is `xpst-core`'s route planner, the same code the
//! pipeline calls before it attempts anything, so this route cannot disagree
//! with a publish about whether a request would be accepted.
//!
//! What this route does **not** do is measure media. `ffprobe` is not run on a
//! request path, so duration, dimensions and codec travel as "not measured"
//! and no verdict depends on them. That is honest: an unmeasured value never
//! causes a refusal, and the response says which fields were not measured.

use std::path::{Path, PathBuf};

use axum::http::StatusCode;
use axum::response::Response;
use serde_json::{json, Map, Value};

use super::{ok, reads};
use crate::app::RequestContext;

/// One requested media path, as much as the filesystem can tell us.
struct LocalMedia {
    path: String,
    size_bytes: Option<u64>,
    container: String,
    exists: bool,
    is_file: bool,
}

fn inspect(path: &str) -> LocalMedia {
    let expanded = xpst_config::expand_home(path);
    let path_buf = PathBuf::from(&expanded);
    let metadata = std::fs::metadata(&path_buf).ok();
    let container = path_buf
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| format!(".{}", extension.to_ascii_lowercase()))
        .unwrap_or_default();
    LocalMedia {
        path: path.to_string(),
        size_bytes: metadata.as_ref().map(std::fs::Metadata::len),
        container,
        exists: metadata.is_some(),
        is_file: metadata.is_some_and(|metadata| metadata.is_file()),
    }
}

fn measurement(media: &LocalMedia) -> xpst_core::plan::MediaMeasurement {
    xpst_core::plan::MediaMeasurement {
        container: media.container.clone(),
        size_bytes: media.size_bytes,
        name: Path::new(&media.path)
            .file_name()
            .and_then(|name| name.to_str())
            .map(str::to_string),
        // Not measured. `None` means "not measured", never zero.
        duration_s: None,
        width: None,
        height: None,
        video_codec: None,
        has_audio: false,
    }
}

/// Per-destination caption overrides: a bare string or `{text: …}`.
fn parse_overrides(value: Option<&Value>) -> Result<Map<String, Value>, String> {
    let mut out = Map::new();
    let Some(Value::Object(entries)) = value else {
        return Ok(out);
    };
    for (platform, raw) in entries {
        let text = match raw {
            Value::String(text) => Some(text.clone()),
            Value::Object(fields) => fields
                .get("text")
                .or_else(|| fields.get("caption"))
                .and_then(Value::as_str)
                .map(str::to_string),
            Value::Null => None,
            other => {
                return Err(format!(
                    "per-destination override must be a string or an object, got {other}"
                ))
            }
        };
        if let Some(text) = text {
            out.insert(platform.trim().to_ascii_lowercase(), Value::String(text));
        }
    }
    Ok(out)
}

fn destinations_for(
    platforms: &[String],
    config: Option<&Value>,
) -> Vec<xpst_core::ingest::DestinationCapability> {
    platforms
        .iter()
        .map(|platform| {
            let enabled = reads::platform_enabled(config, platform);
            // No credential probe on this surface: `authenticated: false` with
            // no detail adds no advisory, which is the truthful state — we did
            // not look.
            xpst_core::ingest::DestinationCapability::built_in(platform, enabled, false)
                .unwrap_or_else(|| {
                    xpst_core::ingest::DestinationCapability::undeclared(platform, enabled, false)
                })
        })
        .collect()
}

fn connected_platforms(config: Option<&Value>) -> Vec<String> {
    xpst_core::content::DESTINATION_CONTENT_PROFILES
        .iter()
        .filter(|profile| profile.publishing && reads::platform_enabled(config, profile.platform))
        .map(|profile| profile.platform.to_string())
        .collect()
}

fn route_row(verdict: &xpst_core::plan::DestinationVerdict, caption: &str) -> Value {
    let blockers: Vec<Value> = verdict
        .refusals
        .iter()
        .map(|reason| json!({ "code": reason.code, "message": reason.message }))
        .collect();
    let warnings: Vec<Value> = verdict
        .advisories
        .iter()
        .map(|reason| json!({ "code": reason.code, "message": reason.message }))
        .collect();
    json!({
        "platform": verdict.platform,
        "state": verdict.state.as_str(),
        "will_post": verdict.accepted,
        "is_failure": verdict.state == xpst_core::plan::DestinationState::Failed,
        "reason": verdict.refusals.first().map(|reason| reason.message.clone()),
        "content_type": verdict.content_type,
        "connected": verdict.accepted,
        "connect_command": if verdict.accepted {
            Value::Null
        } else {
            json!(format!("xpst connect {}", verdict.platform))
        },
        "envelope": Value::Null,
        "caption": caption,
        "caption_length": caption.chars().count(),
        "constraints": [],
        "blockers": blockers,
        "warnings": warnings,
        "transform": Value::Null,
        "transforms": [],
    })
}

/// `POST /api/preflight`.
pub fn preflight(context: &RequestContext<'_>) -> Response {
    let body = match context.json_body() {
        Ok(body) => body,
        Err(response) => return response,
    };
    let media_paths: Vec<String> = match body.get("media_paths") {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(Value::as_str)
            .map(str::trim)
            .filter(|item| !item.is_empty())
            .map(str::to_string)
            .collect(),
        Some(Value::String(single)) if !single.trim().is_empty() => {
            vec![single.trim().to_string()]
        }
        _ => body
            .get("media_path")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|single| !single.is_empty())
            .map(|single| vec![single.to_string()])
            .unwrap_or_default(),
    };
    let caption = body
        .get("text")
        .or_else(|| body.get("caption"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let requested: Vec<String> = body
        .get("platforms")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(|item| item.trim().to_ascii_lowercase())
                .filter(|item| !item.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let format = body
        .get("format")
        .or_else(|| body.get("content_type"))
        .and_then(Value::as_str)
        .unwrap_or("auto")
        .to_string();

    let overrides = match parse_overrides(
        body.get("overrides")
            .or_else(|| body.get("per_destination_overrides")),
    ) {
        Ok(overrides) => overrides,
        Err(message) => {
            return super::http_error(
                StatusCode::BAD_REQUEST,
                format!("Invalid per-destination caption payload: {message}"),
            )
        }
    };

    let raw_document = super::raw_config(context.state.config_dir());
    let raw = raw_document.as_ref();
    let platforms = if requested.is_empty() {
        connected_platforms(raw)
    } else {
        requested.clone()
    };

    let media: Vec<LocalMedia> = media_paths.iter().map(|path| inspect(path)).collect();
    let mut request = xpst_core::plan::RouteRequest::new(
        media.iter().map(measurement).collect(),
        caption.clone(),
    );
    if !format.is_empty() {
        request = request.with_content_type(format.clone());
    }
    // No media probe runs on a request path, so a destination with two video
    // modes cannot be classified from the file. The caller states the mode —
    // otherwise the planner refuses and asks (its message says exactly that).
    if let Some(mode) = body
        .get("mode")
        .or_else(|| body.get("video_mode"))
        .and_then(Value::as_str)
    {
        match xpst_core::envelope::coerce_video_mode(mode) {
            Ok(mode) => request = request.with_mode(mode),
            Err(error) => {
                return super::http_error(
                    StatusCode::BAD_REQUEST,
                    format!("Unknown video mode: {error}"),
                )
            }
        }
    }
    let destinations = destinations_for(&platforms, raw);
    let plan = xpst_core::plan::plan(&request, &destinations);

    let mut blockers: Vec<String> = plan
        .request_blockers
        .iter()
        .map(|reason| reason.message.clone())
        .collect();
    if platforms.is_empty() {
        blockers.push("Choose at least one destination platform.".to_string());
    }
    let mut issues: Vec<Value> = Vec::new();
    for verdict in &plan.destinations {
        for reason in &verdict.refusals {
            blockers.push(reason.message.clone());
            issues.push(json!({
                "code": reason.code,
                "message": reason.message,
                "platform": verdict.platform,
                "severity": "error",
            }));
        }
    }
    let warnings: Vec<String> = plan
        .destinations
        .iter()
        .flat_map(|verdict| {
            verdict
                .advisories
                .iter()
                .map(|reason| reason.message.clone())
        })
        .collect();
    let rows: Vec<Value> = plan
        .destinations
        .iter()
        .map(|verdict| route_row(verdict, &caption))
        .collect();
    let mut per_platform = Map::new();
    for platform in &platforms {
        let text = overrides
            .get(platform)
            .and_then(Value::as_str)
            .unwrap_or(&caption);
        per_platform.insert(platform.clone(), json!(text));
    }
    let first_media = media.first();
    let hard_blockers: Vec<Value> = issues.clone();
    let ready = blockers.is_empty();
    ok(json!({
        "ok": ready,
        "ready": ready,
        "media": {
            "path": first_media.map(|item| item.path.clone()),
            "paths": media_paths,
            "exists": first_media.is_some_and(|item| item.exists),
            "is_file": first_media.is_some_and(|item| item.is_file),
        },
        "caption": {
            "length": caption.chars().count(),
            "per_platform": per_platform,
            "overrides": overrides,
        },
        "platforms": platforms,
        "content_type": plan.content_type.map(|content_type| content_type.as_str()),
        "effective_content_type": plan.content_type.map(|content_type| content_type.as_str()),
        "route": rows,
        "content": {
            "content_type": plan.content_type.map(|content_type| content_type.as_str()),
            "effective_content_type": plan.content_type.map(|content_type| content_type.as_str()),
            "route": [],
            "blockers": blockers,
            "content_issues": issues,
        },
        "blockers": blockers,
        "warnings": warnings,
        "error": plan.request_blockers.first().map(|reason| json!({
            "code": reason.code,
            "message": reason.message,
        })),
        "plan": {
            "ok": ready,
            "hard_blockers": hard_blockers,
            "warnings": warnings.iter().map(|message| json!({ "message": message })).collect::<Vec<_>>(),
            "error": Value::Null,
            "network_calls": false,
        },
        "network_calls": false,
        "route_plan": {
            "content_type": plan.content_type.map(|content_type| content_type.as_str()),
            "source": if requested.is_empty() { "connected" } else { "explicit" },
            "ready": ready,
            "ok": ready,
            "posting_destinations": plan.accepted,
            "skipped_unconnected": plan.destinations.iter()
                .filter(|verdict| verdict.state == xpst_core::plan::DestinationState::SkippedUnconnected)
                .map(|verdict| verdict.platform.clone())
                .collect::<Vec<_>>(),
            "unsupported_format": plan.destinations.iter()
                .filter(|verdict| verdict.state == xpst_core::plan::DestinationState::UnsupportedFormat)
                .map(|verdict| verdict.platform.clone())
                .collect::<Vec<_>>(),
            "failed": plan.destinations.iter()
                .filter(|verdict| verdict.state == xpst_core::plan::DestinationState::Failed)
                .map(|verdict| verdict.platform.clone())
                .collect::<Vec<_>>(),
            "destinations": rows,
            "errors": [],
            "request_blockers": plan.request_blockers.iter().map(|reason| json!({
                "code": reason.code,
                "message": reason.message,
            })).collect::<Vec<_>>(),
            "error": Value::Null,
            "network_calls": false,
        },
    }))
}
