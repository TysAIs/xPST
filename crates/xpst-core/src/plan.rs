//! Per-destination route planning: may this media be published here, and if not
//! why not.
//!
//! This is the decision layer every publishing surface needs before it attempts
//! anything: given a *measured* media set, the requested content type and a set
//! of candidate destinations with their declared capabilities, answer per
//! destination — accepted or refused, the resolved content type and video mode,
//! the refusals in order (stable code + human message), any rendition decision,
//! and the advisories that do not block.
//!
//! It is pure: no ffprobe, no network, no configuration reads, no writes. The
//! measurement is an input and the capability data is an input, so the same
//! answer can be produced on a phone, in an agent, and in the preflight.
//!
//! Behaviour ported from `src/xpst/content.py` (`envelope_violation`,
//! `media_transport_blocker`, `envelope_summary`), `src/xpst/media/specs.py`
//! (`verify_media`'s pure verdicts, `modality_unsupported_message`),
//! `src/xpst/services/post_preflight.py` (blocker codes and wording) and
//! `src/xpst/services/route_plan.py` (`text_refusal`, the unsupported-content
//! reason, the state mapping). Every message is reproduced character for
//! character and pinned by `tests/plan_parity.rs`; nothing is transcribed by
//! hand — the fixture comes from running the Python functions.
//!
//! Rules that are contract, not implementation detail:
//!
//! * **Refuse, never truncate.** An over-envelope file, an over-limit caption
//!   or an over-count carousel is a refusal carrying the numbers that caused it.
//! * **Ambiguous is not a guess.** A video that is neither clearly short-form
//!   nor clearly long-form at a destination with two modes is refused with the
//!   question to answer (`--shorts` / `--long`), never silently classified.
//! * **A public-URL destination says so up front**, in the platform's own words,
//!   instead of failing at upload time.
//! * **Disabled is disabled.** A destination the user turned off is refused as
//!   disabled, never reported as "unconfigured". A *missing credential* on an
//!   enabled destination is an advisory, not a refusal: the engine attempts it
//!   so the authoritative auth error can be reported.
//! * **A format a destination cannot publish is refused by name**, with the
//!   envelope that explains it.

use crate::content::{
    coerce_content_type, ContentType, MediaKind, MediaTransport, MAX_MEDIA_ITEMS,
};
use crate::envelope::{
    classify_video_mode, envelope_violation, format_seconds, VideoEnvelope, VideoMode,
    LONG_FORM_CLASSIFICATION_MIN_SECONDS, SHORTS_CLASSIFICATION_MAX_SECONDS,
};
use crate::ingest::{
    destinations_accepting, DestinationCapability, DestinationProfile, IngestSpec,
};

/// Stable codes. Every refusal a client may branch on is one of these strings
/// (or one of the `video_envelope.*` codes the envelope registry owns).
pub const UNKNOWN_PLATFORM_CODE: &str = "UNKNOWN_PLATFORM";
/// The destination is declared but this engine cannot publish to it: no crate in
/// this workspace has a publish path for it.
pub const UNIMPLEMENTED_DESTINATION_CODE: &str = "DESTINATION_NOT_IMPLEMENTED";
pub const PLATFORM_DISABLED_CODE: &str = "PLATFORM_DISABLED";
pub const AUTH_NOT_READY_CODE: &str = "AUTH_NOT_READY";
pub const CAPTION_TOO_LONG_CODE: &str = "CAPTION_TOO_LONG";
pub const UNSUPPORTED_CONTENT_CODE: &str = "content_type.unsupported";
pub const UNKNOWN_CONTENT_CODE: &str = "content_type.unknown";
pub const TEXT_REQUIRED_CODE: &str = "content_type.text_required";
pub const MEDIA_NOT_ALLOWED_CODE: &str = "content_type.media_not_allowed";
pub const MEDIA_COUNT_CODE: &str = "content_type.media_count";
pub const MEDIA_KIND_CODE: &str = "content_type.media_kind";
pub const MEDIA_MODALITY_UNSUPPORTED_CODE: &str = "MEDIA_MODALITY_UNSUPPORTED";
pub const MEDIA_SPEC_CONTAINER_CODE: &str = "MEDIA_SPEC_CONTAINER";
pub const MEDIA_SPEC_VIDEO_CODEC_CODE: &str = "MEDIA_SPEC_VIDEO_CODEC";
pub const MEDIA_SPEC_DIMENSIONS_CODE: &str = "MEDIA_SPEC_DIMENSIONS";
pub const MEDIA_SPEC_FILE_SIZE_CODE: &str = "MEDIA_SPEC_FILE_SIZE";
pub const MEDIA_SPEC_ASPECT_RATIO_CODE: &str = "MEDIA_SPEC_ASPECT_RATIO";

/// The state a destination's verdict resolves to, in the canonical vocabulary
/// every surface emits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DestinationState {
    /// Will be published to.
    Posted,
    /// Nothing will be attempted, and that is not an error: the account is not
    /// enabled (or not connected).
    SkippedUnconnected,
    /// Evaluated and refused the content.
    UnsupportedFormat,
    /// Evaluated and REFUSED an upload-time visibility the caller asked for:
    /// nothing was uploaded, and nothing failed. It is its own state rather than
    /// `unsupported_format` because the format is fine — the request is what
    /// cannot be honoured, and a caller reading "unsupported_format" would look
    /// for a media problem that does not exist.
    RefusedVisibility,
    /// A real failure. No destination reaches this state in the planning slice:
    /// a failure means something was attempted.
    Failed,
}

