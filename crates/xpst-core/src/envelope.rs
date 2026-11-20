//! Per-destination video format envelopes, ported from `src/xpst/content.py`.
//!
//! One registry holds every number a sender enforces — duration ceilings (whole
//! and short-form), byte caps, containers, codec, aspect rules and the long-form
//! flag — plus the two pure decisions built from it:
//!
//! * [`classify_video_mode`] — short-form / long-form / `None` when ambiguous
//!   ("never guess" is the Python contract and is preserved exactly).
//! * [`video_envelope_violation`] — can this media go to this destination, and
//!   if not why, with the same codes and message wording as Python.
//!
//! No I/O, no ffprobe, no network: these run in a preflight before anything is
//! published. The message strings are part of the contract (a user acts on the
//! number in them), so they are reproduced character for character and pinned
//! by the parity fixture.

use std::fmt;

use crate::content::ContentContractError;

/// Product classification thresholds (explicit by design — a routing decision,
/// not a hidden heuristic). ≤ 60 s and square-or-taller is short-form; ≥ 180 s
/// or wider-than-tall is long-form; anything between is ambiguous and must be
/// asked about, never guessed.
pub const SHORTS_CLASSIFICATION_MAX_SECONDS: u32 = 60;
pub const LONG_FORM_CLASSIFICATION_MIN_SECONDS: u32 = 180;

/// The publishing format a video is routed as.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum VideoMode {
    Shorts,
    Long,
}

impl VideoMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Shorts => "shorts",
            Self::Long => "long",
        }
    }
}

impl fmt::Display for VideoMode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Return the canonical [`VideoMode`] for a caller-supplied spelling.
pub fn coerce_video_mode(value: &str) -> Result<VideoMode, ContentContractError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "shorts" => Ok(VideoMode::Shorts),
        "long" => Ok(VideoMode::Long),
        _ => Err(ContentContractError::UnknownVideoMode(value.to_owned())),
    }
}

/// Stable codes an envelope refusal reports, so a client branches on the code
/// instead of parsing prose.
pub const VIDEO_ENVELOPE_DURATION_CODE: &str = "video_envelope.duration";
pub const VIDEO_ENVELOPE_SIZE_CODE: &str = "video_envelope.size";
pub const VIDEO_ENVELOPE_ASPECT_CODE: &str = "video_envelope.aspect";
pub const VIDEO_ENVELOPE_MODE_CODE: &str = "video_envelope.mode";

/// Severity of a content-contract finding (only errors exist in this slice).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Error,
}

impl Severity {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Error => "error",
        }
    }
}

/// A stable, machine-readable content-contract finding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvelopeViolation {
    pub code: &'static str,
    pub message: String,
    pub severity: Severity,
    pub platform: &'static str,
}

impl EnvelopeViolation {
    /// Whether this finding refuses the operation (true for every code today).
    #[must_use]
    pub const fn is_error(&self) -> bool {
        matches!(self.severity, Severity::Error)
    }
}

/// Exact duration text for a refusal: the number in seconds plus a human form.
///
/// A refusal must carry the number a user can act on, so 140 s is never rounded
/// to "2.3 min" (`140 s (2 min 20 s)`), while an unverifiable ceiling stays
/// `unknown` instead of becoming an invented number.
///
/// Rounding is Python's `round()` — half-to-even — because the message text is
/// contract: 60.5 s reads as `60 s (1 min)` exactly as it does in Python.
#[must_use]
pub fn format_seconds(seconds: Option<f64>) -> String {
    let Some(raw) = seconds else {
        return "unknown".to_owned();
    };
    let value = raw.round_ties_even() as i64;
    if value < 60 {
        return format!("{value} s");
    }
    let human = if value >= 3600 {
        let hours = value / 3600;
        let remainder = value % 3600;
        if remainder == 0 {
            format!("{hours} h")
        } else {
            format!("{hours} h {} min", remainder / 60)
        }
    } else {
        let minutes = value / 60;
        let remainder = value % 60;
        if remainder == 0 {
            format!("{minutes} min")
        } else {
            format!("{minutes} min {remainder} s")
        }
    };
    format!("{value} s ({human})")
}

/// Compact duration for prose: exact seconds under ten minutes, then h/min.
fn compact_seconds(seconds: Option<f64>) -> String {
    let Some(raw) = seconds else {
        return "unknown".to_owned();
    };
    let value = raw.round_ties_even() as i64;
    if value < 600 {
        return format!("{value} s");
    }
    if value >= 3600 {
        let hours = value as f64 / 3600.0;
        return if hours == hours.trunc() {
            // Python formats an integral hour count with `:g` ("12 h").
            format!("{} h", hours as i64)
        } else {
            format!("{hours:.1} h")
        };
    }
    let minutes = value as f64 / 60.0;
    if minutes == minutes.trunc() {
        format!("{} min", minutes as i64)
    } else {
        format!("{minutes:.1} min")
    }
}

