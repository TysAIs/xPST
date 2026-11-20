//! Parity: the Rust content contract against fixtures captured from Python.
//!
//! Every row of `fixtures/content_envelopes.json` was produced by *running*
//! `src/xpst/content.py` (`scripts/capture_content_envelopes.py`), never
//! transcribed by hand, and every row is asserted here. If the Python behaviour
//! changes without a re-capture (or the Rust port drifts), this test fails with
//! the offending row — the whole point of a parity corpus.
//!
//! Regenerate the fixture with:
//!
//! ```text
//! PYTHONPATH=src python scripts/capture_content_envelopes.py
//! ```

use serde_json::Value;
use xpst_core::content::{
    coerce_content_type, content_profile, ContentType, DESTINATION_CONTENT_PROFILES,
};
use xpst_core::envelope::{
    classify_video_mode, coerce_video_mode, envelope_violation, format_bytes, format_seconds,
    video_envelope, video_envelope_violation, MediaMeasurements, Severity, VideoEnvelope,
    VideoMode, LONG_FORM_CLASSIFICATION_MIN_SECONDS, SHORTS_CLASSIFICATION_MAX_SECONDS,
    VIDEO_ENVELOPES,
};

const FIXTURE: &str = include_str!("fixtures/content_envelopes.json");

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
/// A parity test that iterates an empty collection passes without asserting
/// anything, so emptying a fixture array used to turn these tests into no-ops
/// while CI stayed green. Every loop states how many rows the capture produced;
/// a truncated fixture fails here with the collection's name instead of passing
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
        Some(Value::Null) => None,
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

fn sorted(values: &[ContentType]) -> Vec<String> {
    let mut out: Vec<String> = values.iter().map(|item| item.as_str().to_owned()).collect();
    out.sort();
    out
}

fn violation_text(violation: Option<&xpst_core::envelope::EnvelopeViolation>) -> String {
    violation.map_or_else(
        || "<accepted>".to_owned(),
        |item| format!("[{}] {}", item.code, item.message),
    )
}

#[test]
fn thresholds_match_python() {
    let document = document();
    let thresholds = &document["thresholds"];
    assert_eq!(
        thresholds["shorts_max_seconds"].as_u64(),
        Some(u64::from(SHORTS_CLASSIFICATION_MAX_SECONDS))
    );
    assert_eq!(
        thresholds["long_min_seconds"].as_u64(),
        Some(u64::from(LONG_FORM_CLASSIFICATION_MIN_SECONDS))
    );
}

#[test]
fn content_types_and_aliases_match_python() {
    let document = document();
    let fixture_types = string_list(&document, "content_types");
    let rust_types: Vec<String> = xpst_core::content::CONTENT_TYPES
        .iter()
        .map(|item| item.as_str().to_owned())
        .collect();
    assert_eq!(fixture_types, rust_types);

    for row in rows_exact(&document, "aliases", 20) {
        let label = text(row, "label");
        let expected = text(row, "content_type");
        let resolved = coerce_content_type(label)
            .unwrap_or_else(|error| panic!("alias {label:?} rejected by Rust: {error}"));
        assert_eq!(resolved.as_str(), expected, "alias {label:?}");
    }
}

#[test]
fn every_envelope_field_matches_python() {
    let document = document();
    let fixture = rows_exact(&document, "envelopes", 7);
    let rust: Vec<&'static str> = VIDEO_ENVELOPES.iter().map(|item| item.platform).collect();
    let fixture_platforms: Vec<&str> = fixture.iter().map(|row| text(row, "platform")).collect();
    assert_eq!(
        fixture_platforms, rust,
        "the registry must cover exactly the platforms Python declares"
    );

    for row in fixture {
        let platform = text(row, "platform");
        let envelope: &VideoEnvelope = video_envelope(platform)
            .unwrap_or_else(|| panic!("missing Rust envelope for {platform}"));
        assert_eq!(
            envelope.display_name,
            text(row, "display_name"),
            "{platform}"
        );
        assert_eq!(
            envelope.max_duration_seconds.map(u64::from),
            optional_u64(row, "max_duration_seconds"),
            "{platform}.max_duration_seconds"
        );
        assert_eq!(
            envelope.shorts_max_duration_seconds.map(u64::from),
            optional_u64(row, "shorts_max_duration_seconds"),
            "{platform}.shorts_max_duration_seconds"
        );
        assert_eq!(
            envelope.max_bytes,
            optional_u64(row, "max_bytes"),
            "{platform}.max_bytes"
        );
        assert_eq!(
            envelope.containers,
            string_list(row, "containers"),
            "{platform}.containers"
        );
        assert_eq!(
            envelope.video_codec,
            text(row, "video_codec"),
            "{platform}.video_codec"
        );
        assert_eq!(
            envelope.aspect_min,
            optional_f64(row, "aspect_min"),
            "{platform}.aspect_min"
        );
        assert_eq!(
            envelope.aspect_max,
            optional_f64(row, "aspect_max"),
            "{platform}.aspect_max"
        );
        assert_eq!(
            envelope.shorts_max_aspect_ratio,
            optional_f64(row, "shorts_max_aspect_ratio"),
            "{platform}.shorts_max_aspect_ratio"
        );
        let rust_modes: Vec<&str> = envelope.modes.iter().map(|mode| mode.as_str()).collect();
        assert_eq!(rust_modes, string_list(row, "modes"), "{platform}.modes");
        assert_eq!(
            envelope.long_form,
            row["long_form"].as_bool().expect("long_form is a bool"),
            "{platform}.long_form"
        );
        assert_eq!(envelope.source, text(row, "source"), "{platform}.source");
        assert_eq!(envelope.note, text(row, "note"), "{platform}.note");
        assert_eq!(
            envelope
                .duration_cap(Some(VideoMode::Shorts))
                .map(u64::from),
            optional_u64(row, "duration_cap_shorts"),
            "{platform}.duration_cap(shorts)"
        );
        assert_eq!(
            envelope.duration_cap(Some(VideoMode::Long)).map(u64::from),
            optional_u64(row, "duration_cap_long"),
            "{platform}.duration_cap(long)"
        );
    }
}