impl DestinationState {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Posted => "posted",
            Self::SkippedUnconnected => "skipped_unconnected",
            Self::UnsupportedFormat => "unsupported_format",
            Self::RefusedVisibility => "refused_visibility",
            Self::Failed => "failed",
        }
    }
}

/// The canonical destination state a stable preflight code maps to.
///
/// ``skipped_unconnected`` covers "not connected" (disabled, or no usable
/// credential); ``unsupported_format`` covers every refusal about the content,
/// the envelope, or a destination this engine cannot publish to at all;
/// everything else is a genuine failure. Ported from
/// `route_plan._state_for_code`, including its treatment of a code nobody has
/// classified as a failure.
#[must_use]
pub fn state_for_code(code: &str) -> DestinationState {
    let upper = code.trim().to_ascii_uppercase();
    match upper.as_str() {
        "PLATFORM_DISABLED" | "AUTH_NOT_READY" => DestinationState::SkippedUnconnected,
        "VISIBILITY_UNAVAILABLE" => DestinationState::RefusedVisibility,
        "CAPTION_TOO_LONG"
        | "MEDIA_MODALITY_UNSUPPORTED"
        | "REMOTE_MEDIA_UNSUPPORTED"
        | "MEDIA_TRANSPORT"
        | "THREADS_NEEDS_URL"
        | "DESTINATION_NOT_IMPLEMENTED" => DestinationState::UnsupportedFormat,
        _ if upper.starts_with("MEDIA_SPEC_") => DestinationState::UnsupportedFormat,
        _ => DestinationState::Failed,
    }
}

/// One stable refusal or advisory: a code to branch on and the sentence a human
/// acts on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reason {
    pub code: String,
    pub message: String,
}

impl Reason {
    fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_owned(),
            message: message.into(),
        }
    }
}

/// What happens to the source bytes on the way to a destination.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransformAction {
    /// Upload the source bytes untouched.
    Passthrough,
    /// Stream-copy into MP4: no re-encode, no generation loss.
    Remux,
    /// Re-encode through the destination's profile.
    Transcode,
}

impl TransformAction {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Passthrough => "passthrough",
            Self::Remux => "remux",
            Self::Transcode => "transcode",
        }
    }
}

/// The rendition decision for one media item.
///
/// The ffmpeg command lines are deliberately absent: building them is the
/// rendition layer's job and needs a target path. This is the *decision*.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformPlan {
    pub action: TransformAction,
    pub reasons: Vec<String>,
}

/// Containers the destinations ingest natively — no remux needed.
pub const NATIVE_CONTAINERS: [&str; 2] = [".mp4", ".mov"];

/// One media item, as measured by the caller.
///
/// Every numeric field is optional because "not measured" and "measured as
/// zero" must stay distinguishable: an unmeasured value is never invented, and
/// an unmeasured duration never causes a duration refusal.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct MediaMeasurement {
    pub duration_s: Option<f64>,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub size_bytes: Option<u64>,
    /// The file's container, dotted (`".mp4"`) or bare (`"mp4"`).
    pub container: String,
    pub video_codec: Option<String>,
    /// Whether the file carries an audio stream. Retained because a rendition
    /// decision needs it; no verdict in this slice turns on it, exactly as in
    /// the Python preflight (which asks about audio *codec* and *rate*, neither
    /// of which a measurement carries).
    pub has_audio: bool,
    /// The offending file's name, used only by a message that names it.
    pub name: Option<String>,
}

impl MediaMeasurement {
    /// The kind this item looks like, by container extension.
    #[must_use]
    pub fn kind(&self) -> MediaKind {
        let suffix = self.suffix();
        crate::content::media_kind(&format!("x{suffix}"))
    }

    /// Lowercase dotted suffix (`".mp4"`), normalized from whatever was given.
    #[must_use]
    pub fn suffix(&self) -> String {
        let raw = self.container.trim().to_ascii_lowercase();
        if raw.is_empty() {
            String::new()
        } else if raw.starts_with('.') {
            raw
        } else {
            format!(".{raw}")
        }
    }

    fn name_or_placeholder(&self) -> String {
        self.name.clone().unwrap_or_else(|| "<media>".to_owned())
    }
}

/// One publish request: the media, the caption, the format, the mode.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RouteRequest {
    pub media: Vec<MediaMeasurement>,
    pub caption: String,
    /// The caller's content-type value. `None`, empty and `"auto"` all mean
    /// "classify it"; an unrecognized value is refused by name rather than
    /// guessed at.
    pub content_type: Option<String>,
    /// The caller's explicit video mode (`--shorts` / `--long`, or the
    /// account's configured mode). `None` lets the measurement decide, or
    /// refuses the question when it cannot.
    pub requested_mode: Option<VideoMode>,
}

impl RouteRequest {
    /// A request with a shared caption and no explicit format.
    #[must_use]
    pub fn new(media: Vec<MediaMeasurement>, caption: impl Into<String>) -> Self {
        Self {
            media,
            caption: caption.into(),
            content_type: None,
            requested_mode: None,
        }
    }

    /// State the format explicitly (anything except `"auto"` is taken as a
    /// claim; the planner still validates it).
    #[must_use]
    pub fn with_content_type(mut self, content_type: impl Into<String>) -> Self {
        self.content_type = Some(content_type.into());
        self
    }

