//! Mode resolution: which publishing format a file is, or why xPST refuses it.
//!
//! The product rule, ported from `src/xpst/platforms/youtube.py::_resolve_mode`
//! and the classification thresholds in `xpst.content`:
//!
//! * An explicit `shorts`/`long` wins, and is still held to that mode's
//!   envelope.
//! * With no explicit mode, the media probe decides. A video that cannot be
//!   measured is **refused**, and so is one that is neither clearly short-form
//!   nor clearly long-form (over 60 s but under 180 s, portrait) — refused with
//!   the question ("--shorts or --long"), never defaulted. Guessing is exactly
//!   how a landscape long-form video used to ship as a Short.
//! * The ceiling for the chosen mode is enforced with the *measured* number in
//!   the refusal, so the message states what the file is and what the limit is.
//!
//! Classification, formatting and the envelope checks themselves live in
//! `xpst_core::envelope`, which the planner also reads: this module composes
//! them and adds the wording the uploader reports, so there is one threshold
//! set in the workspace, not two.

use std::fmt;

use xpst_core::envelope::{
    classify_video_mode, envelope_violation, format_seconds, MediaMeasurements, VideoMode,
    LONG_FORM_CLASSIFICATION_MIN_SECONDS, SHORTS_CLASSIFICATION_MAX_SECONDS,
};

use crate::capability::envelope;

/// Stable refusal codes. These are what a client branches on; the prose after
/// the colon is for the person reading it.
pub const CODE_UNMEASURABLE: &str = "YOUTUBE_MODE_UNMEASURABLE";
pub const CODE_AMBIGUOUS: &str = "YOUTUBE_MODE_AMBIGUOUS";
pub const CODE_ENVELOPE: &str = "YOUTUBE_VIDEO_ENVELOPE";
pub const CODE_CONFIG_INVALID: &str = "YOUTUBE_MODE_CONFIG";

/// Why a file cannot be published, in the words the uploader reports.
///
/// Not an error type in the `Result`-failure sense of "try again": every
/// variant is a decision about this file, so [`Refusal::retryable`] is always
/// false and the caller must fix the input or pick a mode.
#[derive(Debug, Clone, PartialEq)]
pub struct Refusal {
    code: &'static str,
    message: String,
    /// The envelope-level code when the refusal came from a limit
    /// (`video_envelope.duration`, `video_envelope.size`, …).
    envelope_code: Option<&'static str>,
    /// The mode that was being enforced, when one was known.
    mode: Option<VideoMode>,
    measurements: MediaMeasurements,
}

