//! Parity: the Rust route planner against fixtures captured from Python.
//!
//! Every row of `fixtures/route_plan.json` was produced by *running* the
//! shipped Python decision layer (`scripts/capture_route_plan.py`), never
//! transcribed by hand: the ingest specs, text limits, envelope summaries,
//! caption refusals and video-envelope refusals are direct calls, and the plan
//! rows are composed from those same calls in the order
//! `route_plan.build_route_plan` decides them. Where the shipped planner can be
//! driven over the same request, the capture script asserts agreement with it
//! before writing the file (`python_crosscheck`).
//!
//! Regenerate the fixture with:
//!
//! ```text
//! PYTHONPATH=src python scripts/capture_route_plan.py
//! ```

use serde_json::Value;
use xpst_core::content::{
    media_kind, text_limit, ContentType, MediaKind, MediaTransport, CONTENT_TYPES, MAX_MEDIA_ITEMS,
    TEXT_LIMITS,
};
use xpst_core::envelope::{
    coerce_video_mode, video_envelope, video_envelope_violation, MediaMeasurements, VideoMode,
};
use xpst_core::ingest::{destinations_accepting, ingest_spec, DestinationCapability, INGEST_SPECS};
use xpst_core::plan::{
    envelope_summary, modality_unsupported_message, plan, python_float, state_for_code,
    DestinationState, MediaMeasurement, RouteRequest, TransformAction,
};

const FIXTURE: &str = include_str!("fixtures/route_plan.json");

fn document() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture is valid JSON")
}

fn rows<'a>(document: &'a Value, key: &str) -> &'a Vec<Value> {
    document
        .get(key)
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("fixture key `{key}` must be an array"))
}

/// Rows, with the count the capture recorded.
///
/// A parity test that iterates an empty collection asserts nothing and still
/// passes, so emptying a fixture array used to turn these tests into no-ops
/// while CI stayed green. Every loop states how many rows the capture produced;
/// a truncated fixture fails here, naming the collection, instead of passing
/// silently.
fn rows_exact<'a>(document: &'a Value, key: &str, expected: usize) -> &'a Vec<Value> {
    let rows = rows(document, key);
    assert_eq!(
        rows.len(),
        expected,
        "fixture collection `{key}` holds {} rows, not {expected}: the capture wrote an \
         empty or partial collection, or the fixture grew without updating this count",
        rows.len()
    );
    rows
}

fn text<'a>(row: &'a Value, key: &str) -> &'a str {
    row.get(key)
        .and_then(Value::as_str)
        .unwrap_or_else(|| panic!("`{key}` must be a string in {row}"))
}

fn optional_text<'a>(row: &'a Value, key: &str) -> Option<&'a str> {
    match row.get(key) {
        Some(Value::Null) | None => None,
        Some(Value::String(value)) => Some(value),
        other => panic!("`{key}` must be a string or null in {row}: {other:?}"),
    }
}

fn optional_u64(row: &Value, key: &str) -> Option<u64> {
    match row.get(key) {
        Some(Value::Null) | None => None,
        Some(value) => Some(
            value
                .as_u64()
                .unwrap_or_else(|| panic!("`{key}` must be an unsigned integer in {row}")),
        ),
    }
}

fn optional_u32(row: &Value, key: &str) -> Option<u32> {
    optional_u64(row, key).map(|value| u32::try_from(value).expect("fits in u32"))
}

fn optional_f64(row: &Value, key: &str) -> Option<f64> {
    match row.get(key) {
        Some(Value::Null) | None => None,
        Some(value) => Some(
            value
                .as_f64()
                .unwrap_or_else(|| panic!("`{key}` must be a number in {row}")),
        ),
    }
}

fn string_list(row: &Value, key: &str) -> Vec<String> {
    row.get(key)
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("`{key}` must be an array in {row}"))
        .iter()
        .map(|item| {
            item.as_str()
                .unwrap_or_else(|| panic!("`{key}` entries must be strings in {row}"))
                .to_owned()
        })
        .collect()
}