    /// State the video mode explicitly.
    #[must_use]
    pub fn with_mode(mut self, mode: VideoMode) -> Self {
        self.requested_mode = Some(mode);
        self
    }
}

/// One destination's verdict.
#[derive(Debug, Clone, PartialEq)]
pub struct DestinationVerdict {
    pub platform: String,
    /// Whether this destination will be published to.
    pub accepted: bool,
    pub state: DestinationState,
    /// The resolved content type as a string (`"video"`), or the caller's
    /// unrecognized value when it could not be resolved.
    pub content_type: Option<String>,
    /// The resolved video mode, when the destination has modes and one was
    /// decided.
    pub mode: Option<VideoMode>,
    /// Refusals in the order they were decided; empty when accepted.
    pub refusals: Vec<Reason>,
    /// Advisories: real findings that do not stop the post.
    pub advisories: Vec<Reason>,
    /// The rendition decision per media item, in request order. Empty when the
    /// destination is refused or the request carries no video.
    pub transforms: Vec<TransformPlan>,
}

impl DestinationVerdict {
    fn refused(
        destination: &DestinationCapability,
        state: DestinationState,
        content_type: Option<String>,
        reason: Reason,
    ) -> Self {
        Self {
            platform: destination.platform.clone(),
            accepted: false,
            state,
            content_type,
            mode: None,
            refusals: vec![reason],
            advisories: Vec::new(),
            transforms: Vec::new(),
        }
    }
}

/// The whole plan: one verdict per candidate destination, plus the refusals
/// that apply to the request itself.
#[derive(Debug, Clone, PartialEq)]
pub struct RoutePlan {
    /// The resolved format, when the vocabulary recognized it.
    pub content_type: Option<ContentType>,
    /// Destinations that will be published to, in request order.
    pub accepted: Vec<String>,
    pub destinations: Vec<DestinationVerdict>,
    /// Refusals that are not about one destination: an unrecognized content
    /// type, or a media shape the format cannot carry. Python reports these
    /// before it plans a destination, and so does this.
    pub request_blockers: Vec<Reason>,
}

impl RoutePlan {
    /// Whether anything will be published: at least one accepted destination
    /// and no request-level refusal.
    #[must_use]
    pub fn ready(&self) -> bool {
        !self.accepted.is_empty() && self.request_blockers.is_empty()
    }

    /// The verdict for one destination, if it was a candidate.
    #[must_use]
    pub fn destination(&self, platform: &str) -> Option<&DestinationVerdict> {
        let key = platform.trim().to_ascii_lowercase();
        self.destinations.iter().find(|item| item.platform == key)
    }

    /// Whether anything will be published to `platform`.
    #[must_use]
    pub fn accepts(&self, platform: &str) -> bool {
        self.destination(platform).is_some_and(|item| item.accepted)
    }
}

/// Plan a publish request against the candidate destinations.
///
/// Destinations are evaluated in the order given; each verdict is independent,
/// so an unconnected or refused destination never changes another's answer.
#[must_use]
pub fn plan(request: &RouteRequest, destinations: &[DestinationCapability]) -> RoutePlan {
    let mut request_blockers = Vec::new();
    let resolved = resolve_content_type(request, &mut request_blockers);

    if let Some(content_type) = resolved {
        request_blockers.extend(media_shape_refusals(content_type, &request.media));
    }

    let verdicts: Vec<DestinationVerdict> = destinations
        .iter()
        .map(|destination| plan_destination(request, destination, resolved, &request_blockers))
        .collect();
    let accepted: Vec<String> = verdicts
        .iter()
        .filter(|verdict| verdict.accepted)
        .map(|verdict| verdict.platform.clone())
        .collect();

    RoutePlan {
        content_type: resolved,
        accepted,
        destinations: verdicts,
        request_blockers,
    }
}

/// The caller's content-type claim as a normalized string, `None` when they did
/// not make one (absent, empty or `"auto"`).
#[must_use]
pub fn normalize_content_type(raw: Option<&str>) -> Option<String> {
    let value = raw.unwrap_or("").trim().to_ascii_lowercase();
    if value.is_empty() || value == "auto" {
        None
    } else {
        Some(value)
    }
}

fn resolve_content_type(request: &RouteRequest, blockers: &mut Vec<Reason>) -> Option<ContentType> {
    match normalize_content_type(request.content_type.as_deref()) {
        None => Some(infer_content_type(&request.media)),
        Some(raw) => match coerce_content_type(&raw) {
            Ok(content_type) => Some(content_type),
            Err(error) => {
                // Python refuses an unrecognized content type with this exact
                // sentence (`validate_content_request`); the route planner used
                // to raise instead, which no surface could act on.
                blockers.push(Reason::new(UNKNOWN_CONTENT_CODE, error.to_string()));
                None
            }
        },
    }
}

/// Classify a request that did not state its format.
///
/// No media is a text post, one image is an image post, any other single file
/// is a video post, and several files are a carousel — exactly
/// `xpst.content.infer_content_type`.
#[must_use]
pub fn infer_content_type(media: &[MediaMeasurement]) -> ContentType {
    match media.len() {
        0 => ContentType::Text,
        1 if media[0].kind() == MediaKind::Image => ContentType::Image,
        1 => ContentType::Video,
        _ => ContentType::Carousel,
    }
}

