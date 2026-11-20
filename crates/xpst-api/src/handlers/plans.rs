//! Plan → confirm → publish, backed by `xpst-pipeline`.
//!
//! The HTTP surface owns no publishing logic: it freezes what the caller asked
//! for, hands it to [`xpst_pipeline::Pipeline`], and reports exactly what the
//! pipeline answered. That is what makes a publish request here the same event
//! as a publish request from the CLI or MCP — same gate, same providers, same
//! outcomes.
//!
//! Three refusals are the contract, not an edge case:
//!
//! * a plan that does not exist is `PLAN_UNKNOWN` (404), never an empty plan;
//! * publishing an unconfirmed plan is `PLAN_NOT_CONFIRMED` (409), never an
//!   implicit confirm;
//! * a destination with no publisher is reported as such (409), never as a
//!   successful post.
//!
//! Plans live in this process's memory: they are frozen decisions about media
//! on this machine and the Rust engine has no on-disk plan store yet. A restart
//! makes every plan id unknown, which is the honest answer.

use axum::http::StatusCode;
use axum::response::Response;
use serde_json::{json, Map, Value};

use super::{api_error, blocked_error, json_response};
use crate::app::{PlanEntry, RequestContext};
use crate::auth::sha256_hex;

const ERROR_UNKNOWN_PLAN: &str = "PLAN_UNKNOWN";
const ERROR_NO_MEDIA: &str = "PLAN_NO_MEDIA";
const ERROR_NOT_CONFIRMED: &str = "PLAN_NOT_CONFIRMED";
const ERROR_STALE: &str = "PLAN_CONFIRM_STALE";
const ERROR_PREVIEW_FAILED: &str = "PLAN_PREVIEW_FAILED";
const ERROR_NO_PUBLISHER: &str = "PLAN_NO_PUBLISHER";
const ERROR_UNKNOWN_PLAN_PLATFORM: &str = "PLAN_UNKNOWN_PLATFORM";

fn unknown_plan() -> Response {
    blocked_error(
        StatusCode::NOT_FOUND,
        ERROR_UNKNOWN_PLAN,
        "No plan with that id. It may have been created on another engine or removed.",
    )
}

/// Everything the local planner can say about one request.
struct Preview {
    value: Value,
    plan: xpst_core::plan::RoutePlan,
    measurements: Vec<xpst_core::plan::MediaMeasurement>,
}