fn bool_field(row: &Value, key: &str) -> bool {
    row.get(key)
        .and_then(Value::as_bool)
        .unwrap_or_else(|| panic!("`{key}` must be a bool in {row}"))
}

fn action(value: &str) -> TransformAction {
    match value {
        "passthrough" => TransformAction::Passthrough,
        "remux" => TransformAction::Remux,
        "transcode" => TransformAction::Transcode,
        other => panic!("unknown transform action in fixture: {other}"),
    }
}

fn measurement(row: &Value) -> MediaMeasurement {
    MediaMeasurement {
        duration_s: optional_f64(row, "duration_s"),
        width: optional_u32(row, "width"),
        height: optional_u32(row, "height"),
        size_bytes: optional_u64(row, "size_bytes"),
        container: optional_text(row, "container").unwrap_or("").to_owned(),
        video_codec: optional_text(row, "video_codec").map(str::to_owned),
        has_audio: row
            .get("has_audio")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        name: optional_text(row, "name").map(str::to_owned),
    }
}

fn request(row: &Value) -> RouteRequest {
    let media = row
        .get("media")
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("request media must be an array in {row}"))
        .iter()
        .map(measurement)
        .collect();
    RouteRequest {
        media,
        caption: text(row, "caption").to_owned(),
        content_type: optional_text(row, "content_type").map(str::to_owned),
        requested_mode: optional_text(row, "requested_mode").map(|value| {
            coerce_video_mode(value).unwrap_or_else(|error| panic!("fixture mode: {error}"))
        }),
    }
}

fn capability(row: &Value) -> DestinationCapability {
    let platform = text(row, "platform");
    let enabled = bool_field(row, "enabled");
    let authenticated = bool_field(row, "authenticated");
    let detail = text(row, "auth_detail");
    let capability = DestinationCapability::built_in(platform, enabled, authenticated)
        .unwrap_or_else(|| DestinationCapability::undeclared(platform, enabled, authenticated));
    capability.with_auth_detail(detail)
}

fn destination_profile(
    platform: &str,
) -> Option<&'static xpst_core::content::DestinationContentProfile> {
    xpst_core::content::content_profile(platform)
}

fn reasons(value: &Value, key: &str) -> Vec<(String, String)> {
    value
        .get(key)
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("`{key}` must be an array in {value}"))
        .iter()
        .map(|item| {
            (
                text(item, "code").to_owned(),
                text(item, "message").to_owned(),
            )
        })
        .collect()
}

#[test]
fn content_vocabulary_matches_python() {
    let document = document();
    let fixture: Vec<String> = string_list(&document, "content_types");
    let rust: Vec<String> = CONTENT_TYPES
        .iter()
        .map(|item| item.as_str().to_owned())
        .collect();
    assert_eq!(fixture, rust);
}

#[test]
fn text_limits_match_python() {
    let document = document();
    let fixture = rows_exact(&document, "text_limits", 6);
    let rust: Vec<(&str, usize)> = TEXT_LIMITS.to_vec();
    let names: Vec<String> = fixture
        .iter()
        .map(|row| text(row, "platform").to_owned())
        .collect();
    let rust_names: Vec<String> = rust.iter().map(|(name, _)| (*name).to_owned()).collect();
    assert_eq!(names, rust_names, "text-limit coverage");
    for row in fixture {
        let platform = text(row, "platform");
        assert_eq!(
            text_limit(platform),
            optional_u64(row, "limit").map(|value| value as usize),
            "{platform} text limit"
        );
    }
}