/// Per content type: (minimum media, maximum media, allowed kinds).
fn media_rule(content_type: ContentType) -> (usize, usize, &'static [MediaKind]) {
    const ANY: &[MediaKind] = &[MediaKind::Video, MediaKind::Image, MediaKind::Unknown];
    match content_type {
        ContentType::Video => (1, 1, &[MediaKind::Video, MediaKind::Unknown]),
        ContentType::Image => (1, 1, &[MediaKind::Image]),
        ContentType::Carousel => (2, MAX_MEDIA_ITEMS, ANY),
        ContentType::Text | ContentType::Thread => (0, 0, &[]),
    }
}

fn media_shape_refusals(content_type: ContentType, media: &[MediaMeasurement]) -> Vec<Reason> {
    let (minimum, maximum, allowed) = media_rule(content_type);
    let label = content_type.label();
    if matches!(content_type, ContentType::Text | ContentType::Thread) {
        if media.is_empty() {
            return Vec::new();
        }
        return vec![Reason::new(
            MEDIA_NOT_ALLOWED_CODE,
            format!(
                "{label} carry no media (got {count} file{suffix}); use image, carousel or video instead.",
                count = media.len(),
                suffix = if media.len() == 1 { "" } else { "s" },
            ),
        )];
    }
    if media.len() < minimum || media.len() > maximum {
        let expected = if minimum == maximum {
            format!("exactly {minimum} media file")
        } else {
            format!("between {minimum} and {maximum} media files")
        };
        return vec![Reason::new(
            MEDIA_COUNT_CODE,
            format!("{label} take {expected} (got {}).", media.len()),
        )];
    }
    // The allowed set is never empty here, so the first entry is the noun the
    // refusal names ("image files", "video files").
    let noun = allowed
        .first()
        .map_or("media", |kind| match kind {
            MediaKind::Image => "image",
            MediaKind::Video => "video",
            MediaKind::Unknown => "unknown",
        })
        .to_owned();
    media
        .iter()
        .filter(|item| !allowed.contains(&item.kind()))
        .map(|item| {
            Reason::new(
                MEDIA_KIND_CODE,
                format!(
                    "{label} need {noun} files; {name} is {kind}.",
                    name = item.name_or_placeholder(),
                    kind = item.kind(),
                ),
            )
        })
        .collect()
}

fn plan_destination(
    request: &RouteRequest,
    destination: &DestinationCapability,
    content_type: Option<ContentType>,
    request_blockers: &[Reason],
) -> DestinationVerdict {
    let Some(profile) = destination.profile.as_ref() else {
        return DestinationVerdict::refused(
            destination,
            DestinationState::UnsupportedFormat,
            None,
            Reason::new(
                UNKNOWN_PLATFORM_CODE,
                format!(
                    "{} is not a known publishing destination.",
                    destination.platform
                ),
            ),
        );
    };
    if !destination.enabled {
        return DestinationVerdict::refused(
            destination,
            DestinationState::SkippedUnconnected,
            content_type.map(|item| item.as_str().to_owned()),
            Reason::new(
                PLATFORM_DISABLED_CODE,
                format!(
                    "{} is disabled in local configuration.",
                    destination.platform
                ),
            ),
        );
    }
    let Some(content_type) = content_type else {
        // The request stated a format the vocabulary does not know: no
        // destination can accept it, and pretending otherwise would be a guess.
        // The request-level refusal is the destination's reason too.
        let reason = request_blockers.first().cloned().unwrap_or_else(|| {
            Reason::new(
                UNKNOWN_CONTENT_CODE,
                request
                    .content_type
                    .as_deref()
                    .map_or_else(String::new, unknown_content_message),
            )
        });
        return DestinationVerdict::refused(
            destination,
            DestinationState::UnsupportedFormat,
            request.content_type.clone(),
            reason,
        );
    };
    let resolved = content_type.as_str().to_owned();
    if !profile.publishing {
        return DestinationVerdict::refused(
            destination,
            DestinationState::UnsupportedFormat,
            Some(resolved),
            Reason::new(
                UNSUPPORTED_CONTENT_CODE,
                format!(
                    "{platform} is a messaging destination: it sends DMs and cannot publish {label}. \
                     Use a publishing destination instead.",
                    platform = destination.platform,
                    label = content_type.label(),
                ),
            ),
        );
    }
    // Declared but not built. A destination with no content type this engine can
    // publish is a destination nothing can publish to, so accepting it would put
    // a promise in the plan that no provider can keep. The refusal lands before
    // the content-type check so the reason names the missing implementation
    // rather than claiming a format it cannot take.
    if profile.implemented.is_empty()
        || crate::content::provider_crate(&destination.platform).is_none()
    {
        return DestinationVerdict::refused(
            destination,
            DestinationState::UnsupportedFormat,
            Some(resolved),
            Reason::new(
                UNIMPLEMENTED_DESTINATION_CODE,
                format!(
                    "{platform} is declared but not implemented by this engine: no provider in \
                     this workspace can publish to it, so nothing was attempted. Use a \
                     destination this build implements instead.",
                    platform = destination.platform,
                ),
            ),
        );
    }
    if !profile.supports(content_type) {
        return DestinationVerdict::refused(
            destination,
            DestinationState::UnsupportedFormat,
            Some(resolved),
            Reason::new(
                UNSUPPORTED_CONTENT_CODE,
                unsupported_content_message(destination, profile, content_type),
            ),
        );
    }

    let mut verdict = DestinationVerdict {
        platform: destination.platform.clone(),
        accepted: true,
        state: DestinationState::Posted,
        content_type: Some(resolved),
        mode: None,
        refusals: Vec::new(),
        advisories: Vec::new(),
        transforms: Vec::new(),
    };

    if matches!(content_type, ContentType::Text | ContentType::Thread)
        && request.caption.trim().is_empty()
    {
        verdict.refusals.push(Reason::new(
            TEXT_REQUIRED_CODE,
            format!(
                "{label} for {platform} need text: the request carried none.",
                label = content_type.label(),
                platform = destination.platform,
            ),
        ));
    }

    for item in &request.media {
        let (refusal, advisories, mode, transform) =
            judge_media(request, destination, profile, item);
        if let Some(reason) = refusal {
            verdict.refusals.push(reason);
        }
        verdict.advisories.extend(advisories);
        if mode.is_some() && verdict.mode.is_none() {
            verdict.mode = mode;
        }
        if let Some(transform) = transform {
            verdict.transforms.push(transform);
        }
    }

    if let Some(reason) = caption_refusal(destination, content_type, &request.caption) {
        verdict.refusals.push(reason);
    }

    if !destination.authenticated && !destination.auth_detail.trim().is_empty() {
        // An enabled destination with no usable credential is *attempted*: the
        // uploader reports the authoritative auth error. Never a refusal.
        verdict.advisories.push(Reason::new(
            AUTH_NOT_READY_CODE,
            destination.auth_detail.clone(),
        ));
    }

    if !verdict.refusals.is_empty() {
        verdict.accepted = false;
        verdict.state = DestinationState::UnsupportedFormat;
        verdict.transforms.clear();
    }
    verdict
}

