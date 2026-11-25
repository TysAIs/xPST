//! Row-by-row parity for the tables `preflight` is assembled from.
//!
//! The fixture is captured by running the shipped Python
//! (`xtask/scripts/capture_preflight_tables.py`), never hand-written: the
//! capability profiles, the ingest matrix, the visibility registry, the
//! destination-state mapping and the refusal sentences all come from
//! `xpst.content`, `xpst.media.specs`, `xpst.visibility` and
//! `xpst.services.route_plan`.
//!
//! A parity test nobody has seen fail is decoration, so this one is proven by
//! tampering: change a number in the fixture and the row fails; empty the
//! destination array and the count guard fails.

use serde_json::Value;
use xpst_core::content::{content_profile, text_limit, ContentType, PUBLISH_DESTINATIONS};
use xpst_core::ingest::ingest_spec;
use xpst_core::preflight::{
    self, destination_envelope, envelope_summary, media_transport_error_code, python_float,
    state_string, text_refusal, Envelope, Number,
};
use xpst_core::visibility;

const FIXTURE: &str = include_str!("fixtures/preflight_tables.json");

fn document() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture is valid JSON")
}

fn content_types() -> Vec<String> {
    document()["content_types"]
        .as_array()
        .expect("content_types")
        .iter()
        .map(|item| item.as_str().expect("content type").to_string())
        .collect()
}

#[test]
fn the_publish_destination_order_is_the_shipped_one() {
    let expected: Vec<String> = document()["publish_destinations"]
        .as_array()
        .expect("publish_destinations")
        .iter()
        .map(|item| item.as_str().expect("platform").to_string())
        .collect();
    assert_eq!(
        expected.len(),
        PUBLISH_DESTINATIONS.len(),
        "the fixture and the table disagree on how many destinations publish"
    );
    assert_eq!(
        expected,
        PUBLISH_DESTINATIONS
            .iter()
            .map(|item| (*item).to_string())
            .collect::<Vec<_>>()
    );
}

#[test]
fn every_destination_profile_matches_the_captured_one() {
    let rows = document()["destinations"]
        .as_array()
        .expect("destinations")
        .clone();
    assert_eq!(rows.len(), 8, "the fixture lost a destination row");
    for row in rows {
        let platform = row["platform"].as_str().expect("platform");
        let profile = content_profile(platform).expect("profile");
        assert_eq!(profile.display_name, row["display_name"].as_str().unwrap());
        assert_eq!(profile.publishing, row["publishing"].as_bool().unwrap());
        assert_eq!(
            profile.media_transport.as_str(),
            row["media_transport"].as_str().unwrap()
        );
        assert_eq!(
            media_transport_error_code(platform).map(str::to_string),
            row["media_transport_error_code"]
                .as_str()
                .map(str::to_string)
        );
        assert_eq!(
            text_limit(platform),
            row["text_limit"].as_u64().map(|value| value as usize)
        );

        let mut declared: Vec<&str> = profile.declared.iter().map(|item| item.as_str()).collect();
        declared.sort_unstable();
        let expected: Vec<&str> = row["declared"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item.as_str().unwrap())
            .collect();
        assert_eq!(declared, expected, "{platform} declared set drifted");

        let mut implemented: Vec<&str> = profile
            .python_implemented
            .iter()
            .map(|item| item.as_str())
            .collect();
        implemented.sort_unstable();
        let expected: Vec<&str> = row["implemented"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item.as_str().unwrap())
            .collect();
        assert_eq!(
            implemented, expected,
            "{platform} python_implemented set drifted"
        );
    }
}

#[test]
fn every_envelope_document_matches_the_captured_one() {
    for row in document()["destinations"].as_array().unwrap() {
        let platform = row["platform"].as_str().unwrap();
        let expected = &row["envelope"];
        let actual = destination_envelope(platform).expect("envelope");
        assert_eq!(
            envelope_json(&actual),
            *expected,
            "{platform} envelope document drifted"
        );
    }
}

#[test]
fn every_envelope_summary_matches_the_captured_one() {
    for row in document()["destinations"].as_array().unwrap() {
        let platform = row["platform"].as_str().unwrap();
        for content_type in content_types() {
            let expected = row["envelope_summaries"][&content_type]
                .as_str()
                .expect("summary");
            assert_eq!(
                envelope_summary(platform, Some(&content_type)),
                expected,
                "{platform} / {content_type} envelope summary drifted"
            );
        }
        assert_eq!(
            envelope_summary(platform, None),
            row["envelope_summary_unknown"].as_str().unwrap()
        );
    }
}