/// Human byte ceiling (`256 GB`), binary units; `unknown` if absent.
#[must_use]
pub fn format_bytes(size: Option<u64>) -> String {
    let Some(size) = size else {
        return "unknown".to_owned();
    };
    let mut value = size as f64;
    for unit in ["B", "KB", "MB", "GB", "TB"] {
        if value < 1024.0 || unit == "TB" {
            return if unit == "B" {
                format!("{} B", value as u64)
            } else {
                format!("{value:.0} {unit}")
            };
        }
        value /= 1024.0;
    }
    format!("{value:.0} TB")
}

/// One destination's declared video format envelope.
///
/// Every field is the number a sender enforces, not a suggestion.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct VideoEnvelope {
    pub platform: &'static str,
    pub display_name: &'static str,
    /// Absolute ceiling for any video this destination accepts. `None` means
    /// nothing was verified, so nothing is refused on duration.
    pub max_duration_seconds: Option<u32>,
    /// Ceiling when the video is published in its short-form mode. `None` for a
    /// destination with no separate short mode.
    pub shorts_max_duration_seconds: Option<u32>,
    pub max_bytes: Option<u64>,
    pub containers: &'static [&'static str],
    pub video_codec: &'static str,
    /// Allowed width/height bounds (`None` = any aspect ratio accepted).
    pub aspect_min: Option<f64>,
    pub aspect_max: Option<f64>,
    /// Maximum width/height ratio for the short-form mode (`1.0` = square or
    /// taller than wide, which is what YouTube treats as a Short).
    pub shorts_max_aspect_ratio: Option<f64>,
    /// Modes this destination can publish. `Long` only when there is a real
    /// long-form path — not when a short path merely "also works".
    pub modes: &'static [VideoMode],
    pub long_form: bool,
    /// Where the numbers come from (provider docs / measured behaviour).
    pub source: &'static str,
    pub note: &'static str,
}

impl VideoEnvelope {
    /// The duration ceiling this destination enforces for `mode`.
    #[must_use]
    pub fn duration_cap(&self, mode: Option<VideoMode>) -> Option<u32> {
        if mode == Some(VideoMode::Shorts) {
            // Python's truthiness check: a zero ceiling is "no ceiling".
            if let Some(cap) = self.shorts_max_duration_seconds.filter(|cap| *cap != 0) {
                return Some(cap);
            }
        }
        self.max_duration_seconds
    }

    /// Whether `seconds` fits; an unmeasurable duration is not refused.
    #[must_use]
    pub fn accepts_duration(&self, seconds: Option<f64>, mode: Option<VideoMode>) -> bool {
        let Some(cap) = self.duration_cap(mode) else {
            return true;
        };
        let Some(seconds) = seconds else {
            return true;
        };
        seconds <= f64::from(cap)
    }

    /// Whether this destination has a real path for `mode`.
    #[must_use]
    pub fn supports_mode(&self, mode: VideoMode) -> bool {
        self.modes.contains(&mode)
    }

