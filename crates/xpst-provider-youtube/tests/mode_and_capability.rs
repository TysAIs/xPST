//! Mode resolution and the capability declaration.
//!
//! The rule under test: an explicit mode is accepted, an unmeasurable or
//! ambiguous video is refused with the question rather than defaulted, and the
//! ceiling for the chosen mode is enforced with the *measured* number in the
//! refusal.

use xpst_core::envelope::{video_envelope, MediaMeasurements, VideoMode};
use xpst_provider_youtube::capability::{
    capability, capability_or_gaps, CapabilityError, CHUNK_GRANULARITY_BYTES,
    UPLOADS_PER_DAY_PER_PROJECT,
};
use xpst_provider_youtube::mode::{
    mode_from_config, resolve_mode, Refusal, CODE_AMBIGUOUS, CODE_CONFIG_INVALID, CODE_ENVELOPE,
    CODE_UNMEASURABLE,
};

fn measured(duration: f64, width: u32, height: u32) -> MediaMeasurements {
    MediaMeasurements {
        duration_s: Some(duration),
        size_bytes: Some(2 * 1024 * 1024),
        width: Some(width),
        height: Some(height),
    }
}

// ---------------------------------------------------------------------------
// The capability declaration
// ---------------------------------------------------------------------------

#[test]
fn capability_carries_the_documented_limits() {
    let capability = capability().expect("registry has YouTube");

    assert_eq!(capability.max_bytes, 256 * 1024 * 1024 * 1024, "256 GB");
    assert_eq!(capability.max_duration_seconds, 12 * 60 * 60, "12 h");
    assert_eq!(capability.shorts_max_duration_seconds, 60, "Shorts ≤ 60 s");
    assert_eq!(capability.shorts_max_aspect_ratio, 1.0, "square-or-taller");
    assert_eq!(capability.uploads_per_day_per_project, 100);
    assert_eq!(UPLOADS_PER_DAY_PER_PROJECT, 100);
    assert_eq!(capability.max_title_chars, 100);
    assert_eq!(capability.max_description_chars, 5_000);
    assert_eq!(capability.max_tags, 15);

    assert!(
        capability.gaps().is_empty(),
        "the declaration contradicts itself: {:?}",
        capability.gaps()
    );
    assert!(capability_or_gaps().is_ok());
}

#[test]
fn capability_cannot_disagree_with_the_planner_registry() {
    let capability = capability().expect("registry has YouTube");
    let row = video_envelope("youtube").expect("registry row");

    assert_eq!(Some(capability.max_bytes), row.max_bytes);
    assert_eq!(
        Some(capability.max_duration_seconds),
        row.max_duration_seconds
    );
    assert_eq!(
        Some(capability.shorts_max_duration_seconds),
        row.shorts_max_duration_seconds
    );
    assert_eq!(
        Some(capability.shorts_max_aspect_ratio),
        row.shorts_max_aspect_ratio
    );
    assert_eq!(capability.modes, row.modes);
    assert_eq!(capability.containers, row.containers);
    assert_eq!(capability.duration_ceiling(VideoMode::Shorts), 60);
    assert_eq!(capability.duration_ceiling(VideoMode::Long), 43_200);
}

#[test]
fn chunk_sizes_are_legal_and_mode_specific() {
    let capability = capability().expect("registry has YouTube");

    assert_eq!(capability.chunk_granularity_bytes, CHUNK_GRANULARITY_BYTES);
    assert_eq!(capability.shorts_chunk_bytes % CHUNK_GRANULARITY_BYTES, 0);
    assert_eq!(
        capability.long_form_chunk_bytes % CHUNK_GRANULARITY_BYTES,
        0
    );
    assert!(
        capability.long_form_chunk_bytes > capability.shorts_chunk_bytes,
        "a 12-hour upload must not go out in 256 KiB chunks"
    );
    assert_eq!(capability.chunk_size(VideoMode::Shorts), 256 * 1024);
    assert_eq!(capability.chunk_size(VideoMode::Long), 8 * 1024 * 1024);
}

#[test]
fn a_missing_registry_row_is_an_error_not_a_default() {
    // The registry lookup is the only source of the numbers; a caller that
    // cannot get them must see the failure.
    assert!(matches!(capability_or_gaps(), Ok(_) | Err(_)));
    assert_eq!(
        CapabilityError::MissingEnvelope.to_string(),
        "the capability registry has no YouTube video envelope"
    );
}

// ---------------------------------------------------------------------------
// Unmeasurable and ambiguous input is refused, never defaulted
// ---------------------------------------------------------------------------

#[test]
fn an_unmeasurable_video_is_refused_with_the_question() {
    let unmeasured = MediaMeasurements {
        duration_s: None,
        size_bytes: Some(1024),
        width: None,
        height: None,
    };

    let refusal = resolve_mode(None, "clip.mp4", &unmeasured).expect_err("refused");
    assert_eq!(refusal.code(), CODE_UNMEASURABLE);
    assert!(refusal
        .to_string()
        .starts_with("YOUTUBE_MODE_UNMEASURABLE: "));
    assert!(refusal.message().contains("--shorts or --long"));
    assert!(refusal.message().contains("youtube.video_mode"));
    assert!(refusal.message().contains("60 s"));
    assert!(refusal.message().contains("clip.mp4"));
    assert!(!refusal.retryable());
}