#[test]
fn capability_rows_match_python() {
    let document = document();
    let fixture = rows_exact(&document, "capabilities", 9);
    let rust: Vec<&'static str> = DESTINATION_CONTENT_PROFILES
        .iter()
        .map(|item| item.platform)
        .collect();
    let fixture_platforms: Vec<&str> = fixture.iter().map(|row| text(row, "platform")).collect();
    assert_eq!(fixture_platforms, rust, "capability table coverage");

    for row in fixture {
        let platform = text(row, "platform");
        let profile = content_profile(platform)
            .unwrap_or_else(|| panic!("missing Rust capability profile for {platform}"));
        assert_eq!(
            profile.display_name,
            text(row, "display_name"),
            "{platform}"
        );
        assert_eq!(
            profile.publishing,
            row["publishing"].as_bool().expect("publishing is a bool"),
            "{platform}.publishing"
        );
        assert_eq!(
            profile.media_transport.as_str(),
            text(row, "media_transport"),
            "{platform}.media_transport"
        );
        assert_eq!(
            sorted(profile.declared),
            string_list(row, "declared"),
            "{platform}.declared"
        );
        // The fixture records what the *Python* engine implements. This table
        // records what *this workspace* can publish. They are the same claim
        // only where a provider crate exists here; where none does, the honest
        // Rust answer is "nothing", and the gap is asserted rather than hidden.
        let rust_implemented = sorted(profile.implemented);
        if xpst_core::content::provider_crate(platform).is_some() {
            assert_eq!(
                rust_implemented,
                string_list(row, "implemented"),
                "{platform}.implemented"
            );
        } else {
            assert!(
                rust_implemented.is_empty(),
                "{platform} has no provider crate in this workspace, so it cannot list \
                 implemented content types: {rust_implemented:?}"
            );
            assert!(
                !string_list(row, "implemented").is_empty(),
                "{platform} is declared-but-unimplemented here; the fixture should still record \
                 Python's implementation, which is what makes the gap visible"
            );
        }
        assert_eq!(
            profile.video_envelope().is_some(),
            row["has_video_envelope"]
                .as_bool()
                .expect("has_video_envelope is a bool"),
            "{platform}.has_video_envelope"
        );
    }
}

#[test]
fn mode_coercion_matches_python() {
    let document = document();
    for row in rows_exact(&document, "mode_coercion", 6) {
        let input = text(row, "input");
        match optional_text(row, "error") {
            None => {
                let expected = text(row, "mode");
                let resolved = coerce_video_mode(input)
                    .unwrap_or_else(|error| panic!("Rust rejected {input:?}: {error}"));
                assert_eq!(resolved.as_str(), expected, "mode {input:?}");
            }
            Some(expected_error) => {
                let error = coerce_video_mode(input)
                    .expect_err(&format!("Rust accepted invalid mode {input:?}"));
                assert_eq!(
                    error.to_string(),
                    expected_error,
                    "mode {input:?} error text"
                );
            }
        }
    }
}

#[test]
fn classification_grid_matches_python() {
    let document = document();
    let mut checked = 0usize;
    for row in rows_exact(&document, "classify", 135) {
        let duration = optional_f64(row, "duration_s");
        let width = optional_u64(row, "width").map(|value| u32::try_from(value).unwrap());
        let height = optional_u64(row, "height").map(|value| u32::try_from(value).unwrap());
        let expected = optional_text(row, "mode");
        let actual = classify_video_mode(duration, width, height);
        assert_eq!(
            actual.map(VideoMode::as_str),
            expected,
            "classify_video_mode({duration:?}, {width:?}, {height:?}) must match Python"
        );
        checked += 1;
    }
    assert_eq!(checked, 135, "every classification row must be checked");
}