    /// One-sentence envelope, with the numbers, for refusal messages.
    ///
    /// The absolute ceiling is always stated, and the short-form ceiling is
    /// named separately when the destination has one — so a Shorts refusal
    /// cannot read as if YouTube's overall limit were one minute.
    #[must_use]
    pub fn describe(&self) -> String {
        let mut parts = vec![format!(
            "max duration {}",
            compact_seconds(self.max_duration_seconds.map(f64::from))
        )];
        if let Some(shorts) = self.shorts_max_duration_seconds {
            parts.push(format!(
                "short-form mode {}",
                compact_seconds(Some(f64::from(shorts)))
            ));
        }
        parts.push(format!("max size {}", format_bytes(self.max_bytes)));
        if let (Some(min), Some(max)) = (self.aspect_min, self.aspect_max) {
            parts.push(format!("aspect {min:.2}-{max:.2} (width/height)"));
        }
        if let Some(ratio) = self.shorts_max_aspect_ratio {
            parts.push(format!(
                "short-form needs width/height ≤ {ratio:.2} (square or vertical)"
            ));
        }
        if !self.containers.is_empty() {
            parts.push(format!("containers {}", self.containers.join(", ")));
        }
        if !self.video_codec.is_empty() {
            parts.push(format!("codec {}", self.video_codec));
        }
        let modes = if self.modes.is_empty() {
            "none".to_owned()
        } else {
            self.modes
                .iter()
                .map(|mode| mode.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        };
        parts.push(format!("modes {modes}"));
        format!(
            "{} video envelope: {}.",
            self.display_name,
            parts.join("; ")
        )
    }

    /// Stable JSON-ish shape used by the capability document and the preflight.
    /// Returned as ordered pairs so this crate stays dependency-free.
    #[must_use]
    pub fn fields(&self) -> Vec<(&'static str, String)> {
        vec![
            ("platform", self.platform.to_owned()),
            ("display_name", self.display_name.to_owned()),
            (
                "max_duration_seconds",
                option_number(self.max_duration_seconds.map(u64::from)),
            ),
            (
                "shorts_max_duration_seconds",
                option_number(self.shorts_max_duration_seconds.map(u64::from)),
            ),
            ("max_bytes", option_number(self.max_bytes)),
            ("containers", self.containers.join(",")),
            ("video_codec", self.video_codec.to_owned()),
            ("aspect_min", option_float(self.aspect_min)),
            ("aspect_max", option_float(self.aspect_max)),
            (
                "shorts_max_aspect_ratio",
                option_float(self.shorts_max_aspect_ratio),
            ),
            (
                "modes",
                self.modes
                    .iter()
                    .map(|mode| mode.as_str())
                    .collect::<Vec<_>>()
                    .join(","),
            ),
            ("long_form", self.long_form.to_string()),
        ]
    }
}

fn option_number(value: Option<u64>) -> String {
    value.map_or_else(|| "null".to_owned(), |value| value.to_string())
}

fn option_float(value: Option<f64>) -> String {
    value.map_or_else(|| "null".to_owned(), |value| format!("{value}"))
}

/// Per-destination video envelopes. The ONLY home for these numbers: the media
/// preflight and every uploader read their limit from here, so no surface can
/// enforce a different number than the registry declares.
pub static VIDEO_ENVELOPES: [VideoEnvelope; 7] = [
    VideoEnvelope {
        platform: "youtube",
        display_name: "YouTube",
        // YouTube's upload ceiling is 12 hours or 256 GB, whichever comes first.
        max_duration_seconds: Some(43_200),
        shorts_max_duration_seconds: Some(60),
        max_bytes: Some(256 * 1024 * 1024 * 1024),
        containers: &[".mp4", ".mov"],
        video_codec: "h264",
        modes: &[VideoMode::Shorts, VideoMode::Long],
        long_form: true,
        // YouTube only treats a video as a Short when it is square or taller
        // than wide; a landscape upload tagged #Shorts is an ordinary video.
        shorts_max_aspect_ratio: Some(1.0),
        aspect_min: None,
        aspect_max: None,
        source: "YouTube Data API v3 videos.insert (resumable upload); YouTube upload limits: 12 hours / 256 GB; Shorts: ≤ 60 s, square or taller than wide",
        note: "Both modes use the same resumable videos.insert call; the mode decides the title tag, the returned URL, and which ceiling is enforced.",
    },
    VideoEnvelope {
        platform: "x",
        display_name: "X",
        // Standard accounts: 2 min 20 s (140 s) of video. X Premium accounts
        // allow longer, which is why the adapter lets config raise the cap.
        max_duration_seconds: Some(140),
        shorts_max_duration_seconds: None,
        max_bytes: Some(512 * 1024 * 1024),
        containers: &[".mp4", ".mov"],
        video_codec: "h264",
        aspect_min: Some(1.0 / 2.39),
        aspect_max: Some(2.39),
        shorts_max_aspect_ratio: None,
        modes: &[VideoMode::Shorts],
        long_form: false,
        source: "X media upload limits: 140 s of video for standard accounts, 512 MB, 1:2.39-2.39:1; Premium accounts allow longer videos (raise it with x.max_video_duration_seconds)",
        note: "No long-form path until the account's real cap is raised; over 140 s is refused, never truncated.",
    },
    VideoEnvelope {
        platform: "instagram",
        display_name: "Instagram Reels",
        max_duration_seconds: Some(900),
        shorts_max_duration_seconds: None,
        max_bytes: Some(4 * 1024 * 1024 * 1024),
        containers: &[".mp4", ".mov"],
        video_codec: "h264",
        aspect_min: None,
        aspect_max: None,
        shorts_max_aspect_ratio: None,
        modes: &[VideoMode::Shorts],
        long_form: false,
        source: "Meta Reels API: Reels up to 15 minutes, 4 GB",
        note: "",
    },
    VideoEnvelope {
        platform: "tiktok",
        display_name: "TikTok",
        max_duration_seconds: Some(600),
        shorts_max_duration_seconds: None,
        max_bytes: Some(1024 * 1024 * 1024),
        containers: &[".mp4", ".mov"],
        video_codec: "h264",
        aspect_min: None,
        aspect_max: None,
        shorts_max_aspect_ratio: None,
        modes: &[VideoMode::Shorts],
        long_form: false,
        source: "TikTok Content Posting API: video up to 10 minutes, 1 GB",
        note: "",
    },
    VideoEnvelope {
        platform: "threads",
        display_name: "Threads",
        max_duration_seconds: Some(300),
        shorts_max_duration_seconds: None,
        max_bytes: Some(1024 * 1024 * 1024),
        containers: &[".mp4", ".mov"],
        video_codec: "h264",
        aspect_min: None,
        aspect_max: None,
        shorts_max_aspect_ratio: None,
        modes: &[VideoMode::Shorts],
        long_form: false,
        source: "Threads API: video up to 5 minutes (the container fetches it from a public URL)",
        note: "",
    },
    VideoEnvelope {
        platform: "facebook",
        display_name: "Facebook Page",
        max_duration_seconds: Some(14_400),
        shorts_max_duration_seconds: None,
        max_bytes: Some(10 * 1024 * 1024 * 1024),
        containers: &[".mp4", ".mov"],
        video_codec: "h264",
        aspect_min: None,
        aspect_max: None,
        shorts_max_aspect_ratio: None,
        modes: &[VideoMode::Shorts],
        long_form: false,
        source: "Graph API /{page-id}/videos: up to 4 hours, 10 GB",
        note: "",
    },
    VideoEnvelope {
        platform: "rumble",
        display_name: "Rumble",
        // Rumble's documented upload limits are per-plan and the API is
        // partner-gated, so xPST refuses nothing it cannot verify.
        max_duration_seconds: None,
        shorts_max_duration_seconds: None,
        max_bytes: None,
        containers: &[".mp4", ".mov"],
        video_codec: "h264",
        aspect_min: None,
        aspect_max: None,
        shorts_max_aspect_ratio: None,
        modes: &[VideoMode::Long],
        long_form: true,
        source: "Rumble video upload API is partner-gated; limits are reported by the service per plan",
        note: "Integration-uncertain: no self-serve developer console, so the manual handoff is the reliable path.",
    },
];

/// Return the declared video envelope for a destination, or `None`.
///
/// The lookup is trimmed and case-insensitive, as in Python.
#[must_use]
pub fn video_envelope(platform: &str) -> Option<&'static VideoEnvelope> {
    let key = platform.trim().to_ascii_lowercase();
    VIDEO_ENVELOPES
        .iter()
        .find(|envelope| envelope.platform == key)
}