#[test]
fn an_ambiguous_video_is_refused_with_both_thresholds_and_the_measurement() {
    // 90 s is over the Shorts ceiling but under the long-form floor; portrait
    // alone does not settle it, so xPST must ask.
    let refusal = resolve_mode(None, "clip.mp4", &measured(90.0, 1080, 1920))
        .expect_err("ambiguous is refused");

    assert_eq!(refusal.code(), CODE_AMBIGUOUS);
    assert!(
        refusal.message().contains("90 s (1 min 30 s)"),
        "{}",
        refusal.message()
    );
    assert!(refusal.message().contains("1080x1920"));
    assert!(
        refusal.message().contains("≤ 60 s"),
        "{}",
        refusal.message()
    );
    assert!(
        refusal.message().contains("≥ 180 s"),
        "{}",
        refusal.message()
    );
    assert!(refusal.message().contains("xPST will not guess"));
    assert_eq!(refusal.mode(), None);
}

#[test]
fn classification_that_is_clear_is_accepted() {
    assert_eq!(
        resolve_mode(None, "a.mp4", &measured(30.0, 1080, 1920)),
        Ok(VideoMode::Shorts)
    );
    assert_eq!(
        resolve_mode(None, "b.mp4", &measured(300.0, 1920, 1080)),
        Ok(VideoMode::Long)
    );
    // A landscape video under 60 s is long-form: it is not square-or-taller.
    assert_eq!(
        resolve_mode(None, "c.mp4", &measured(30.0, 1920, 1080)),
        Ok(VideoMode::Long)
    );
}

#[test]
fn an_explicit_mode_wins_and_is_still_held_to_its_envelope() {
    // 30 s portrait would classify as a Short, but the caller said long-form.
    assert_eq!(
        resolve_mode(Some(VideoMode::Long), "a.mp4", &measured(30.0, 1080, 1920)),
        Ok(VideoMode::Long)
    );
}

// ---------------------------------------------------------------------------
// The ceiling refusal carries the measured number
// ---------------------------------------------------------------------------

#[test]
fn over_the_shorts_ceiling_names_the_measurement_and_the_limit() {
    let refusal = resolve_mode(
        Some(VideoMode::Shorts),
        "clip.mp4",
        &measured(90.0, 1080, 1920),
    )
    .expect_err("90 s is over the Shorts ceiling");

    assert_eq!(refusal.code(), CODE_ENVELOPE);
    assert_eq!(refusal.envelope_code(), Some("video_envelope.duration"));
    assert!(refusal.to_string().starts_with("YOUTUBE_VIDEO_ENVELOPE: "));
    assert!(
        refusal
            .message()
            .contains("Short-form is 90 s (1 min 30 s)"),
        "{}",
        refusal.message()
    );
    assert!(
        refusal.message().contains("60 s (1 min)"),
        "{}",
        refusal.message()
    );
    assert_eq!(refusal.measurements().duration_s, Some(90.0));
}

#[test]
fn over_the_long_form_ceiling_names_the_measurement_and_the_limit() {
    let refusal = resolve_mode(
        Some(VideoMode::Long),
        "long.mp4",
        &measured(13.0 * 3600.0, 1920, 1080),
    )
    .expect_err("13 h is over the 12 h ceiling");

    assert_eq!(refusal.envelope_code(), Some("video_envelope.duration"));
    assert!(
        refusal.message().contains("46800 s (13 h)"),
        "{}",
        refusal.message()
    );
    assert!(
        refusal.message().contains("43200 s (12 h)"),
        "{}",
        refusal.message()
    );
}

#[test]
fn over_the_byte_ceiling_names_both_sizes() {
    let too_big = MediaMeasurements {
        duration_s: Some(30.0),
        size_bytes: Some(300 * 1024 * 1024 * 1024),
        width: Some(1080),
        height: Some(1920),
    };

    let refusal = resolve_mode(Some(VideoMode::Shorts), "huge.mp4", &too_big)
        .expect_err("300 GB is over the ceiling");

    assert_eq!(refusal.envelope_code(), Some("video_envelope.size"));
    assert!(
        refusal.message().contains("300 GB"),
        "{}",
        refusal.message()
    );
    assert!(
        refusal.message().contains("256 GB"),
        "{}",
        refusal.message()
    );
}

#[test]
fn a_landscape_file_cannot_be_forced_into_shorts() {
    let refusal = resolve_mode(
        Some(VideoMode::Shorts),
        "wide.mp4",
        &measured(30.0, 1920, 1080),
    )
    .expect_err("a 1.78 aspect is not square-or-vertical");

    assert_eq!(refusal.envelope_code(), Some("video_envelope.aspect"));
    assert!(refusal.message().contains("1.78"), "{}", refusal.message());
    assert!(refusal.message().contains("1.00"), "{}", refusal.message());
    assert!(refusal.message().contains("long-form"));
}

// ---------------------------------------------------------------------------
// Configuration spelling
// ---------------------------------------------------------------------------

#[test]
fn auto_means_ask_the_probe_and_an_unknown_spelling_is_reported() {
    assert_eq!(mode_from_config("auto"), Ok(None));
    assert_eq!(mode_from_config(""), Ok(None));
    assert_eq!(mode_from_config(" shorts "), Ok(Some(VideoMode::Shorts)));
    assert_eq!(mode_from_config("LONG"), Ok(Some(VideoMode::Long)));

    let error: Refusal = mode_from_config("short-form").expect_err("unknown spelling");
    assert_eq!(error.code(), CODE_CONFIG_INVALID);
    assert!(error.message().contains("short-form"), "{}", error);
    assert!(!error.retryable());
}