impl Refusal {
    /// The stable code (`YOUTUBE_MODE_AMBIGUOUS`, …).
    #[must_use]
    pub const fn code(&self) -> &'static str {
        self.code
    }

    /// The message without the code prefix.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }

    /// The envelope violation code, when a ceiling refused the file.
    #[must_use]
    pub const fn envelope_code(&self) -> Option<&'static str> {
        self.envelope_code
    }

    /// The mode that was being enforced, when one was known.
    #[must_use]
    pub const fn mode(&self) -> Option<VideoMode> {
        self.mode
    }

    /// What was measured. Repeating it in a log line is how a refusal stays
    /// auditable after the file changes.
    #[must_use]
    pub const fn measurements(&self) -> MediaMeasurements {
        self.measurements
    }

    /// Always `false`: a refusal is a decision, not a transient failure.
    #[must_use]
    pub const fn retryable(&self) -> bool {
        false
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for Refusal {}

/// `1920x1080`, or `unmeasured dimensions` when the probe left one out.
///
/// Python's message interpolates `None` here; saying "unmeasured" instead is
/// the only deviation in this module's prose, and it is deliberate — a refusal
/// a user reads should not contain the word `None`.
fn resolution(width: Option<u32>, height: Option<u32>) -> String {
    match (width, height) {
        (Some(width), Some(height)) => format!("{width}x{height}"),
        _ => "unmeasured dimensions".to_owned(),
    }
}

/// Resolve the mode from configuration, honouring `auto` as "ask the probe".
///
/// `auto` (and an unset value) means no explicit mode. An unknown spelling is
/// reported as `Err` so the caller can warn and fall back to the probe — the
/// Python adapter logs exactly that instead of failing the post.
pub fn mode_from_config(value: &str) -> Result<Option<VideoMode>, Refusal> {
    let trimmed = value.trim().to_ascii_lowercase();
    if trimmed.is_empty() || trimmed == "auto" {
        return Ok(None);
    }
    match trimmed.as_str() {
        "shorts" => Ok(Some(VideoMode::Shorts)),
        "long" => Ok(Some(VideoMode::Long)),
        _ => Err(Refusal {
            code: CODE_CONFIG_INVALID,
            message: format!(
                "unknown youtube.video_mode '{value}' — expected 'shorts', 'long' or 'auto'. \
                 Refusing rather than guessing."
            ),
            envelope_code: None,
            mode: None,
            measurements: MediaMeasurements::default(),
        }),
    }
}

/// Decide the publish mode, or return the refusal that stops the upload.
///
/// `requested` is the explicit per-post or configured mode. `file_name` is
/// only used in the message (`video.mp4`), never read.
pub fn resolve_mode(
    requested: Option<VideoMode>,
    file_name: &str,
    measurements: &MediaMeasurements,
) -> Result<VideoMode, Refusal> {
    let envelope = envelope().map_err(|error| Refusal {
        code: CODE_ENVELOPE,
        message: format!(
            "the capability registry has no YouTube video envelope ({error}), so no limit can be \
             enforced. Refusing rather than publishing unchecked."
        ),
        envelope_code: None,
        mode: requested,
        measurements: *measurements,
    })?;

    let target = match requested {
        Some(mode) => mode,
        None => {
            let Some(duration) = measurements.duration_s else {
                return Err(Refusal {
                    code: CODE_UNMEASURABLE,
                    message: format!(
                        "the media probe reported no duration for {file_name}, so xPST cannot \
                         tell a Short (≤ {SHORTS_CLASSIFICATION_MAX_SECONDS} s) from long-form. \
                         Pick one: --shorts or --long (MCP: video_mode), or set \
                         youtube.video_mode. Check that ffprobe can read the file."
                    ),
                    envelope_code: None,
                    mode: None,
                    measurements: *measurements,
                });
            };
            match classify_video_mode(Some(duration), measurements.width, measurements.height) {
                Some(mode) => mode,
                None => {
                    return Err(Refusal {
                        code: CODE_AMBIGUOUS,
                        message: format!(
                            "{} at {} is neither clearly short-form (≤ {} s, square-or-vertical) \
                             nor clearly long-form (≥ {} s or landscape). Pick one: --shorts or \
                             --long (MCP: video_mode), or set youtube.video_mode. xPST will not \
                             guess.",
                            format_seconds(Some(duration)),
                            resolution(measurements.width, measurements.height),
                            SHORTS_CLASSIFICATION_MAX_SECONDS,
                            LONG_FORM_CLASSIFICATION_MIN_SECONDS,
                        ),
                        envelope_code: None,
                        mode: None,
                        measurements: *measurements,
                    });
                }
            }
        }
    };

    if let Some(violation) = envelope_violation(Some(envelope), measurements, Some(target)) {
        return Err(Refusal {
            code: CODE_ENVELOPE,
            message: violation.message,
            envelope_code: Some(violation.code),
            mode: Some(target),
            measurements: *measurements,
        });
    }
    Ok(target)
}

/// The URL a published video gets. Shorts are only "Short" by URL/placement,
/// which is why the mode decides it rather than the caller.
#[must_use]
pub fn post_url(video_id: &str, mode: VideoMode) -> String {
    match mode {
        VideoMode::Shorts => format!("https://youtube.com/shorts/{video_id}"),
        VideoMode::Long => format!("https://www.youtube.com/watch?v={video_id}"),
    }
}