/// The duration ceiling a sender must enforce for `platform`.
#[must_use]
pub fn video_duration_limit(platform: &str, mode: Option<VideoMode>) -> Option<u32> {
    video_envelope(platform).and_then(|envelope| envelope.duration_cap(mode))
}

/// Everything a caller may have measured about the media, in one struct.
///
/// Every field is optional because "not measured" and "measured as zero" must
/// stay distinguishable: an unmeasured value is never invented.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct MediaMeasurements {
    pub duration_s: Option<f64>,
    pub size_bytes: Option<u64>,
    pub width: Option<u32>,
    pub height: Option<u32>,
}

impl MediaMeasurements {
    /// Measured width/height ratio, or `None` when either side is unmeasured.
    #[must_use]
    pub fn aspect(&self) -> Option<f64> {
        match (self.width, self.height) {
            (Some(width), Some(height)) if width != 0 && height != 0 => {
                Some(f64::from(width) / f64::from(height))
            }
            _ => None,
        }
    }
}

/// The refusal for input outside the given platform's video envelope.
///
/// Pure and side-effect free (no ffprobe, no network), so the preflight, the
/// uploader and a route plan can all call it before anything is published.
/// `None` when the destination is unknown or the input fits.
#[must_use]
pub fn video_envelope_violation(
    platform: &str,
    media: &MediaMeasurements,
    mode: Option<VideoMode>,
) -> Option<EnvelopeViolation> {
    envelope_violation(video_envelope(platform), media, mode)
}

