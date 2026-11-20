//! A publish request must reach `xpst-pipeline` with the request's own content,
//! and the answer must be the pipeline's, not a canned success.

mod common;

use std::sync::Arc;

use common::{sandbox, state};
use serde_json::{json, Value};
use xpst_api::AppState;
use xpst_pipeline::fake::{CallLog, FakePublisher};

fn clip(dir: &std::path::Path) -> String {
    let path = dir.join("clip.mp4");
    std::fs::write(&path, vec![0u8; 2_048]).expect("write clip");
    path.display().to_string()
}

async fn create_and_confirm(state: Arc<AppState>, dir: &std::path::Path) -> (String, Value) {
    let (status, created) = common::post_json(
        state.clone(),
        "/api/plans",
        json!({
            "media_paths": [clip(dir)],
            "caption": "a caption for the plan",
            "platforms": ["youtube"],
            "mode": "shorts",
            "idempotency_key": "test-key-1",
        }),
    )
    .await;
    assert_eq!(status.as_u16(), 201, "create: {created}");
    let plan_id = created["plan_id"].as_str().expect("plan id").to_string();
    let (status, confirmed) = common::post_json(
        state,
        &format!("/api/plans/{plan_id}/confirm"),
        json!({"digest": created["digest"]}),
    )
    .await;
    assert_eq!(status.as_u16(), 200, "confirm: {confirmed}");
    (plan_id, created)
}

#[tokio::test]
async fn a_publish_request_reaches_the_pipeline_with_the_plans_content() {
    let dir = sandbox("publish");
    let log = CallLog::new();
    let publisher = Arc::new(FakePublisher::publishing("youtube").logging_on(log.clone()));
    let state = Arc::new(
        AppState::new(&dir)
            .with_auth(
                xpst_api::AuthConfig::new()
                    .with_token("test-token")
                    .with_config_dir(&dir),
            )
            .with_publisher(publisher.clone()),
    );

    let (plan_id, created) = create_and_confirm(state.clone(), &dir).await;
    assert_eq!(created["platforms"], json!(["youtube"]));
    assert_eq!(created["caption"], json!("a caption for the plan"));

    let (status, record) =
        common::post_json(state, &format!("/api/plans/{plan_id}/publish"), json!({})).await;
    assert_eq!(status.as_u16(), 200, "publish: {record}");

    let destinations = record["result"]["destinations"]
        .as_array()
        .expect("destinations");
    assert_eq!(destinations.len(), 1);
    assert_eq!(destinations[0]["platform"], json!("youtube"));
    assert_eq!(destinations[0]["outcome"], json!("published"));
    assert_eq!(destinations[0]["post_id"], json!("youtube-post-1"));
    assert_eq!(record["result"]["counts"]["published"], json!(1));

    // The provider was called exactly once, from the pipeline.
    assert_eq!(publisher.publish_calls(), 1);
    assert_eq!(log.count("publish:youtube"), 1);
}

#[tokio::test]
async fn publishing_an_unconfirmed_plan_is_refused() {
    let dir = sandbox("publish-unconfirmed");
    let state = state(&dir);
    let (status, created) = common::post_json(
        state.clone(),
        "/api/plans",
        json!({"media_paths": [clip(&dir)], "caption": "c", "platforms": ["youtube"], "mode": "shorts"}),
    )
    .await;
    assert_eq!(status.as_u16(), 201, "{created}");
    let plan_id = created["plan_id"].as_str().unwrap();
    let (status, body) =
        common::post_json(state, &format!("/api/plans/{plan_id}/publish"), json!({})).await;
    assert_eq!(status.as_u16(), 409);
    assert_eq!(body["code"], json!("PLAN_NOT_CONFIRMED"));
}

#[tokio::test]
async fn a_confirmed_plan_with_no_publisher_refuses_instead_of_pretending() {
    let dir = sandbox("publish-no-provider");
    let state = state(&dir);
    let (plan_id, _) = create_and_confirm(state.clone(), &dir).await;
    let (status, body) =
        common::post_json(state, &format!("/api/plans/{plan_id}/publish"), json!({})).await;
    assert_eq!(status.as_u16(), 409, "{body}");
    assert_eq!(body["code"], json!("PLAN_NO_PUBLISHER"));
    assert!(body["message"]
        .as_str()
        .unwrap()
        .contains("no publisher is registered"));
}