fn unknown_content_message(raw: &str) -> String {
    let known = crate::content::CONTENT_TYPES
        .iter()
        .map(|item| item.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    format!("Unknown content type '{raw}': choose one of {known}.")
}

/// The route planner's refusal for a format a destination cannot publish.
///
/// It names the destination, lists what it *can* publish, and carries the
/// envelope that explains the limit — so a text post aimed at a video-only
/// destination learns the video ceilings in the same breath.
fn unsupported_content_message(
    destination: &DestinationCapability,
    profile: &DestinationProfile,
    content_type: ContentType,
) -> String {
    let mut supported: Vec<&str> = profile
        .implemented
        .iter()
        .map(|item| item.as_str())
        .collect();
    supported.sort_unstable();
    format!(
        "{platform} cannot publish {label} (supports: {supported}). Envelope — {envelope}.",
        platform = destination.platform,
        label = content_type.label(),
        supported = supported.join(", "),
        envelope = envelope_summary(destination, profile, Some(content_type.as_str())),
    )
}

/// One-line, human-readable envelope for a destination.
///
/// Used verbatim in refusal messages, so a user learns the limit from the
/// refusal instead of guessing. It mirrors `route_plan.envelope_summary`
/// including its quirks: a destination asked for a format it does not take
/// still shows its real video envelope, and a destination with no text limit
/// simply omits the caption clause.
#[must_use]
pub fn envelope_summary(
    destination: &DestinationCapability,
    profile: &DestinationProfile,
    content_type: Option<&str>,
) -> String {
    let wanted = content_type.unwrap_or("").trim().to_ascii_lowercase();
    let mut parts: Vec<String> = Vec::new();
    // "What this destination takes", so `declared`: the envelope is a property
    // of the destination, not of how much of it this engine implements.
    let video_declared = profile.declares(ContentType::Video);
    let video = profile.envelope;
    if video_declared && wanted != "image" {
        let mut caps: Vec<String> = Vec::new();
        if let Some(envelope) = video {
            if let Some(duration) = envelope.max_duration_seconds {
                caps.push(format!("≤{duration}s"));
            }
        }
        if let Some(ingest) = profile.ingest {
            if !ingest.video_containers.is_empty() {
                caps.push(upper_slash_join(ingest.video_containers));
            }
            if !ingest.video_codec.is_empty() {
                caps.push(ingest.video_codec.to_owned());
            }
            if let Some(bytes) = ingest.video_file_size_cap_bytes {
                caps.push(format!("≤{}", human_bytes(bytes)));
            }
        }
        parts.push(format!("video {}", caps.join(", ")));
    }
    if wanted == "image" && profile.declares(ContentType::Image) {
        if let Some(ingest) = profile.ingest {
            let mut caps: Vec<String> = Vec::new();
            if !ingest.image_containers.is_empty() {
                caps.push(upper_slash_join(ingest.image_containers));
            }
            if let Some(bytes) = ingest.image_max_bytes {
                caps.push(format!("≤{}", human_bytes(bytes)));
            }
            if let (Some(min), Some(max)) = (ingest.image_aspect_min, ingest.image_aspect_max) {
                caps.push(format!(
                    "aspect {}:1–{}:1",
                    python_float(min),
                    python_float(max)
                ));
            }
            parts.push(format!("image {}", caps.join(", ")));
        }
    }
    if let Some(limit) = profile.text_limit {
        parts.push(format!("caption ≤{limit} chars"));
    }
    if parts.is_empty() {
        // Nothing measurable to describe: name the content types the
        // destination declares. This reads `declared`, not `implemented`,
        // because the question is what the destination takes — an engine-side
        // implementation gap (a destination with no provider crate) is not an
        // envelope property, and the planner refuses such a destination before
        // any envelope message is built.
        let mut declared: Vec<&str> = profile.declared.iter().map(|item| item.as_str()).collect();
        declared.sort_unstable();
        let list = if declared.is_empty() {
            "nothing".to_owned()
        } else {
            declared.join(", ")
        };
        return format!("{}: {list}", destination.platform);
    }
    format!("{}: {}", destination.platform, parts.join(", "))
}

/// A float as Python's `str()` renders it: shortest round-trip, but an integral
/// value keeps its `.0` (`3.0`, not `3`). The envelope summary quotes these
/// numbers verbatim, so the difference is visible to a user.
#[must_use]
pub fn python_float(value: f64) -> String {
    let text = format!("{value}");
    if text.contains('.') || text.contains('e') || text.contains("inf") || text.contains("NaN") {
        text
    } else {
        format!("{text}.0")
    }
}

fn upper_slash_join(containers: &[&str]) -> String {
    containers
        .iter()
        .map(|item| item.trim_start_matches('.').to_ascii_uppercase())
        .collect::<Vec<_>>()
        .join("/")
}

/// Byte ceiling in the largest whole unit (GB/MB/TB), for messages.
fn human_bytes(value: u64) -> String {
    if value == 0 {
        return "0 B".to_owned();
    }
    for (unit, size) in [
        ("TB", 1024_u64.pow(4)),
        ("GB", 1024_u64.pow(3)),
        ("MB", 1024_u64.pow(2)),
    ] {
        if value >= size {
            let amount = value as f64 / size as f64;
            return if amount >= 10.0 {
                format!("{amount:.0} {unit}")
            } else {
                format!("{amount:.1} {unit}")
            };
        }
    }
    format!("{value} B")
}

/// The refusal for a caption past a destination's verified limit, or `None`.
///
/// Refusing (not truncating) is the contract: the message names both numbers
/// and the destination's envelope, so the caller can shorten once instead of
/// discovering the limit per platform.
#[must_use]
pub fn caption_refusal(
    destination: &DestinationCapability,
    content_type: ContentType,
    caption: &str,
) -> Option<Reason> {
    let profile = destination.profile.as_ref()?;
    let limit = profile.text_limit?;
    let length = caption.chars().count();
    if length <= limit {
        return None;
    }
    let refusal = format!(
        "{platform} accepts {limit} characters and this caption is {length}; xPST refuses rather than truncates. \
         Shorten it to {limit} characters or use --caption-for {platform}=<text>. Envelope — {envelope}.",
        platform = destination.platform,
        envelope = envelope_summary(destination, profile, Some("text")),
    );
    // The route plan appends the detected format's envelope to every
    // unsupported-format reason, and a caption refusal is one — so the envelope
    // is stated twice, exactly as the shipped planner does it.
    Some(Reason::new(
        CAPTION_TOO_LONG_CODE,
        format!(
            "{refusal} Envelope — {}.",
            envelope_summary(destination, profile, Some(content_type.as_str()))
        ),
    ))
}

/// Judge one media item for one destination.
///
/// Returns at most one refusal (the first decisive one, so a refusal is never a
/// pile of unrelated complaints), the advisories that do not block, the mode it
/// decided, and the rendition decision when the item is accepted here.
fn judge_media(
    request: &RouteRequest,
    destination: &DestinationCapability,
    profile: &DestinationProfile,
    item: &MediaMeasurement,
) -> (
    Option<Reason>,
    Vec<Reason>,
    Option<VideoMode>,
    Option<TransformPlan>,
) {
    let kind = item.kind();
    let ingest = profile.ingest;
    // A destination that cannot publish this file's modality is already fully
    // explained by the one modality refusal: do not stack a container or
    // transport complaint on top of it.
    let modality_blocked = match kind {
        MediaKind::Image => !profile.supports(ContentType::Image),
        MediaKind::Video => !profile.supports(ContentType::Video),
        MediaKind::Unknown => false,
    };
    if modality_blocked {
        let modality = if kind == MediaKind::Image {
            "image"
        } else {
            "video"
        };
        let reason = Reason::new(
            MEDIA_MODALITY_UNSUPPORTED_CODE,
            modality_unsupported_message(
                modality,
                &item.suffix(),
                ingest.map_or(destination.platform.as_str(), |spec| spec.display_name),
            ),
        );
        return (Some(reason), Vec::new(), None, None);
    }

    if kind == MediaKind::Image {
        return judge_image(item, ingest);
    }

    // ── Video (or an unrecognized container the caller claims is video) ──
    if let Some(spec) = ingest {
        let suffix = item.suffix();
        if !spec.video_containers.contains(&suffix.as_str()) {
            return (
                Some(Reason::new(
                    MEDIA_SPEC_CONTAINER_CODE,
                    format!(
                        "{suffix} vs accepted {accepted}",
                        suffix = if suffix.is_empty() {
                            "(none)".to_owned()
                        } else {
                            suffix
                        },
                        accepted = spec.video_containers.join(", "),
                    ),
                )),
                Vec::new(),
                None,
                None,
            );
        }
    }

    let advisories = ingest.map_or_else(Vec::new, |spec| video_advisories(spec, item));
    let mode = match resolve_mode(request, destination, profile, item) {
        Ok(mode) => mode,
        Err(reason) => return (Some(reason), advisories, None, None),
    };
    if let Some(envelope) = profile.envelope.as_ref() {
        let media = crate::envelope::MediaMeasurements {
            duration_s: item.duration_s,
            size_bytes: item.size_bytes,
            width: item.width,
            height: item.height,
        };
        if let Some(violation) = envelope_violation(Some(envelope), &media, mode) {
            return (
                Some(Reason::new(violation.code, violation.message)),
                advisories,
                mode,
                None,
            );
        }
    }
    // The transport verdict comes from the destination's own declaration, so
    // the plan can never promise a local file a destination would then refuse —
    // and no network call is made to find out.
    if let Some(reason) = transport_refusal(profile) {
        return (Some(reason), advisories, mode, None);
    }
    let transform = ingest.and_then(|spec| transform_for(spec, item));
    (None, advisories, mode, transform)
}

fn judge_image(
    item: &MediaMeasurement,
    ingest: Option<&IngestSpec>,
) -> (
    Option<Reason>,
    Vec<Reason>,
    Option<VideoMode>,
    Option<TransformPlan>,
) {
    let Some(spec) = ingest else {
        return (None, Vec::new(), None, None);
    };
    let display = spec.display_name;
    let suffix = item.suffix();
    if !spec.image_containers.contains(&suffix.as_str()) {
        return (
            Some(Reason::new(
                MEDIA_SPEC_CONTAINER_CODE,
                format!(
                    "{display} does not publish {suffix} images: it accepts {accepted}. \
                     Convert the file and try again.",
                    suffix = if suffix.is_empty() {
                        "that file type".to_owned()
                    } else {
                        suffix
                    },
                    accepted = if spec.image_containers.is_empty() {
                        "(none)".to_owned()
                    } else {
                        spec.image_containers.join(", ")
                    },
                ),
            )),
            Vec::new(),
            None,
            None,
        );
    }
    let dimensions = item
        .width
        .zip(item.height)
        .filter(|(width, height)| *width != 0 && *height != 0);
    if let (Some((width, height)), Some(min), Some(max)) =
        (dimensions, spec.image_aspect_min, spec.image_aspect_max)
    {
        let ratio = f64::from(width) / f64::from(height);
        if !(min - 1e-6..=max + 1e-6).contains(&ratio) {
            return (
                Some(Reason::new(
                    MEDIA_SPEC_ASPECT_RATIO_CODE,
                    format!(
                        "{display} needs an image between {min:.2}:1 and {max:.2}:1 \
                         (width:height); this one is {width}x{height} ({ratio:.2}:1). \
                         Crop or resize it and try again."
                    ),
                )),
                Vec::new(),
                None,
                None,
            );
        }
    }
    if let (Some(size), Some(cap), Some(cap_text)) =
        (item.size_bytes, spec.image_max_bytes, spec.image_cap_text())
    {
        if size > cap {
            let size_mb = size as f64 / (1024.0 * 1024.0);
            return (
                Some(Reason::new(
                    MEDIA_SPEC_FILE_SIZE_CODE,
                    format!("{size_mb:.1} MB vs {display} image cap {cap_text}"),
                )),
                Vec::new(),
                None,
                None,
            );
        }
    }
    let advisories = image_dimensions_advisory(item, spec).into_iter().collect();
    (None, advisories, None, None)
}

/// The long-edge advisory for a still image: above the destination's ceiling it
/// re-encodes, which is not a refusal.
fn image_dimensions_advisory(item: &MediaMeasurement, spec: &IngestSpec) -> Option<Reason> {
    let (width, height) = item.width.zip(item.height)?;
    let long_edge = width.max(height);
    if long_edge == 0 || long_edge <= spec.video_long_edge {
        return None;
    }
    Some(Reason::new(
        MEDIA_SPEC_DIMENSIONS_CODE,
        format!(
            "{width}x{height} (long edge {long_edge}) vs max {edge}",
            edge = spec.video_long_edge
        ),
    ))
}

/// The non-blocking findings for a video item.
///
/// A codec mismatch and a long edge above the destination's ceiling are real:
/// the destination either re-encodes or processes the file differently, which is
/// why they are advisories rather than silence. Neither refuses the post.
fn video_advisories(spec: &IngestSpec, item: &MediaMeasurement) -> Vec<Reason> {
    let mut advisories = Vec::new();
    if let Some(actual) = item.video_codec.as_deref() {
        if actual != spec.video_codec {
            advisories.push(Reason::new(
                MEDIA_SPEC_VIDEO_CODEC_CODE,
                format!("{actual} vs {}", spec.video_codec),
            ));
        }
    }
    if let Some((width, height)) = item.width.zip(item.height) {
        // Python's check is `0 < long_edge <= cap`, so an unmeasured (zero)
        // dimension warns as well as one above the ceiling: both mean the
        // destination will re-encode rather than ingest the file as-is.
        let long_edge = width.max(height);
        if long_edge == 0 || long_edge > spec.video_long_edge {
            advisories.push(Reason::new(
                MEDIA_SPEC_DIMENSIONS_CODE,
                format!(
                    "{width}x{height} (long edge {long_edge}) vs max {edge} — advisory: {display} \
                     re-encodes above its ceiling instead of rejecting the file",
                    edge = spec.video_long_edge,
                    display = spec.display_name,
                ),
            ));
        }
    }
    advisories
}

/// Resolve the video mode for a destination, or return the refusal that asks.
///
/// An explicit request wins. A destination with a single mode needs none. A
/// destination with two modes (short-form and long-form) must be told which
/// one, because the ceiling it enforces differs — and a video the measurement
/// cannot place is refused with the question, never guessed.
fn resolve_mode(
    request: &RouteRequest,
    destination: &DestinationCapability,
    profile: &DestinationProfile,
    item: &MediaMeasurement,
) -> Result<Option<VideoMode>, Reason> {
    if let Some(mode) = request.requested_mode {
        return Ok(Some(mode));
    }
    let Some(envelope) = profile.envelope.as_ref() else {
        return Ok(None);
    };
    if !needs_mode_decision(envelope) {
        return Ok(None);
    }
    let Some(duration) = item.duration_s else {
        let code = format!(
            "{}_MODE_UNMEASURABLE",
            destination.platform.to_ascii_uppercase()
        );
        return Err(Reason::new(
            &code,
            format!(
                "{code}: the media probe reported no duration for {name}, so xPST cannot tell a \
                 Short (≤ {shorts} s) from long-form. Pick one: --shorts or --long \
                 (MCP: video_mode), or set {platform}.video_mode. Check that ffprobe can read the file.",
                name = item.name_or_placeholder(),
                shorts = SHORTS_CLASSIFICATION_MAX_SECONDS,
                platform = destination.platform,
            ),
        ));
    };
    match classify_video_mode(Some(duration), item.width, item.height) {
        Some(mode) => Ok(Some(mode)),
        None => {
            let code = format!(
                "{}_MODE_AMBIGUOUS",
                destination.platform.to_ascii_uppercase()
            );
            Err(Reason::new(
                &code,
                format!(
                    "{code}: {duration} at {width}x{height} is neither clearly short-form \
                     (≤ {shorts} s, square-or-vertical) nor clearly long-form \
                     (≥ {long} s or landscape). Pick one: --shorts or --long (MCP: video_mode), \
                     or set {platform}.video_mode. xPST will not guess.",
                    duration = format_seconds(Some(duration)),
                    width = display_dimension(item.width),
                    height = display_dimension(item.height),
                    shorts = SHORTS_CLASSIFICATION_MAX_SECONDS,
                    long = LONG_FORM_CLASSIFICATION_MIN_SECONDS,
                    platform = destination.platform,
                ),
            ))
        }
    }
}

fn display_dimension(value: Option<u32>) -> String {
    value.map_or_else(|| "None".to_owned(), |value| value.to_string())
}

/// Whether a destination's envelope has more than one mode, which makes the
/// choice the caller's (or the measurement's) rather than the planner's.
#[must_use]
pub fn needs_mode_decision(envelope: &VideoEnvelope) -> bool {
    envelope.supports_mode(VideoMode::Shorts) && envelope.supports_mode(VideoMode::Long)
}

fn transport_refusal(profile: &DestinationProfile) -> Option<Reason> {
    if profile.media_transport != MediaTransport::PublicUrl {
        return None;
    }
    let message = profile
        .media_transport_requirement
        .clone()
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| {
            format!(
                "{display} cannot take a local file: it fetches media from a URL you host, \
                 and xPST has no server to provide one.",
                display = profile.display_name,
            )
        });
    Some(Reason::new(
        profile
            .media_transport_error_code
            .as_deref()
            .unwrap_or(crate::content::MEDIA_TRANSPORT_ERROR_CODE),
        message,
    ))
}