#[allow(clippy::result_large_err)] // the error IS the response to send
fn preview(
    config: Option<&Value>,
    media_paths: &[String],
    caption: &str,
    platforms: &[String],
    overrides: &Map<String, Value>,
    content_type: &str,
    mode: Option<xpst_core::envelope::VideoMode>,
) -> Result<Preview, Response> {
    let media: Vec<xpst_core::plan::MediaMeasurement> = media_paths
        .iter()
        .map(|path| {
            let expanded = xpst_config::expand_home(path);
            let metadata = std::fs::metadata(&expanded).ok();
            let container = std::path::Path::new(&expanded)
                .extension()
                .and_then(|extension| extension.to_str())
                .map(|extension| format!(".{}", extension.to_ascii_lowercase()))
                .unwrap_or_default();
            xpst_core::plan::MediaMeasurement {
                container,
                size_bytes: metadata.as_ref().map(std::fs::Metadata::len),
                name: Some(path.clone()),
                duration_s: None,
                width: None,
                height: None,
                video_codec: None,
                has_audio: false,
            }
        })
        .collect();
    let mut request = xpst_core::plan::RouteRequest::new(media.clone(), caption.to_string());
    if !content_type.is_empty() {
        request = request.with_content_type(content_type.to_string());
    }
    if let Some(mode) = mode {
        request = request.with_mode(mode);
    }
    let destinations: Vec<xpst_core::ingest::DestinationCapability> = platforms
        .iter()
        .map(|platform| {
            let enabled = super::reads::platform_enabled(config, platform);
            xpst_core::ingest::DestinationCapability::built_in(platform, enabled, false)
                .unwrap_or_else(|| {
                    xpst_core::ingest::DestinationCapability::undeclared(platform, enabled, false)
                })
        })
        .collect();
    let plan = xpst_core::plan::plan(&request, &destinations);

    let blockers: Vec<String> = plan
        .destinations
        .iter()
        .flat_map(|verdict| verdict.refusals.iter().map(|reason| reason.message.clone()))
        .collect();
    let rows: Vec<Value> = plan
        .destinations
        .iter()
        .map(|verdict| {
            json!({
                "platform": verdict.platform,
                "state": verdict.state.as_str(),
                "will_post": verdict.accepted,
                "reason": verdict.refusals.first().map(|reason| reason.message.clone()),
                "content_type": verdict.content_type,
                "blockers": verdict.refusals.iter()
                    .map(|reason| json!({ "code": reason.code, "message": reason.message }))
                    .collect::<Vec<_>>(),
                "warnings": verdict.advisories.iter()
                    .map(|reason| json!({ "code": reason.code, "message": reason.message }))
                    .collect::<Vec<_>>(),
            })
        })
        .collect();
    let ready = blockers.is_empty() && plan.request_blockers.is_empty();
    let value = json!({
        "ok": ready,
        "ready": ready,
        "content_type": plan.content_type.map(|content_type| content_type.as_str()),
        "media_paths": media_paths,
        "caption": caption,
        "overrides": overrides,
        "platforms": platforms,
        "route": rows,
        "blockers": blockers,
        "errors": [],
        "request_blockers": plan.request_blockers.iter()
            .map(|reason| json!({ "code": reason.code, "message": reason.message }))
            .collect::<Vec<_>>(),
        "plan": {
            "hard_blockers": plan.destinations.iter()
                .flat_map(|verdict| verdict.refusals.iter().map(|reason| json!({
                    "code": reason.code,
                    "message": reason.message,
                    "platform": verdict.platform,
                })))
                .collect::<Vec<_>>(),
            "warnings": plan.destinations.iter()
                .flat_map(|verdict| verdict.advisories.iter().map(|reason| json!({
                    "code": reason.code,
                    "message": reason.message,
                })))
                .collect::<Vec<_>>(),
            "network_calls": false,
        },
        "network_calls": false,
    });
    if plan.content_type.is_none() && !content_type.is_empty() && content_type != "auto" {
        return Err(blocked_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            ERROR_PREVIEW_FAILED,
            format!("{content_type} is not a content type this engine publishes"),
        ));
    }
    Ok(Preview {
        value,
        plan,
        measurements: media,
    })
}

const MUTATING_HINT: &str =
    "plans are frozen decisions about local media; pass media_paths or media_ids";

fn plan_id_for(context: &RequestContext<'_>, seed: &str) -> String {
    let sequence = context.state.plans().sequence();
    let digest = sha256_hex(&format!(
        "{seed}:{}:{sequence}:{}",
        context.state.now_ms(),
        context.state.config_dir().display()
    ));
    format!("plan_{}", &digest[..16])
}

fn digest_for(inputs: &Value) -> String {
    sha256_hex(&serde_json::to_string(inputs).unwrap_or_default())
}

/// Extract `media_paths`, honouring `media_path` and refusing `media_ids` that
/// this build cannot resolve.
#[allow(clippy::result_large_err)] // the error IS the response to send
fn media_paths_from(body: &Value) -> Result<Vec<String>, Response> {
    if let Some(Value::Array(items)) = body.get("media_paths") {
        let paths: Vec<String> = items
            .iter()
            .filter_map(Value::as_str)
            .map(str::trim)
            .filter(|item| !item.is_empty())
            .map(str::to_string)
            .collect();
        if !paths.is_empty() {
            return Ok(paths);
        }
    }
    if let Some(single) = body.get("media_path").and_then(Value::as_str) {
        let single = single.trim();
        if !single.is_empty() {
            return Ok(vec![single.to_string()]);
        }
    }
    if let Some(Value::Array(ids)) = body.get("media_ids") {
        if !ids.is_empty() {
            return Err(api_error(
                StatusCode::BAD_REQUEST,
                "PLAN_MEDIA_IDS_UNSUPPORTED",
                "Staged media ids are not ported to the Rust engine yet; pass media_paths.",
            ));
        }
    }
    Err(blocked_error(
        StatusCode::BAD_REQUEST,
        ERROR_NO_MEDIA,
        "This plan has no media. Upload a file first (POST /api/media) and pass its media id.",
    ))
}