#[test]
fn violation_grid_matches_python() {
    let document = document();
    let mut checked = 0usize;
    for row in rows_exact(&document, "violations", 242) {
        let platform = text(row, "platform");
        let media = MediaMeasurements {
            duration_s: optional_f64(row, "duration_s"),
            size_bytes: optional_u64(row, "size_bytes"),
            width: optional_u64(row, "width").map(|value| u32::try_from(value).unwrap()),
            height: optional_u64(row, "height").map(|value| u32::try_from(value).unwrap()),
        };
        let mode = optional_text(row, "mode").map(|value| {
            coerce_video_mode(value)
                .unwrap_or_else(|error| panic!("fixture mode {value:?}: {error}"))
        });
        let actual = video_envelope_violation(platform, &media, mode);
        let expected_code = optional_text(row, "code");
        let actual_code = actual.as_ref().map(|item| item.code);
        assert_eq!(
            actual_code,
            expected_code,
            "{}: {} must match Python ({})",
            platform,
            format_args!("{media:?} mode={mode:?}"),
            violation_text(actual.as_ref())
        );
        if let Some(expected_code) = expected_code {
            let violation = actual.as_ref().expect("a code implies a violation");
            assert_eq!(
                violation.message,
                text(row, "message"),
                "{platform}/{expected_code}"
            );
            assert_eq!(
                violation.severity.as_str(),
                text(row, "severity"),
                "{platform}/{expected_code} severity"
            );
            assert_eq!(
                violation.platform,
                platform.trim().to_ascii_lowercase(),
                "{platform}/{expected_code} platform"
            );
            assert!(violation.is_error());
        }
        checked += 1;
    }
    assert_eq!(checked, 242, "every violation row must be checked");
}

#[test]
fn envelope_override_speaks_the_same_vocabulary() {
    // The account-specific path (an X Premium ceiling) adjusts the envelope and
    // is still judged by the same function.
    let mut envelope = *video_envelope("x").expect("x has an envelope");
    envelope.max_duration_seconds = Some(600);
    let fit = MediaMeasurements {
        duration_s: Some(500.0),
        ..MediaMeasurements::default()
    };
    assert!(envelope_violation(Some(&envelope), &fit, None).is_none());
    let over = MediaMeasurements {
        duration_s: Some(601.0),
        ..MediaMeasurements::default()
    };
    let violation =
        envelope_violation(Some(&envelope), &over, None).expect("601 s exceeds the 600 s ceiling");
    assert!(violation.message.contains("600 s"), "{}", violation.message);
    assert_eq!(violation.severity, Severity::Error);
}

#[test]
fn ambiguous_video_is_never_guessed() {
    assert_eq!(
        classify_video_mode(Some(45.0), Some(1080), Some(1920)),
        Some(VideoMode::Shorts)
    );
    assert_eq!(
        classify_video_mode(Some(45.0), Some(1920), Some(1080)),
        Some(VideoMode::Long)
    );
    assert_eq!(
        classify_video_mode(Some(90.0), Some(1080), Some(1920)),
        None
    );
    assert_eq!(classify_video_mode(None, Some(1080), Some(1920)), None);
}

#[test]
fn formatters_match_python() {
    let document = document();
    let formatting = &document["formatting"];
    for row in rows_exact(formatting, "seconds", 14) {
        let value = optional_f64(row, "value");
        assert_eq!(
            format_seconds(value),
            text(row, "text"),
            "format_seconds({value:?})"
        );
    }
    for row in rows_exact(formatting, "bytes", 9) {
        let value = optional_u64(row, "value");
        assert_eq!(
            format_bytes(value),
            text(row, "text"),
            "format_bytes({value:?})"
        );
    }
}

/// The capture's contract, in one place.
///
/// `scripts/capture_content_envelopes.py` writes this file, and the script is
/// not allowed to fail: it would happily write an empty array. Each parity test
/// guards its own collection, and this test guards the whole artifact — one
/// list of every collection and the row count it must hold, so a capture that
/// produced nothing is one loud failure with the collection's name on it rather
/// than sixteen tests that pass because they had nothing to check.
#[test]
fn every_fixture_collection_is_present_and_complete() {
    let document = document();

    rows_exact(&document, "content_types", 5)
        .iter()
        .for_each(|row| {
            assert!(row.is_string(), "content_types holds names, not rows");
        });

    let object_fields: &[(&str, usize)] = &[
        ("envelopes", 7),
        ("capabilities", 9),
        ("aliases", 20),
        ("mode_coercion", 6),
        ("classify", 135),
        ("violations", 242),
    ];
    for (key, expected) in object_fields {
        let rows = rows_exact(&document, key, *expected);
        assert!(
            rows.iter().all(Value::is_object),
            "`{key}` must hold objects, not scalars"
        );
    }

    let formatting = &document["formatting"];
    rows_exact(formatting, "seconds", 14);
    rows_exact(formatting, "bytes", 9);

    assert!(
        document["generated_from"]
            .as_str()
            .unwrap_or_default()
            .contains("xpst/content.py"),
        "the fixture must record the shipped module it was captured from, \
         not a hand-written table"
    );
}
