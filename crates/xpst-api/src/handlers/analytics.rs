//! The analytics-backed read routes: `/api/summary`, `/api/analytics/outcomes`,
//! `/api/videos`, `/api/videos/{video_id}`, `/api/activity` and `/state`.
//!
//! Every document is built by `xpst-surface`'s dashboard and analytics
//! builders — the same code the CLI and MCP surfaces render — so the HTTP
//! surface cannot disagree with them about a number. Nothing here opens a
//! socket: the recorded-mode reads are `state.json` plus `analytics.db`.
//!
//! `live=1` on `/api/analytics/outcomes` is the one branch that needs the
//! platform APIs, so it answers a typed `501` naming the network tranche
//! instead of serving a recorded report labelled live.

use axum::http::StatusCode;
use axum::response::Response;
use serde_json::json;

use super::{http_error, json_response, ok};
use crate::app::RequestContext;

/// The process clock as a `jiff` instant.
///
/// `AppState::with_clock` pins it, which is what makes the freshness fields
/// reproducible in a test; a real server uses the wall clock.
fn now(context: &RequestContext<'_>) -> jiff::Timestamp {
    jiff::Timestamp::from_millisecond(context.state.now_ms())
        .unwrap_or_else(|_| jiff::Timestamp::now())
}

/// `GET /api/summary` — aggregate statistics from `state.json` + the ledger.
pub fn summary(context: &RequestContext<'_>) -> Response {
    ok(xpst_surface::dashboard::summary(
        context.state.config_dir(),
        now(context),
    ))
}

/// `GET /state` — the same document `/api/summary` serves.
///
/// Python declares the route twice (once in `server.py`, once in the `/api`
/// router) and both call `cached_summary_stats`, so the bodies are identical.
pub fn state(context: &RequestContext<'_>) -> Response {
    summary(context)
}

/// `GET /api/analytics/outcomes` — per-post/per-platform outcomes.
///
/// `live=0` (the default) reads the persisted snapshots and the verified
/// ownership set: no metric network calls. `live=1` runs a real collection in
/// Python; that is the network tranche, so this build refuses it rather than
/// labelling recorded numbers as live.
pub fn analytics_outcomes(context: &RequestContext<'_>) -> Response {
    let live = context
        .query_value("live")
        .map(|value| value.trim().parse::<i64>().unwrap_or(0))
        .unwrap_or(0)
        != 0;
    if live {
        return json_response(
            StatusCode::NOT_IMPLEMENTED,
            json!({
                "ok": false,
                "code": "LIVE_COLLECTION_NOT_IMPLEMENTED",
                "route": context.spec.id,
                "method": context.spec.method.as_str(),
                "path": context.spec.path,
                "reason": LIVE_COLLECTION_REASON,
                "error": { "code": "LIVE_COLLECTION_NOT_IMPLEMENTED", "message": LIVE_COLLECTION_REASON },
            }),
        );
    }
    let report = xpst_surface::analytics::outcome_report(context.state.config_dir(), now(context));
    ok(xpst_config::output::to_json(&report))
}

const LIVE_COLLECTION_REASON: &str = "live metric collection is not ported: it calls every platform's analytics API, which is the network tranche; `live=0` serves the recorded report";

/// `GET /api/videos` — the video lineup plus per-platform totals.
pub fn videos(context: &RequestContext<'_>) -> Response {
    ok(xpst_surface::dashboard::videos(context.state.config_dir()))
}

/// `GET /api/videos/{video_id}` — one video's posts and snapshot series.
pub fn video_detail(context: &RequestContext<'_>) -> Response {
    let video_id = context.param("video_id").unwrap_or_default();
    match xpst_surface::dashboard::video_detail(context.state.config_dir(), video_id) {
        Some(body) => ok(body),
        // Python: `raise HTTPException(404, detail=f"Unknown video: {video_id}")`.
        None => http_error(StatusCode::NOT_FOUND, format!("Unknown video: {video_id}")),
    }
}

/// `GET /api/activity` — recorded failures with recovery metadata.
pub fn activity(context: &RequestContext<'_>) -> Response {
    ok(xpst_surface::dashboard::activity(
        context.state.config_dir(),
    ))
}