fn platforms_from(body: &Value) -> Vec<String> {
    body.get("platforms")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(|item| item.trim().to_ascii_lowercase())
                .filter(|item| !item.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

fn overrides_from(body: &Value) -> Map<String, Value> {
    body.get("overrides")
        .and_then(Value::as_object)
        .map(|entries| {
            entries
                .iter()
                .filter_map(|(platform, raw)| {
                    let text = match raw {
                        Value::String(text) => Some(text.clone()),
                        Value::Object(fields) => fields
                            .get("text")
                            .or_else(|| fields.get("caption"))
                            .and_then(Value::as_str)
                            .map(str::to_string),
                        _ => None,
                    };
                    text.map(|text| (platform.trim().to_ascii_lowercase(), Value::String(text)))
                })
                .collect()
        })
        .unwrap_or_default()
}

/// `POST /api/plans` — freeze a plan.
pub fn create(context: &RequestContext<'_>) -> Response {
    let body = match context.json_body() {
        Ok(body) => body,
        Err(response) => return response,
    };
    let media_paths = match media_paths_from(&body) {
        Ok(paths) => paths,
        Err(response) => return response,
    };
    let caption = body
        .get("caption")
        .or_else(|| body.get("text"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let platforms = platforms_from(&body);
    if platforms.is_empty() {
        return blocked_error(
            StatusCode::BAD_REQUEST,
            ERROR_UNKNOWN_PLAN_PLATFORM,
            "Choose at least one destination platform.",
        );
    }
    let overrides = overrides_from(&body);
    let content_type = body
        .get("content_type")
        .or_else(|| body.get("format"))
        .and_then(Value::as_str)
        .unwrap_or("auto")
        .to_string();
    let mode = match body
        .get("mode")
        .or_else(|| body.get("video_mode"))
        .and_then(Value::as_str)
    {
        Some(value) => match xpst_core::envelope::coerce_video_mode(value) {
            Ok(mode) => Some(mode),
            Err(error) => {
                return blocked_error(
                    StatusCode::BAD_REQUEST,
                    "PLAN_BAD_VIDEO_MODE",
                    format!("Unknown video mode: {error}"),
                )
            }
        },
        None => None,
    };
    let idempotency_key = body
        .get("idempotency_key")
        .or_else(|| body.get("plan_idempotency_key"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();

    let raw_document = super::raw_config(context.state.config_dir());
    let prepared = match preview(
        raw_document.as_ref(),
        &media_paths,
        &caption,
        &platforms,
        &overrides,
        &content_type,
        mode,
    ) {
        Ok(prepared) => prepared,
        Err(response) => return response,
    };

    // Idempotency: the same key with the same inputs returns the same plan.
    if !idempotency_key.is_empty() {
        let store = context.state.plans();
        for entry in store.entries() {
            let record = &entry.record;
            let same_key = record.get("client_idempotency_key").and_then(Value::as_str)
                == Some(idempotency_key.as_str());
            let same_inputs = record.get("caption").and_then(Value::as_str)
                == Some(caption.as_str())
                && record.get("platforms") == Some(&json!(platforms));
            if same_key && same_inputs {
                let mut replayed = record.clone();
                replayed["replayed"] = json!(true);
                return json_response(StatusCode::OK, replayed);
            }
        }
    }

    let media_material: Vec<Value> = prepared
        .measurements
        .iter()
        .zip(media_paths.iter())
        .map(|(measurement, path)| {
            json!({
                "path": path,
                "size_bytes": measurement.size_bytes,
                "container": measurement.container,
            })
        })
        .collect();
    let digest = digest_for(&json!({
        "media": media_material,
        "caption": caption,
        "overrides": overrides,
        "platforms": platforms,
        "preview": prepared.value,
    }));
    let plan_id = plan_id_for(context, &digest);
    let now = context.state.now_ms();

    let content_hash = digest.clone();
    let mut content = xpst_pipeline::PublicationContent::new(content_hash.clone(), caption.clone())
        .with_media(
            media_paths
                .iter()
                .map(|path| xpst_pipeline::MediaRef::new(path.clone(), content_hash.clone()))
                .collect(),
        );
    for platform in &platforms {
        let text = overrides
            .get(platform)
            .and_then(Value::as_str)
            .unwrap_or(&caption)
            .to_string();
        content = content.with_caption_for(platform.clone(), text);
    }

    let record = json!({
        "plan_id": plan_id,
        "created_at": now,
        "updated_at": now,
        "state": "previewed",
        "client_idempotency_key": if idempotency_key.is_empty() { Value::Null } else { json!(idempotency_key) },
        "media": media_material,
        "media_ids": [],
        "media_probe": [],
        "caption": caption,
        "overrides": overrides,
        "platforms": platforms,
        "content_type": prepared.plan.content_type.map(|content_type| content_type.as_str()),
        "preview": prepared.value,
        "digest": digest,
        "confirmed_at": Value::Null,
        "confirmations": [],
        "confirmed_destinations": [],
        "result": Value::Null,
        "dry_run_result": Value::Null,
        "replayed_result": false,
    });

    context.state.plans().insert(
        plan_id,
        PlanEntry {
            record: record.clone(),
            plan: prepared.plan,
            confirmed: None,
            content,
        },
    );
    json_response(StatusCode::CREATED, record)
}

/// `GET /api/plans/{plan_id}` — the frozen plan.
pub fn get(context: &RequestContext<'_>) -> Response {
    let plan_id = context.param("plan_id").unwrap_or_default().to_string();
    let store = context.state.plans();
    match store.get(&plan_id) {
        Some(entry) => json_response(StatusCode::OK, entry.record.clone()),
        None => unknown_plan(),
    }
}

/// `POST /api/plans/{plan_id}/confirm` — freeze it for publishing.
pub fn confirm(context: &RequestContext<'_>) -> Response {
    let plan_id = context.param("plan_id").unwrap_or_default().to_string();
    let body = match context.json_body() {
        Ok(body) => body,
        Err(response) => return response,
    };
    let confirm_stale = body
        .get("confirm_stale")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let client_digest = body
        .get("digest")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();

    let mut store = context.state.plans();
    let Some(entry) = store.get_mut(&plan_id) else {
        return unknown_plan();
    };
    if entry.record.get("state").and_then(Value::as_str) == Some("published") {
        let mut record = entry.record.clone();
        record["already_published"] = json!(true);
        return json_response(StatusCode::OK, record);
    }
    if !client_digest.is_empty()
        && entry.record.get("digest").and_then(Value::as_str) != Some(client_digest.as_str())
    {
        return blocked_error(
            StatusCode::CONFLICT,
            ERROR_STALE,
            "The client confirmed a different plan version than the engine holds. Reload the preview.",
        );
    }
    let digest = entry
        .record
        .get("digest")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let confirmed = match xpst_pipeline::confirm(&entry.plan, &entry.content) {
        Ok(confirmed) => confirmed,
        Err(error) => {
            return blocked_error(
                StatusCode::CONFLICT,
                ERROR_STALE,
                format!("This plan cannot be confirmed: {error}"),
            )
        }
    };
    if !confirm_stale && confirmed.destinations().is_empty() {
        return blocked_error(
            StatusCode::CONFLICT,
            ERROR_STALE,
            "This plan would publish to no destination; nothing is confirmed.",
        );
    }
    let now = context.state.now_ms();
    let destinations: Vec<String> = confirmed
        .destinations()
        .iter()
        .map(|record| record.platform().to_string())
        .collect();
    entry.confirmed = Some(confirmed);
    entry.record["state"] = json!("confirmed");
    entry.record["confirmed_at"] = json!(now);
    entry.record["updated_at"] = json!(now);
    entry.record["confirmations"] = json!([{ "digest": digest, "at": now }]);
    entry.record["confirmed_destinations"] = json!(destinations);
    json_response(StatusCode::OK, entry.record.clone())
}

fn destination_rows(confirmed: Option<&xpst_pipeline::ConfirmedPlan>, verify: bool) -> Vec<Value> {
    let Some(confirmed) = confirmed else {
        return Vec::new();
    };
    confirmed
        .destinations()
        .iter()
        .map(|record| {
            let outcome = record.outcome();
            let (post_id, url, verification) = match outcome {
                Some(xpst_pipeline::DestinationOutcome::Published(published)) => (
                    Value::String(published.post_id.clone()),
                    published.url.clone().map_or(Value::Null, Value::String),
                    if verify {
                        "verified"
                    } else {
                        "submitted_unconfirmed"
                    },
                ),
                Some(xpst_pipeline::DestinationOutcome::SkippedDuplicate(duplicate)) => (
                    duplicate
                        .existing_post_id
                        .clone()
                        .map_or(Value::Null, Value::String),
                    Value::Null,
                    "skipped_duplicate",
                ),
                _ => (Value::Null, Value::Null, "submitted_unconfirmed"),
            };
            json!({
                "platform": record.platform(),
                "state": outcome.as_str_or("pending"),
                "outcome": outcome.map(xpst_pipeline::DestinationOutcome::as_str),
                "post_id": post_id,
                "url": url,
                "verification": verification,
                "attempts": record.attempts(),
                "confirmed": true,
            })
        })
        .collect()
}

trait OutcomeWord {
    fn as_str_or(&self, fallback: &'static str) -> &'static str;
}

impl OutcomeWord for Option<&xpst_pipeline::DestinationOutcome> {
    fn as_str_or(&self, fallback: &'static str) -> &'static str {
        self.map_or(fallback, xpst_pipeline::DestinationOutcome::as_str)
    }
}

/// `GET /api/plans/{plan_id}/readback` — per-destination ids and URLs.
pub fn readback(context: &RequestContext<'_>) -> Response {
    let plan_id = context.param("plan_id").unwrap_or_default().to_string();
    let verify = context.query_flag("verify");
    let store = context.state.plans();
    let Some(entry) = store.get(&plan_id) else {
        return unknown_plan();
    };
    json_response(
        StatusCode::OK,
        json!({
            "ok": true,
            "plan_id": plan_id,
            "verify": verify,
            "destinations": destination_rows(entry.confirmed.as_ref(), verify),
            "written": false,
        }),
    )
}

/// The result envelope for one publish report.
fn result_envelope(report: &xpst_pipeline::PlanReport) -> Value {
    let counts = report.counts();
    json!({
        "ok": counts.published + counts.skipped_duplicate > 0
            && counts.failed_retryable == 0
            && counts.failed_permanent == 0
            && counts.refused == 0,
        "plan_id": report.plan_id(),
        "state": report.state().as_str(),
        "counts": {
            "published": counts.published,
            "skipped_duplicate": counts.skipped_duplicate,
            "deferred": counts.deferred,
            "refused": counts.refused,
            "failed_retryable": counts.failed_retryable,
            "failed_permanent": counts.failed_permanent,
            "unconfirmed": counts.unconfirmed,
            "pending": counts.pending,
        },
        "destinations": report.entries().iter().map(|entry| {
            let outcome = entry.outcome.as_ref();
            let (post_id, url, detail) = match outcome {
                Some(xpst_pipeline::DestinationOutcome::Published(published)) => (
                    Value::String(published.post_id.clone()),
                    published.url.clone().map_or(Value::Null, Value::String),
                    Value::Null,
                ),
                Some(xpst_pipeline::DestinationOutcome::Failed(failed)) => (
                    Value::Null,
                    Value::Null,
                    json!({ "code": failed.code, "message": failed.detail }),
                ),
                Some(xpst_pipeline::DestinationOutcome::Refused(refused)) => (
                    Value::Null,
                    Value::Null,
                    json!({ "code": refused.code, "message": refused.detail }),
                ),
                Some(xpst_pipeline::DestinationOutcome::Deferred(deferred)) => (
                    Value::Null,
                    Value::Null,
                    json!({ "code": "DEFERRED", "message": deferred.detail }),
                ),
                Some(xpst_pipeline::DestinationOutcome::Unconfirmed(unconfirmed)) => (
                    Value::Null,
                    Value::Null,
                    json!({ "code": "UNCONFIRMED", "message": unconfirmed.detail }),
                ),
                Some(xpst_pipeline::DestinationOutcome::SkippedDuplicate(duplicate)) => (
                    duplicate.existing_post_id.clone().map_or(Value::Null, Value::String),
                    Value::Null,
                    json!({ "code": "SKIPPED_DUPLICATE", "message": "this content was already live" }),
                ),
                None => (Value::Null, Value::Null, Value::Null),
            };
            json!({
                "platform": entry.platform,
                "outcome": entry.outcome_word(),
                "attempted_this_run": entry.attempted_this_run,
                "attempts_this_run": entry.attempts_this_run,
                "post_id": post_id,
                "url": url,
                "error": detail,
                "state_write_error": entry.state_write_error,
            })
        }).collect::<Vec<_>>(),
        "network_calls": true,
    })
}

/// `POST /api/plans/{plan_id}/publish`.
pub async fn publish(context: &RequestContext<'_>) -> Response {
    let plan_id = context.param("plan_id").unwrap_or_default().to_string();
    let body = match context.json_body() {
        Ok(body) => body,
        Err(response) => return response,
    };
    let dry_run = body
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    {
        let store = context.state.plans();
        let Some(entry) = store.get(&plan_id) else {
            return unknown_plan();
        };
        if entry.confirmed.is_none() {
            return blocked_error(
                StatusCode::CONFLICT,
                ERROR_NOT_CONFIRMED,
                "Confirm this plan before publishing it.",
            );
        }
    }

    if dry_run {
        // No provider call, no gate consumption: report what *would* be
        // attempted from the frozen intents.
        let store = context.state.plans();
        let Some(entry) = store.get(&plan_id) else {
            return unknown_plan();
        };
        let confirmed = entry.confirmed.as_ref().expect("checked above");
        let rows: Vec<Value> = confirmed
            .destinations()
            .iter()
            .map(|record| {
                let intent = record.intent();
                json!({
                    "platform": record.platform(),
                    "would_publish": intent.is_some(),
                    "delivery_key": intent.map(|intent| intent.key.as_str().to_string()),
                    "caption": intent.map(|intent| intent.caption.clone()),
                    "media": intent.map(|intent| intent.media.iter().map(|item| item.path.clone()).collect::<Vec<_>>()),
                    "content_hash": intent.map(|intent| intent.key.as_str().to_string()),
                    "reason": intent.is_none().then_some("refused by the plan"),
                })
            })
            .collect();
        let envelope = json!({
            "ok": rows.iter().all(|row| row["would_publish"] == json!(true)),
            "plan_id": plan_id,
            "state": "dry_run",
            "counts": {
                "would_publish": rows.iter().filter(|row| row["would_publish"] == json!(true)).count(),
                "refused": rows.iter().filter(|row| row["would_publish"] == json!(false)).count(),
            },
            "destinations": rows,
            "network_calls": false,
        });
        drop(store);
        let mut store = context.state.plans();
        if let Some(entry) = store.get_mut(&plan_id) {
            entry.record["dry_run_result"] = envelope.clone();
            entry.record["updated_at"] = json!(context.state.now_ms());
            let mut record = entry.record.clone();
            record["dry_run_result"] = envelope;
            return json_response(StatusCode::OK, record);
        }
        return unknown_plan();
    }

    // ── The real path: limits gate → provider → outcome → state write ──
    //
    // The frozen plan is moved out of the table, and the publish itself runs on
    // a blocking thread: no lock is held across a provider call, no upload can
    // stall a read, and the future this handler returns stays `Send`.
    let taken = {
        let mut store = context.state.plans();
        let Some(entry) = store.get_mut(&plan_id) else {
            return unknown_plan();
        };
        entry.confirmed.take()
    };
    let Some(mut confirmed_plan) = taken else {
        return blocked_error(
            StatusCode::CONFLICT,
            ERROR_NOT_CONFIRMED,
            "Confirm this plan before publishing it.",
        );
    };

    let held: Vec<std::sync::Arc<dyn xpst_pipeline::DestinationPublisher>> = confirmed_plan
        .destinations()
        .iter()
        .filter(|record| record.intent().is_some())
        .filter_map(|record| context.state.publishers().get(record.platform()))
        .collect();
    let config_dir = context.state.config_dir().to_path_buf();
    let now_ms = context.state.now_ms();
    let account = context.state.account().to_string();

    let outcome = tokio::task::spawn_blocking(move || {
        let mut gate = match xpst_pipeline::EnvelopeGate::open(&config_dir) {
            Ok(gate) => gate,
            Err(error) => {
                return Err(format!(
                    "The rate-limit ledger could not be opened: {error}"
                ))
            }
        };
        let mut set = xpst_pipeline::PublisherSet::new();
        for publisher in &held {
            set.insert(publisher.as_ref());
        }
        let mut pipeline = xpst_pipeline::Pipeline::new(&mut gate, set);
        let runtime = match tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
        {
            Ok(runtime) => runtime,
            Err(error) => return Err(format!("could not start the publish context: {error}")),
        };
        let report = runtime.block_on(pipeline.publish(
            &mut confirmed_plan,
            xpst_pipeline::PublishContext {
                now_ms,
                account: &account,
                owner: "http",
                state: None,
            },
        ));
        Ok((confirmed_plan, report))
    })
    .await;

    let (confirmed_plan, report) = match outcome {
        Ok(Ok((plan, report))) => (plan, report),
        Ok(Err(message)) => {
            // The ledger could not be opened: nothing was attempted, and the
            // plan's frozen state is unchanged.
            return blocked_error(StatusCode::CONFLICT, ERROR_NO_PUBLISHER, message);
        }
        Err(error) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({
                    "ok": false,
                    "code": "PLAN_PUBLISH_ABORTED",
                    "message": format!("the publish task could not run: {error}"),
                }),
            )
        }
    };

    // Whatever happened, the plan's per-destination outcomes are real state.
    {
        let mut store = context.state.plans();
        if let Some(entry) = store.get_mut(&plan_id) {
            entry.confirmed = Some(confirmed_plan);
        }
    }

    let report = match report {
        Ok(report) => report,
        Err(error) => {
            let (status, code) = match &error {
                xpst_pipeline::PublishError::NoPublisher { .. } => {
                    (StatusCode::CONFLICT, ERROR_NO_PUBLISHER)
                }
                _ => (StatusCode::CONFLICT, "PLAN_PUBLISH_REFUSED"),
            };
            return blocked_error(status, code, error.to_string());
        }
    };
    let envelope = result_envelope(&report);
    let blocked = envelope.get("ok").and_then(Value::as_bool) != Some(true);

    let mut store = context.state.plans();
    let Some(entry) = store.get_mut(&plan_id) else {
        return unknown_plan();
    };
    entry.record["result"] = envelope.clone();
    entry.record["updated_at"] = json!(context.state.now_ms());
    if !blocked {
        entry.record["state"] = json!("published");
    }
    let record = entry.record.clone();
    drop(store);
    json_response(
        if blocked {
            StatusCode::CONFLICT
        } else {
            StatusCode::OK
        },
        record,
    )
}

/// Kept public so a caller can print the media-no-media hint without importing
/// the response helpers.
#[must_use]
pub fn mutating_hint() -> &'static str {
    MUTATING_HINT
}