#[test]
fn ingest_specs_match_python() {
    let document = document();
    let fixture = rows_exact(&document, "ingest", 8);
    let rust: Vec<&str> = INGEST_SPECS.iter().map(|spec| spec.platform).collect();
    let names: Vec<&str> = fixture.iter().map(|row| text(row, "platform")).collect();
    assert_eq!(names, rust, "ingest-spec coverage");

    for row in fixture {
        let platform = text(row, "platform");
        let spec =
            ingest_spec(platform).unwrap_or_else(|| panic!("missing ingest spec {platform}"));
        assert_eq!(spec.display_name, text(row, "display_name"), "{platform}");
        assert_eq!(
            spec.video_containers,
            string_list(row, "video_containers"),
            "{platform}.video_containers"
        );
        assert_eq!(spec.video_codec, text(row, "video_codec"), "{platform}");
        assert_eq!(
            u64::from(spec.video_long_edge),
            optional_u64(row, "video_long_edge").expect("long edge"),
            "{platform}.video_long_edge"
        );
        assert_eq!(
            spec.video_file_size_cap_bytes,
            optional_u64(row, "video_file_size_cap_bytes"),
            "{platform}.video_file_size_cap_bytes"
        );
        assert_eq!(
            spec.image_containers,
            string_list(row, "image_containers"),
            "{platform}.image_containers"
        );
        assert_eq!(
            spec.image_max_bytes,
            optional_u64(row, "image_max_bytes"),
            "{platform}.image_max_bytes"
        );
        assert_eq!(
            spec.image_aspect_min,
            optional_f64(row, "image_aspect_min"),
            "{platform}.image_aspect_min"
        );
        assert_eq!(
            spec.image_aspect_max,
            optional_f64(row, "image_aspect_max"),
            "{platform}.image_aspect_max"
        );
        assert_eq!(
            spec.publishes_video(),
            bool_field(row, "publishes_video"),
            "{platform}.publishes_video"
        );
        assert_eq!(
            spec.publishes_image(),
            bool_field(row, "publishes_image"),
            "{platform}.publishes_image"
        );
    }
    const { assert!(MAX_MEDIA_ITEMS >= 2) };
}

#[test]
fn media_transport_matches_python() {
    let document = document();
    for row in rows_exact(&document, "media_transport", 9) {
        let platform = text(row, "platform");
        let capability = DestinationCapability::built_in(platform, true, true)
            .unwrap_or_else(|| panic!("missing capability for {platform}"));
        let profile = capability.profile.as_ref().expect("built-in profile");
        assert_eq!(
            profile.media_transport.as_str(),
            text(row, "transport"),
            "{platform} transport"
        );
        match row.get("blocker").and_then(Value::as_object) {
            // A destination that fetches media itself must say so with the
            // platform's own requirement text, never a paraphrase.
            None => assert!(
                profile.media_transport_requirement.is_none(),
                "{platform} should have no requirement"
            ),
            Some(_) => {
                let requirement = profile
                    .media_transport_requirement
                    .clone()
                    .unwrap_or_else(|| panic!("{platform} must carry its requirement"));
                assert_eq!(
                    requirement,
                    text(row, "requirement"),
                    "{platform} requirement"
                );
                assert_eq!(
                    profile.media_transport_error_code.as_deref(),
                    optional_text(row, "error_code"),
                    "{platform} error code"
                );
                let blocker = row["blocker"].as_object().expect("blocker object");
                assert_eq!(blocker["code"].as_str(), Some("THREADS_NEEDS_URL"));
                assert_eq!(blocker["message"].as_str(), Some(requirement.as_str()));
            }
        }
    }
}

#[test]
fn envelope_summaries_match_python() {
    let document = document();
    for row in rows_exact(&document, "envelope_summaries", 45) {
        let platform = text(row, "platform");
        let capability = DestinationCapability::built_in(platform, true, true)
            .unwrap_or_else(|| panic!("missing capability for {platform}"));
        let profile = capability.profile.as_ref().expect("built-in profile");
        assert_eq!(
            envelope_summary(&capability, profile, optional_text(row, "content_type")),
            text(row, "text"),
            "{platform}/{:?} envelope summary",
            optional_text(row, "content_type")
        );
    }
}