/// The refusal for one concrete envelope (`None` envelope = no refusal).
///
/// Split from [`video_envelope_violation`] so an adapter that knows its
/// account's REAL ceiling (a Premium X account, say) can enforce an adjusted
/// envelope and still speak one vocabulary.
#[must_use]
pub fn envelope_violation(
    envelope: Option<&VideoEnvelope>,
    media: &MediaMeasurements,
    mode: Option<VideoMode>,
) -> Option<EnvelopeViolation> {
    let envelope = envelope?;
    if let Some(mode) = mode {
        if !envelope.supports_mode(mode) {
            let modes = if envelope.modes.is_empty() {
                "none".to_owned()
            } else {
                envelope
                    .modes
                    .iter()
                    .map(|item| item.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            return Some(EnvelopeViolation {
                code: VIDEO_ENVELOPE_MODE_CODE,
                message: format!(
                    "{} has no {}-form path (modes: {}). {}",
                    envelope.display_name,
                    mode.as_str(),
                    modes,
                    envelope.describe()
                ),
                severity: Severity::Error,
                platform: envelope.platform,
            });
        }
    }
    let cap = envelope.duration_cap(mode);
    if let (Some(duration), Some(cap)) = (media.duration_s, cap) {
        if duration > f64::from(cap) {
            let label = if mode == Some(VideoMode::Shorts) {
                "Short-form"
            } else {
                "Video"
            };
            return Some(EnvelopeViolation {
                code: VIDEO_ENVELOPE_DURATION_CODE,
                message: format!(
                    "{label} is {} — over {}'s {} limit. {} Refused rather than truncated.",
                    format_seconds(Some(duration)),
                    envelope.display_name,
                    format_seconds(Some(f64::from(cap))),
                    envelope.describe()
                ),
                severity: Severity::Error,
                platform: envelope.platform,
            });
        }
    }
    if let (Some(size), Some(max_bytes)) = (media.size_bytes, envelope.max_bytes) {
        if size > max_bytes {
            return Some(EnvelopeViolation {
                code: VIDEO_ENVELOPE_SIZE_CODE,
                message: format!(
                    "File is {} — over {}'s {} limit. {}",
                    format_bytes(Some(size)),
                    envelope.display_name,
                    format_bytes(Some(max_bytes)),
                    envelope.describe()
                ),
                severity: Severity::Error,
                platform: envelope.platform,
            });
        }
    }
    let aspect = media.aspect();
    if let (Some(measured), Some(ratio)) = (aspect, envelope.shorts_max_aspect_ratio) {
        if mode == Some(VideoMode::Shorts) && measured > ratio {
            return Some(EnvelopeViolation {
                code: VIDEO_ENVELOPE_ASPECT_CODE,
                message: format!(
                    "{}x{} is aspect {measured:.2} — a {} short must be square or vertical \
                     (width/height ≤ {ratio:.2}). Publish it as long-form, or export a vertical \
                     rendition. {}",
                    media.width.unwrap_or_default(),
                    media.height.unwrap_or_default(),
                    envelope.display_name,
                    envelope.describe()
                ),
                severity: Severity::Error,
                platform: envelope.platform,
            });
        }
    }
    if let (Some(measured), Some(min), Some(max)) =
        (aspect, envelope.aspect_min, envelope.aspect_max)
    {
        if measured < min || measured > max {
            return Some(EnvelopeViolation {
                code: VIDEO_ENVELOPE_ASPECT_CODE,
                message: format!(
                    "{}x{} is aspect {measured:.2} — outside {}'s {min:.2}-{max:.2} range. {}",
                    media.width.unwrap_or_default(),
                    media.height.unwrap_or_default(),
                    envelope.display_name,
                    envelope.describe()
                ),
                severity: Severity::Error,
                platform: envelope.platform,
            });
        }
    }
    None
}

/// Classify a measured video as short-form, long-form, or ambiguous.
///
/// The thresholds are the product decision stated above. `None` means "cannot
/// tell" — the caller must ask, never guess.
#[must_use]
pub fn classify_video_mode(
    duration_s: Option<f64>,
    width: Option<u32>,
    height: Option<u32>,
) -> Option<VideoMode> {
    let duration = duration_s?;
    // Python: `vertical = None if not width or not height else height >= width`
    // — a zero dimension counts as unmeasured, not as landscape.
    let vertical = match (width, height) {
        (Some(width), Some(height)) if width != 0 && height != 0 => Some(height >= width),
        _ => None,
    };
    if duration <= f64::from(SHORTS_CLASSIFICATION_MAX_SECONDS) && vertical != Some(false) {
        return Some(VideoMode::Shorts);
    }
    if duration >= f64::from(LONG_FORM_CLASSIFICATION_MIN_SECONDS) || vertical == Some(false) {
        return Some(VideoMode::Long);
    }
    None
}