/// The rendition decision for one video item.
///
/// Mirrors `plan_transform`'s fidelity invariant: never spend a quality
/// generation on media that does not need one. A measurement carries the codec
/// but not the pixel format, frame rate or bitrate a full compliance probe
/// reads, so a codec mismatch is the only stream violation this layer can see —
/// it refuses to claim passthrough and decides transcode.
fn transform_for(spec: &IngestSpec, item: &MediaMeasurement) -> Option<TransformPlan> {
    if item.kind() != MediaKind::Video {
        return None;
    }
    let actual = item.video_codec.as_deref().unwrap_or("unknown");
    let compliance = format!("video codec {actual} vs {}", spec.video_codec);
    if actual != spec.video_codec {
        return Some(TransformPlan {
            action: TransformAction::Transcode,
            reasons: vec![format!("streams violate profile: {compliance}")],
        });
    }
    let suffix = item.suffix();
    let mut reasons = vec![format!("streams satisfy profile ({compliance})")];
    let action = if NATIVE_CONTAINERS.contains(&suffix.as_str()) {
        reasons.push(format!("container {suffix} natively accepted"));
        TransformAction::Passthrough
    } else {
        reasons.push(format!(
            "container {suffix} not natively accepted — stream-copy to MP4 (no re-encode)"
        ));
        TransformAction::Remux
    };
    Some(TransformPlan { action, reasons })
}

/// The refusal sentence for a file whose modality a destination cannot publish.
///
/// Ported from `xpst.media.specs.modality_unsupported_message`: it names who
/// cannot take the file, who can, and what to do instead — never a bare spec
/// comparison.
#[must_use]
pub fn modality_unsupported_message(modality: &str, suffix: &str, destination: &str) -> String {
    let article = if modality
        .chars()
        .next()
        .is_some_and(|first| "aeiouAEIOU".contains(first))
    {
        "an"
    } else {
        "a"
    };
    let mut file_label = format!("{article} {modality} file");
    if !suffix.is_empty() {
        file_label.push_str(&format!(" ({suffix})"));
    }
    let accepting = destinations_accepting(modality);
    let advice = if modality == "image" {
        " Choose an image format your destinations accept, or a video file."
    } else {
        " Choose a different file."
    };
    let head = format!("{destination} cannot accept {file_label}");
    if accepting.is_empty() {
        let tail = if modality == "image" {
            " xPST has no image publish path yet."
        } else {
            " xPST cannot publish this file type yet."
        };
        return format!("{head}.{tail}{advice}");
    }
    format!(
        "{head}. It can be posted to {names}.{advice}",
        names = accepting.join(", ")
    )
}