#[test]
fn caption_refusals_match_python() {
    let document = document();
    for row in rows_exact(&document, "text_refusals", 30) {
        let platform = text(row, "platform");
        let length = optional_u64(row, "caption_length").expect("caption length") as usize;
        let caption = "a".repeat(length);
        let capability = DestinationCapability::built_in(platform, true, true).expect("capability");
        let reason = xpst_core::plan::caption_refusal(&capability, ContentType::Text, &caption);
        match optional_text(row, "text") {
            None => assert!(
                reason.is_none(),
                "{platform} should not refuse {length} chars"
            ),
            Some(expected) => {
                let reason = reason.unwrap_or_else(|| {
                    panic!("{platform} must refuse {length} characters");
                });
                // The refusal states both numbers, and the envelope once —
                // plus the format envelope the plan appends, exactly as
                // Python does it.
                assert!(
                    reason.message.starts_with(expected),
                    "{platform}/{length}\n  rust:     {}\n  python:   {expected}",
                    reason.message
                );
                assert_eq!(reason.code, "CAPTION_TOO_LONG");
                assert!(
                    reason.message.contains(&length.to_string()),
                    "the refusal must carry the measured length: {}",
                    reason.message
                );
            }
        }
    }
}

#[test]
fn state_codes_match_python() {
    let document = document();
    for row in rows_exact(&document, "state_codes", 14) {
        assert_eq!(
            state_for_code(text(row, "code")).as_str(),
            text(row, "state"),
            "state for {:?}",
            text(row, "code")
        );
    }
}

#[test]
fn media_kinds_match_python() {
    let document = document();
    for row in rows_exact(&document, "media_kinds", 15) {
        let path = text(row, "path");
        let expected = text(row, "kind");
        let actual = media_kind(path);
        assert_eq!(actual.as_str(), expected, "media_kind({path})");
        assert_eq!(
            actual == MediaKind::Video,
            matches!(actual, MediaKind::Video),
            "kind helper"
        );
    }
}

#[test]
fn modality_messages_match_python() {
    let document = document();
    for row in rows_exact(&document, "modality_messages", 5) {
        assert_eq!(
            modality_unsupported_message(
                text(row, "modality"),
                text(row, "suffix"),
                text(row, "destination")
            ),
            text(row, "text"),
            "modality message for {}",
            text(row, "destination")
        );
    }
    // The sentence names the destinations that *can* take the file, from the
    // global registry rather than the candidate set.
    assert!(destinations_accepting("image").contains(&"Instagram Reels"));
}

#[test]
fn envelope_refusals_match_python() {
    let document = document();
    let mut checked = 0usize;
    for row in rows_exact(&document, "envelope_refusals", 570) {
        let platform = text(row, "platform");
        let media = MediaMeasurements {
            duration_s: optional_f64(row, "duration_s"),
            size_bytes: optional_u64(row, "size_bytes"),
            width: optional_u32(row, "width"),
            height: optional_u32(row, "height"),
        };
        let mode =
            optional_text(row, "mode").map(|value| coerce_video_mode(value).expect("fixture mode"));
        let violation = video_envelope_violation(platform, &media, mode);
        match optional_text(row, "code") {
            None => assert!(
                violation.is_none(),
                "{platform} {media:?} mode={mode:?} should be accepted, got {violation:?}"
            ),
            Some(expected) => {
                let violation = violation.unwrap_or_else(|| {
                    panic!("{platform} {media:?} mode={mode:?} must be refused")
                });
                assert_eq!(violation.code, expected, "{platform}/{expected}");
                assert_eq!(
                    violation.message,
                    text(row, "message"),
                    "{platform}/{expected}"
                );
                assert_eq!(
                    violation.severity.as_str(),
                    text(row, "severity"),
                    "{platform}/{expected}"
                );
            }
        }
        checked += 1;
    }
    assert_eq!(checked, 570, "every envelope-refusal row must be checked");
}