#[test]
fn every_ingest_spec_matches_the_captured_one() {
    let rows = document()["ingest"].as_array().expect("ingest").clone();
    assert_eq!(rows.len(), 8, "the fixture lost an ingest row");
    for row in rows {
        let platform = row["platform"].as_str().unwrap();
        let spec = ingest_spec(platform).expect("ingest spec");
        assert_eq!(spec.display_name, row["display_name"].as_str().unwrap());
        assert_eq!(
            spec.video_containers,
            row["containers"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| item.as_str().unwrap())
                .collect::<Vec<_>>()
        );
        assert_eq!(spec.video_codec, row["video_codec"].as_str().unwrap());
        assert_eq!(spec.pix_fmt, row["pix_fmt"].as_str().unwrap());
        assert_eq!(
            spec.video_long_edge as u64,
            row["long_edge"].as_u64().unwrap()
        );
        assert_eq!(spec.fps_cap as u64, row["fps_cap"].as_u64().unwrap());
        assert_eq!(spec.audio_codec, row["audio_codec"].as_str().unwrap());
        assert_eq!(spec.audio_rate as u64, row["audio_rate"].as_u64().unwrap());
        assert_eq!(spec.lufs, row["lufs"].as_f64().unwrap());
        assert_eq!(
            spec.max_video_bitrate_bps,
            row["max_video_bitrate_bps"].as_u64().unwrap()
        );
        assert_eq!(spec.file_size_cap_mb, row["file_size_cap_mb"].as_u64());
        assert_eq!(
            spec.duration_cap_s.map(u64::from),
            row["duration_cap_s"].as_u64()
        );
        assert_eq!(
            spec.image_containers,
            row["image_containers"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| item.as_str().unwrap())
                .collect::<Vec<_>>()
        );
        // The fixture records the cap in whole megabytes (Python's
        // `image_file_size_cap_mb`); the table stores bytes, so the comparison
        // is on the same number in the same unit.
        let expected_bytes = row["image_file_size_cap_mb"]
            .as_f64()
            .map(|megabytes| (megabytes * 1024.0 * 1024.0) as u64);
        assert_eq!(spec.image_max_bytes, expected_bytes);
        assert_eq!(spec.image_aspect_min, row["image_aspect_min"].as_f64());
        assert_eq!(spec.image_aspect_max, row["image_aspect_max"].as_f64());
    }
}

#[test]
fn the_visibility_registry_matches_the_captured_one() {
    let rows = document()["visibility_routes"]
        .as_array()
        .expect("visibility_routes")
        .clone();
    assert_eq!(
        rows.len(),
        visibility::VISIBILITY_SUPPORT.len(),
        "the fixture and the registry disagree on how many routes exist"
    );
    for row in rows {
        let platform = row["platform"].as_str().unwrap();
        let route = row["route"].as_str().unwrap();
        let support = visibility::support(platform, route).expect("registry row");
        assert_eq!(support.policy.as_str(), row["policy"].as_str().unwrap());
        assert_eq!(support.note, row["note"].as_str().unwrap());
        assert_eq!(support.default_level, row["default_level"].as_str());
        let levels: Vec<(String, String)> = row["levels"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(key, value)| (key.clone(), value.as_str().unwrap().to_string()))
            .collect();
        let mut actual: Vec<(String, String)> = support
            .levels
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect();
        let mut expected = levels;
        actual.sort();
        expected.sort();
        assert_eq!(actual, expected, "{platform}/{route} levels drifted");
        assert_eq!(
            support.requestable(),
            row["requestable"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| item.as_str().unwrap())
                .collect::<Vec<_>>()
        );
    }
}

#[test]
fn every_visibility_verdict_matches_the_captured_one() {
    let rows = document()["visibility_plan_entries"]
        .as_array()
        .expect("visibility_plan_entries")
        .clone();
    assert!(!rows.is_empty(), "the fixture lost every visibility row");
    for row in rows {
        let platform = row["platform"].as_str().unwrap();
        let route = row["route"].as_str().unwrap();
        let requested = row["requested"].as_str();
        assert_eq!(
            visibility::refusal(platform, requested, route),
            row["refusal"].as_str().map(str::to_string),
            "{platform}/{route}/{requested:?} refusal drifted"
        );
        assert_eq!(
            visibility::advisory(platform, requested, route),
            row["advisory"].as_str().map(str::to_string),
            "{platform}/{route}/{requested:?} advisory drifted"
        );
        let entry = visibility::plan_entry(platform, requested, route, None, None);
        match (entry, row["entry"].as_object()) {
            (None, None) => {}
            (Some(entry), Some(expected)) => {
                assert_eq!(entry.requested, expected["requested"].as_str().unwrap());
                assert_eq!(entry.level.as_deref(), expected["level"].as_str());
                assert_eq!(entry.honoured, expected["honoured"].as_bool().unwrap());
                assert_eq!(entry.route, expected["route"].as_str().unwrap());
                assert_eq!(entry.enforced, expected["enforced"].as_str().unwrap());
                assert_eq!(entry.note.as_deref(), expected["note"].as_str());
            }
            (entry, expected) => panic!(
                "{platform}/{route}/{requested:?}: plan_entry {entry:?} vs fixture {expected:?}"
            ),
        }
    }
}