#[tokio::test]
async fn a_dry_run_reports_the_frozen_intents_without_calling_a_provider() {
    let dir = sandbox("publish-dry-run");
    let publisher = Arc::new(FakePublisher::publishing("youtube"));
    let state = Arc::new(
        AppState::new(&dir)
            .with_auth(
                xpst_api::AuthConfig::new()
                    .with_token("test-token")
                    .with_config_dir(&dir),
            )
            .with_publisher(publisher.clone()),
    );
    let (plan_id, _) = create_and_confirm(state.clone(), &dir).await;
    let (status, body) = common::post_json(
        state,
        &format!("/api/plans/{plan_id}/publish"),
        json!({"dry_run": true}),
    )
    .await;
    assert_eq!(status.as_u16(), 200, "{body}");
    let rows = body["dry_run_result"]["destinations"].as_array().unwrap();
    assert_eq!(rows[0]["would_publish"], json!(true));
    assert_eq!(rows[0]["caption"], json!("a caption for the plan"));
    assert_eq!(publisher.publish_calls(), 0, "a dry run must not publish");
}

#[tokio::test]
async fn an_unknown_plan_is_a_typed_404() {
    let dir = sandbox("plan-unknown");
    let state = state(&dir);
    let (status, body) = common::get(state.clone(), "/api/plans/plan_missing").await;
    assert_eq!(status.as_u16(), 404);
    assert_eq!(body["code"], json!("PLAN_UNKNOWN"));

    let (status, body) =
        common::post_json(state, "/api/plans/plan_missing/confirm", json!({})).await;
    assert_eq!(status.as_u16(), 404);
    assert_eq!(body["code"], json!("PLAN_UNKNOWN"));
}

#[tokio::test]
async fn creating_the_same_plan_twice_with_one_idempotency_key_replays_it() {
    let dir = sandbox("plan-idempotent");
    let state = state(&dir);
    let body = json!({
        "media_paths": [clip(&dir)],
        "caption": "same",
        "platforms": ["youtube"],
        "idempotency_key": "same-key",
    });
    let (status, first) = common::post_json(state.clone(), "/api/plans", body.clone()).await;
    assert_eq!(status.as_u16(), 201, "{first}");
    let (status, second) = common::post_json(state, "/api/plans", body).await;
    assert_eq!(status.as_u16(), 200, "a replay is not a new plan");
    assert_eq!(first["plan_id"], second["plan_id"]);
    assert_eq!(second["replayed"], json!(true));
}

#[tokio::test]
async fn a_plan_without_media_is_refused_with_pythons_code() {
    let dir = sandbox("plan-no-media");
    let state = state(&dir);
    let (status, body) = common::post_json(state, "/api/plans", json!({"caption": "hi"})).await;
    assert_eq!(status.as_u16(), 400);
    assert_eq!(body["code"], json!("PLAN_NO_MEDIA"));
    assert_eq!(body["blocked"], json!(true));
}

#[tokio::test]
async fn confirming_with_a_foreign_digest_is_a_conflict() {
    let dir = sandbox("plan-stale");
    let state = state(&dir);
    let (status, created) = common::post_json(
        state.clone(),
        "/api/plans",
        json!({"media_paths": [clip(&dir)], "caption": "c", "platforms": ["youtube"], "mode": "shorts"}),
    )
    .await;
    assert_eq!(status.as_u16(), 201);
    let plan_id = created["plan_id"].as_str().unwrap();
    let (status, body) = common::post_json(
        state,
        &format!("/api/plans/{plan_id}/confirm"),
        json!({"digest": "not-the-digest"}),
    )
    .await;
    assert_eq!(status.as_u16(), 409);
    assert_eq!(body["code"], json!("PLAN_CONFIRM_STALE"));
}