#[test]
fn mode_decisions_match_python() {
    let document = document();
    let mut checked = 0usize;
    for row in rows_exact(&document, "mode_resolution", 36) {
        let mut request = RouteRequest::new(
            vec![MediaMeasurement {
                duration_s: optional_f64(row, "duration_s"),
                width: optional_u32(row, "width"),
                height: optional_u32(row, "height"),
                size_bytes: Some(1024),
                container: ".mp4".to_owned(),
                video_codec: Some("h264".to_owned()),
                has_audio: true,
                name: Some("clip.mp4".to_owned()),
            }],
            "hi",
        );
        request.content_type = Some("video".to_owned());
        request.requested_mode = optional_text(row, "requested_mode")
            .map(|value| coerce_video_mode(value).expect("fixture mode"));
        let plan = plan(
            &request,
            &[DestinationCapability::built_in("youtube", true, true).expect("youtube")],
        );
        let verdict = plan.destination("youtube").expect("youtube verdict");

        match optional_text(row, "code") {
            Some(expected_code) => {
                let refusal = verdict
                    .refusals
                    .first()
                    .unwrap_or_else(|| panic!("expected a mode refusal: {row}"));
                assert_eq!(refusal.code, expected_code, "{row}");
                assert_eq!(refusal.message, text(row, "message"), "{row}");
            }
            None => assert_eq!(
                verdict.mode.map(VideoMode::as_str),
                optional_text(row, "mode"),
                "{row}"
            ),
        }
        checked += 1;
    }
    assert_eq!(checked, 36, "every mode-resolution row must be checked");
}