#[test]
fn every_state_code_maps_to_the_captured_state() {
    let document = document();
    let rows = document["state_for_code"].as_object().expect("states");
    assert!(rows.len() >= 18, "the fixture lost state rows");
    for (code, expected) in rows {
        assert_eq!(
            state_string(code),
            expected.as_str().unwrap(),
            "{code} state drifted"
        );
    }
}

#[test]
fn every_text_refusal_matches_the_captured_one() {
    let rows = document()["text_refusals"]
        .as_array()
        .expect("text_refusals")
        .clone();
    assert!(!rows.is_empty(), "the fixture lost every text refusal");
    for row in rows {
        let platform = row["platform"].as_str().unwrap();
        let length = row["length"].as_u64().unwrap() as usize;
        let caption = "a".repeat(length);
        assert_eq!(
            text_refusal(platform, &caption),
            row["refusal"].as_str().map(str::to_string),
            "{platform} / {length} characters drifted"
        );
    }
}

#[test]
fn the_shared_refusal_sentences_match_the_captured_ones() {
    let document = document();
    assert_eq!(
        preflight::NO_DESTINATIONS_CODE,
        document["no_destinations"]["code"].as_str().unwrap()
    );
    assert_eq!(
        preflight::NO_DESTINATIONS_MESSAGE,
        document["no_destinations"]["message"].as_str().unwrap()
    );
    assert_eq!(
        preflight::NO_CONNECTED_ACCOUNTS_CODE,
        document["no_connected_accounts"]["code"].as_str().unwrap()
    );
    assert_eq!(
        preflight::no_connected_accounts_message(),
        document["no_connected_accounts"]["message"]
            .as_str()
            .unwrap()
    );
    assert_eq!(
        preflight::NO_ROUTE_FOR_FORMAT_CODE,
        document["no_route_for_format"]["code"].as_str().unwrap()
    );
    assert_eq!(
        preflight::no_route_for_format_message(
            "image",
            &["youtube".to_string(), "tiktok".to_string()]
        ),
        document["no_route_for_format"]["message"].as_str().unwrap()
    );
    assert_eq!(
        preflight::connect_command(Some("youtube")),
        document["connect_command"]["platform"].as_str().unwrap()
    );
    assert_eq!(
        preflight::connect_command(None),
        document["connect_command"]["generic"].as_str().unwrap()
    );
}

/// The envelope document as the payload renders it, so the comparison is on the
/// same shape the fixture captured.
fn envelope_json(envelope: &Envelope) -> Value {
    let mut document = serde_json::Map::new();
    document.insert(
        "platform".to_string(),
        Value::from(envelope.platform.clone()),
    );
    document.insert(
        "display_name".to_string(),
        Value::from(envelope.display_name.clone()),
    );
    document.insert("publishing".to_string(), Value::from(envelope.publishing));
    document.insert(
        "content_types".to_string(),
        serde_json::json!({
            "implemented": envelope.implemented,
            "declared": envelope.declared,
        }),
    );
    document.insert(
        "caption".to_string(),
        serde_json::json!({ "max_characters": envelope.caption_max_characters }),
    );
    document.insert(
        "media_transport".to_string(),
        Value::from(envelope.media_transport),
    );
    document.insert(
        "media_transport_requirement".to_string(),
        envelope
            .media_transport_requirement
            .clone()
            .map_or(Value::Null, Value::from),
    );
    document.insert(
        "video".to_string(),
        envelope.video.as_ref().map_or(Value::Null, |video| {
            serde_json::json!({
                "max_duration_seconds": video.max_duration_seconds,
                "containers": video.containers,
                "video_codec": video.video_codec,
                "pix_fmt": video.pix_fmt,
                "max_bytes": video.max_bytes,
                "max_long_edge": video.max_long_edge,
                "fps_cap": video.fps_cap,
                "audio_codec": video.audio_codec,
            })
        }),
    );
    document.insert(
        "image".to_string(),
        envelope.image.as_ref().map_or(Value::Null, |image| {
            serde_json::json!({
                "containers": image.containers,
                "max_bytes": image.max_bytes.map(|value| match value {
                    Number::Int(value) => Value::from(value),
                    Number::Float(value) => Value::from(value),
                }),
                "aspect_min": image.aspect_min,
                "aspect_max": image.aspect_max,
            })
        }),
    );
    Value::Object(document)
}

#[test]
fn python_float_keeps_the_integral_point() {
    assert_eq!(python_float(3.0), "3.0");
    assert_eq!(python_float(0.8), "0.8");
    assert_eq!(python_float(1.0 / 3.0), "0.3333333333333333");
}

#[test]
fn the_content_type_vocabulary_is_the_captured_one() {
    let expected = content_types();
    let actual: Vec<String> = [
        ContentType::Video,
        ContentType::Image,
        ContentType::Carousel,
        ContentType::Text,
        ContentType::Thread,
    ]
    .iter()
    .map(|item| item.as_str().to_string())
    .collect();
    assert_eq!(actual, expected);
}