#[test]
fn plans_match_python() {
    let document = document();
    let mut checked = 0usize;
    for row in rows_exact(&document, "plans", 341) {
        let request = request(&row["request"]);
        let destinations: Vec<DestinationCapability> = row["destinations"]
            .as_array()
            .expect("destinations array")
            .iter()
            .map(capability)
            .collect();
        let expected = &row["expected"];
        let actual = xpst_core::plan::plan(&request, &destinations);

        // The corpus records the Python engine's answers. A destination this
        // workspace has no provider for is refused here and accepted there, so
        // the comparable set is the destinations this build implements; the
        // refusal of the others is asserted below, per verdict. A destination
        // with no capability profile at all (`myspace`) is not an
        // implementation question — the corpus's UNKNOWN_PLATFORM answer stands.
        let comparable = |platform: &str| {
            xpst_core::content::provider_crate(platform).is_some()
                || xpst_core::content::content_profile(platform).is_none()
        };
        let expected_accepted: Vec<String> = string_list(expected, "accepted")
            .into_iter()
            .filter(|platform| comparable(platform))
            .collect();

        assert_eq!(
            actual.content_type.map(|item| item.as_str()),
            optional_text(expected, "content_type"),
            "content type for {row}"
        );
        assert_eq!(
            actual.ready(),
            bool_field(expected, "ready") && !expected_accepted.is_empty(),
            "ready for {row}"
        );
        assert_eq!(
            actual.accepted, expected_accepted,
            "accepted destinations for {row}"
        );
        assert_eq!(
            actual
                .request_blockers
                .iter()
                .map(|reason| (reason.code.clone(), reason.message.clone()))
                .collect::<Vec<_>>(),
            reasons(expected, "request_blockers"),
            "request blockers for {row}"
        );

        let expected_verdicts = expected["destinations"].as_array().expect("verdicts");
        assert_eq!(actual.destinations.len(), expected_verdicts.len());
        for (verdict, want) in actual.destinations.iter().zip(expected_verdicts) {
            let context = format!("{} in {row}", text(want, "platform"));
            assert_eq!(verdict.platform, text(want, "platform"), "{context}");
            if !comparable(&verdict.platform) {
                assert!(!verdict.accepted, "{context}: nothing can publish here");
                // Messenger is refused as a messaging destination before the
                // implementation question is asked, which is the sharper reason
                // and the one the corpus records; every other unimplemented
                // destination must be refused by name.
                let profile = destination_profile(&verdict.platform);
                let expected_code = if profile.is_some_and(|profile| !profile.publishing) {
                    "content_type.unsupported"
                } else {
                    xpst_core::plan::UNIMPLEMENTED_DESTINATION_CODE
                };
                assert_eq!(
                    verdict.refusals.first().map(|reason| reason.code.as_str()),
                    Some(expected_code),
                    "{context}: an unimplemented destination is refused by name"
                );
                continue;
            }
            assert_eq!(verdict.accepted, bool_field(want, "accepted"), "{context}");
            assert_eq!(verdict.state.as_str(), text(want, "state"), "{context}");
            assert_eq!(
                verdict.content_type.as_deref(),
                optional_text(want, "content_type"),
                "{context}"
            );
            assert_eq!(
                verdict.mode.map(VideoMode::as_str),
                optional_text(want, "mode"),
                "{context}"
            );
            assert_eq!(
                verdict
                    .refusals
                    .iter()
                    .map(|reason| (reason.code.clone(), reason.message.clone()))
                    .collect::<Vec<_>>(),
                reasons(want, "refusals"),
                "{context} refusals"
            );
            assert_eq!(
                verdict
                    .advisories
                    .iter()
                    .map(|reason| (reason.code.clone(), reason.message.clone()))
                    .collect::<Vec<_>>(),
                reasons(want, "advisories"),
                "{context} advisories"
            );
            let want_transforms = want["transforms"].as_array().expect("transforms");
            assert_eq!(
                verdict.transforms.len(),
                want_transforms.len(),
                "{context} transforms"
            );
            for (transform, want) in verdict.transforms.iter().zip(want_transforms) {
                assert_eq!(
                    transform.action,
                    action(text(want, "action")),
                    "{context} action"
                );
                assert_eq!(
                    transform.reasons,
                    string_list(want, "reasons"),
                    "{context} reasons"
                );
            }
        }
        checked += 1;
    }
    assert_eq!(checked, 341, "every plan row must be checked");
}

#[test]
fn python_crosscheck_rows_are_recorded() {
    // The capture script asserts agreement with the shipped planner before it
    // writes the file; this keeps the evidence in the fixture's history rather
    // than in a comment.
    let document = document();
    for row in rows_exact(&document, "python_crosscheck", 7) {
        assert_eq!(
            text(row, "state"),
            "agrees",
            "crosscheck {}",
            text(row, "label")
        );
    }
}

// ── The contract rules, stated as tests ────────────────────────────────────

fn video(duration: Option<f64>, width: u32, height: u32) -> MediaMeasurement {
    MediaMeasurement {
        duration_s: duration,
        width: Some(width),
        height: Some(height),
        size_bytes: Some(1024),
        container: ".mp4".to_owned(),
        video_codec: Some("h264".to_owned()),
        has_audio: true,
        name: Some("clip.mp4".to_owned()),
    }
}

#[test]
fn ambiguous_video_asks_instead_of_guessing() {
    // 90 s portrait is neither a Short nor long-form at YouTube, which has both
    // modes: the plan must refuse and ask, never pick one.
    let request =
        RouteRequest::new(vec![video(Some(90.0), 1080, 1920)], "hi").with_content_type("video");
    let outcome = plan(
        &request,
        &[DestinationCapability::built_in("youtube", true, true).expect("youtube")],
    );
    let verdict = outcome.destination("youtube").expect("verdict");
    assert!(!verdict.accepted);
    assert_eq!(verdict.refusals[0].code, "YOUTUBE_MODE_AMBIGUOUS");
    assert_eq!(verdict.mode, None);
    assert!(
        verdict.refusals[0].message.contains("--shorts or --long"),
        "the refusal must say what to answer: {}",
        verdict.refusals[0].message
    );

    // Stating the mode resolves it, and the stated ceiling then applies.
    let stated = request.clone().with_mode(VideoMode::Long);
    let outcome = plan(
        &stated,
        &[DestinationCapability::built_in("youtube", true, true).expect("youtube")],
    );
    let verdict = outcome.destination("youtube").expect("verdict");
    assert!(verdict.accepted, "{:?}", verdict.refusals);
    assert_eq!(verdict.mode, Some(VideoMode::Long));
}

#[test]
fn disabled_is_refused_as_disabled_never_as_unconfigured() {
    let request =
        RouteRequest::new(vec![video(Some(30.0), 1080, 1920)], "hi").with_content_type("video");
    let outcome = plan(
        &request,
        &[DestinationCapability::built_in("youtube", false, true).expect("youtube")],
    );
    let verdict = outcome.destination("youtube").expect("verdict");
    assert_eq!(verdict.state, DestinationState::SkippedUnconnected);
    assert_eq!(verdict.refusals[0].code, "PLATFORM_DISABLED");
    assert!(verdict.refusals[0].message.contains("disabled"));
    assert!(!verdict.refusals[0].message.contains("unconfigured"));

    // An enabled account whose credential is missing is attempted: advisory
    // only, never a refusal.
    let request = RouteRequest::new(Vec::new(), "hello").with_content_type("text");
    let capability = DestinationCapability::built_in("x", true, false)
        .expect("x")
        .with_auth_detail("X cookie file is missing.");
    let outcome = plan(&request, &[capability]);
    let verdict = outcome.destination("x").expect("verdict");
    assert!(verdict.accepted, "{:?}", verdict.refusals);
    assert_eq!(verdict.advisories[0].code, "AUTH_NOT_READY");
}

#[test]
fn public_url_destination_states_its_requirement_up_front() {
    // Threads is the one public-URL destination, and this workspace has no
    // provider crate for it yet, so a plan refuses it as unimplemented before
    // any transport question is asked. The requirement itself is still part of
    // the capability data and still says what the platform demands, which is
    // what a surface has to show; the refusal a plan emits is now the sharper
    // "nothing can publish here".
    let request =
        RouteRequest::new(vec![video(Some(30.0), 1080, 1920)], "hi").with_content_type("video");
    let outcome = plan(
        &request,
        &[DestinationCapability::built_in("threads", true, true).expect("threads")],
    );
    let verdict = outcome.destination("threads").expect("verdict");
    assert!(!verdict.accepted);
    assert_eq!(
        verdict.refusals[0].code,
        xpst_core::plan::UNIMPLEMENTED_DESTINATION_CODE
    );
    assert!(
        !outcome.ready(),
        "a plan whose only destination cannot be published is not ready"
    );

    let capability = DestinationCapability::built_in("threads", true, true).expect("threads");
    let profile = capability.profile.as_ref().expect("profile");
    assert_eq!(profile.media_transport, MediaTransport::PublicUrl);
    assert_eq!(
        profile.media_transport_requirement.as_deref(),
        Some(xpst_core::content::THREADS_PUBLIC_URL_REQUIREMENT)
    );
    assert_eq!(
        profile.media_transport_error_code.as_deref(),
        Some("THREADS_NEEDS_URL")
    );
    assert!(xpst_core::content::THREADS_PUBLIC_URL_REQUIREMENT.contains("public server"));
}

#[test]
fn text_post_to_a_video_only_destination_is_refused_by_name() {
    let request = RouteRequest::new(Vec::new(), "a text post").with_content_type("text");
    let outcome = plan(
        &request,
        &[DestinationCapability::built_in("youtube", true, true).expect("youtube")],
    );
    let verdict = outcome.destination("youtube").expect("verdict");
    assert_eq!(verdict.state, DestinationState::UnsupportedFormat);
    assert_eq!(verdict.refusals[0].code, "content_type.unsupported");
    let message = &verdict.refusals[0].message;
    assert!(
        message.starts_with("youtube cannot publish text posts"),
        "{message}"
    );
    assert!(message.contains("supports: video"), "{message}");
    // Refusing with the envelope is the point: the user learns the limit.
    assert!(message.contains("Envelope — youtube: video"), "{message}");
}

#[test]
fn over_limit_caption_is_refused_with_both_numbers() {
    let request = RouteRequest::new(Vec::new(), "a".repeat(281)).with_content_type("text");
    let outcome = plan(
        &request,
        &[DestinationCapability::built_in("x", true, true).expect("x")],
    );
    let verdict = outcome.destination("x").expect("verdict");
    assert!(!verdict.accepted);
    let message = &verdict.refusals[0].message;
    assert!(message.contains("accepts 280 characters"), "{message}");
    assert!(message.contains("this caption is 281"), "{message}");
    assert!(
        message.contains("refuses rather than truncates"),
        "{message}"
    );
}

/// The capture's contract, in one place.
///
/// `scripts/capture_route_plan.py` writes this file and would happily write an
/// empty array. Each parity test guards its own collection; this test guards the
/// artifact — one table of every collection and the row count it must hold — so
/// a capture that produced nothing is one loud failure rather than sixteen
/// tests that pass because they had nothing to check.
#[test]
fn every_fixture_collection_is_present_and_complete() {
    let document = document();

    rows_exact(&document, "notes", 3).iter().for_each(|row| {
        assert!(row.is_string(), "notes holds strings, not rows");
    });
    rows_exact(&document, "content_types", 5)
        .iter()
        .for_each(|row| {
            assert!(row.is_string(), "content_types holds names, not rows");
        });

    let collections: &[(&str, usize)] = &[
        ("text_limits", 6),
        ("ingest", 8),
        ("media_transport", 9),
        ("envelope_summaries", 45),
        ("text_refusals", 30),
        ("state_codes", 14),
        ("media_kinds", 15),
        ("modality_messages", 5),
        ("envelope_refusals", 570),
        ("mode_resolution", 36),
        ("plans", 341),
        ("python_crosscheck", 7),
    ];
    for (key, expected) in collections {
        let rows = rows_exact(&document, key, *expected);
        assert!(
            rows.iter().all(Value::is_object),
            "`{key}` must hold objects, not scalars"
        );
    }

    assert!(
        document["generated_from"]
            .as_str()
            .unwrap_or_default()
            .contains("route_plan"),
        "the fixture must record the shipped module it was captured from, \
         not a hand-written table"
    );
}

#[test]
fn an_unknown_destination_is_not_assumed_capable() {
    let request =
        RouteRequest::new(vec![video(Some(30.0), 1080, 1920)], "hi").with_content_type("video");
    let outcome = plan(
        &request,
        &[DestinationCapability::undeclared("myspace", true, true)],
    );
    let verdict = outcome.destination("myspace").expect("verdict");
    assert_eq!(verdict.refusals[0].code, "UNKNOWN_PLATFORM");
    assert_eq!(verdict.state, DestinationState::UnsupportedFormat);
}

#[test]
fn python_float_keeps_integral_values_distinct() {
    assert_eq!(python_float(3.0), "3.0");
    assert_eq!(python_float(0.8), "0.8");
    assert_eq!(python_float(1.91), "1.91");
    assert_eq!(python_float(1.0 / 3.0), "0.3333333333333333");
}

#[test]
fn video_envelope_lookup_is_the_registry_a_sender_reads() {
    assert!(
        video_envelope(" YouTube ").is_some(),
        "lookup is trimmed and lowercase"
    );
}
