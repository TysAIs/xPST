//! The config-aware route plan: who gets this post, in what state, and why.
//!
//! Ported from `xpst/services/route_plan.py` and
//! `xpst/services/post_preflight.py`. This is the assembly the CLI, the MCP
//! server, the HTTP API and the scheduler all consume, so no surface can resolve
//! a different destination set — and it is the object `preflight` prints.
//!
//! Two settled product requirements live here:
//!
//! * **R3 — every platform is optional.** A post targets an explicit set, or the
//!   *format route* ∩ *connected accounts*. There is no implied "all platforms":
//!   an unconnected destination is `skipped_unconnected` (neutral), never a
//!   failure.
//! * **R4 — format-aware routing is the core value.** A plan is resolved from the
//!   capability registry and shown before anything is published. Out-of-envelope
//!   input is refused with the envelope in the message; nothing is truncated to
//!   fit.
//!
//! The module is pure: it reads no configuration, runs no probe and writes
//! nothing. Everything machine-dependent arrives through [`PreflightHost`], which
//! the CLI implements with the real config, credential store, quota ledger and
//! ffprobe. That split is what lets the assembly be tested against a captured
//! fixture without a filesystem.

use crate::content::{
    coerce_content_type, content_profile, media_kind, text_limit, ContentType,
    DestinationContentProfile, MediaKind, MediaTransport,
};
use crate::ingest::ingest_spec;
use crate::plan::state_for_code;
use crate::visibility;

// ── Canonical destination states (R3 rule 4) ────────────────────────────────
// These are the only spellings any surface may emit. `skipped_unconnected` is
// deliberately NOT a failure: a user who has not connected an account has not
// broken anything.
pub const POSTED: &str = "posted";
pub const SKIPPED_UNCONNECTED: &str = "skipped_unconnected";
pub const UNSUPPORTED_FORMAT: &str = "unsupported_format";
pub const REFUSED_BY_COMMUNITY: &str = "refused_by_community";
/// The destination was evaluated and REFUSED an upload-time visibility the caller
/// asked for: nothing was uploaded, and nothing failed.
pub const REFUSED_VISIBILITY: &str = visibility::REFUSED_VISIBILITY_STATE;
/// The destination is cooling down (rate limit) or outside the anti-bot window:
/// nothing was uploaded *yet* and nothing failed.
pub const DEFERRED: &str = "deferred";
pub const FAILED: &str = "failed";

// ── Canonical refusals ──────────────────────────────────────────────────────
pub const NO_CONNECTED_ACCOUNTS_CODE: &str = "NO_CONNECTED_ACCOUNTS";
pub const NO_ROUTE_FOR_FORMAT_CODE: &str = "NO_ROUTE_FOR_FORMAT";
/// The canonical zero-destination guard, stated once and consumed by every
/// surface so no two can word it (or classify it) differently.
pub const NO_DESTINATIONS_CODE: &str = "NO_DESTINATIONS";
pub const NO_DESTINATIONS_MESSAGE: &str = "Choose at least one destination platform.";
pub const CONNECT_COMMAND: &str = "xpst connect {platform}";

/// The command that fixes an unconnected destination, named in messages.
#[must_use]
pub fn connect_command(platform: Option<&str>) -> String {
    match platform {
        Some(platform) => format!("`xpst connect {platform}`"),
        None => "`xpst connect youtube`, `xpst connect x`, …".to_owned(),
    }
}

/// One actionable sentence: nothing is connected, here is the command.
#[must_use]
pub fn no_connected_accounts_message() -> String {
    format!(
        "No platform account is connected, so there is nowhere to publish. Connect one with {} \
         — nothing is connected by default.",
        connect_command(None)
    )
}

/// The format route is empty after intersecting with connected accounts.
#[must_use]
pub fn no_route_for_format_message(content_type: &str, connected: &[String]) -> String {
    let names = if connected.is_empty() {
        "none".to_owned()
    } else {
        connected.join(", ")
    };
    format!(
        "No connected account accepts {content_type} posts (connected: {names}). Connect one with \
         {} that supports {content_type}, or pick the destinations explicitly with --platforms.",
        connect_command(None)
    )
}

// ── Issues ──────────────────────────────────────────────────────────────────

/// Whether an issue blocks the destination or merely advises.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Blocker,
    Warning,
}

impl Severity {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Blocker => "blocker",
            Self::Warning => "warning",
        }
    }
}

/// A stable, machine-readable warning or hard blocker.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreflightIssue {
    pub code: String,
    pub message: String,
    pub severity: Severity,
    pub media_path: Option<String>,
}

impl PreflightIssue {
    #[must_use]
    pub fn blocker(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_owned(),
            message: message.into(),
            severity: Severity::Blocker,
            media_path: None,
        }
    }

    #[must_use]
    pub fn warning(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_owned(),
            message: message.into(),
            severity: Severity::Warning,
            media_path: None,
        }
    }

    #[must_use]
    pub fn with_media_path(mut self, path: impl Into<String>) -> Self {
        self.media_path = Some(path.into());
        self
    }
}

// ── Readiness ───────────────────────────────────────────────────────────────

/// Local-only authentication readiness; no health endpoint is contacted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthReadiness {
    pub ready: bool,
    pub status: String,
    pub detail: String,
    pub auth_mode: String,
}

impl AuthReadiness {
    #[must_use]
    pub fn new(ready: bool, status: &str, detail: impl Into<String>, auth_mode: &str) -> Self {
        Self {
            ready,
            status: status.to_owned(),
            detail: detail.into(),
            auth_mode: auth_mode.to_owned(),
        }
    }

    /// The row `_readiness` reports when readiness checks are switched off.
    #[must_use]
    pub fn unchecked() -> Self {
        Self::new(true, "unchecked", "Readiness checks disabled.", "unknown")
    }
}

/// Read-only quota snapshot for a target platform.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuotaReadiness {
    pub ready: bool,
    pub daily: Option<i64>,
    pub hourly: Option<i64>,
    pub detail: String,
}

impl QuotaReadiness {
    /// The row `_readiness` reports when readiness checks are switched off.
    #[must_use]
    pub fn unchecked() -> Self {
        Self {
            ready: true,
            daily: None,
            hourly: None,
            detail: "Readiness checks disabled.".to_owned(),
        }
    }

    /// `f"Quota exhausted: {remaining}."` — Python renders the dict with `repr`,
    /// so the braces and the `None` are part of the sentence.
    #[must_use]
    pub fn exhausted_detail(&self) -> String {
        format!(
            "Quota exhausted: {{'daily': {}, 'hourly': {}}}.",
            python_optional_int(self.daily),
            python_optional_int(self.hourly)
        )
    }
}

fn python_optional_int(value: Option<i64>) -> String {
    value.map_or_else(|| "None".to_owned(), |number| number.to_string())
}

/// Enabled, auth, and quota gates for one target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlatformReadiness {
    pub enabled: bool,
    pub auth: AuthReadiness,
    pub quota: QuotaReadiness,
}

impl PlatformReadiness {
    #[must_use]
    pub fn ready(&self) -> bool {
        self.enabled && self.auth.ready && self.quota.ready
    }
}

// ── Media ───────────────────────────────────────────────────────────────────

/// One pre-flight check result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Check {
    pub name: String,
    pub status: CheckStatus,
    pub detail: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckStatus {
    Ok,
    Warn,
    Error,
}

impl CheckStatus {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Warn => "warn",
            Self::Error => "error",
        }
    }
}

/// What a media probe established, plus the spec checks it produced.
///
/// The probe document itself never reaches the route-plan payload (only the
/// derived duration/aspect do), so the host hands over the two numbers instead
/// of a JSON tree — which is also what keeps this crate dependency-free.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct MediaVerdict {
    pub checks: Vec<Check>,
    pub duration_seconds: Option<f64>,
    pub aspect_ratio: Option<f64>,
    /// The raw `ffprobe` document, verbatim, when a probe succeeded.
    ///
    /// `MediaReport.to_dict(include_probe=True)` publishes it as-is, so the text
    /// (not a parsed map) is what keeps ffprobe's own key order. `xpst-core` is
    /// dependency-free, so the ordered re-parse happens in the CLI.
    pub probe: Option<String>,
}

/// What will (or would) happen to a source file for one platform.
#[derive(Debug, Clone, PartialEq)]
pub struct TransformPlan {
    pub platform: String,
    pub action: String,
    pub reasons: Vec<String>,
    pub remux_cmd: Option<Vec<String>>,
    pub transcode_summary: Option<String>,
    pub loudness_target_lufs: Option<f64>,
    pub loudness_normalized: bool,
    pub probe_error: Option<String>,
}

impl TransformPlan {
    #[must_use]
    pub fn preserves_bytes(&self) -> bool {
        matches!(self.action.as_str(), "passthrough" | "remux")
    }

    /// The conservative decision: re-encode, because compliance was not verified.
    #[must_use]
    pub fn transcode(platform: &str, reason: impl Into<String>) -> Self {
        Self {
            platform: platform.to_owned(),
            action: "transcode".to_owned(),
            reasons: vec![reason.into()],
            remux_cmd: None,
            transcode_summary: None,
            loudness_target_lufs: None,
            loudness_normalized: false,
            probe_error: None,
        }
    }
}

/// Local file facts, probe/spec results, and transform decision.
#[derive(Debug, Clone, PartialEq)]
pub struct MediaFilePlan {
    pub path: String,
    pub file_type: String,
    pub exists: Option<bool>,
    pub is_file: Option<bool>,
    pub size_bytes: Option<u64>,
    pub duration_seconds: Option<f64>,
    pub aspect_ratio: Option<f64>,
    /// The ingest checks the verdict produced, in order.
    ///
    /// `MediaReport.to_dict()` publishes them as `media_spec.checks`, which is
    /// what `verify-media` reports; the route plan only reads the derived
    /// blockers and warnings.
    pub checks: Vec<Check>,
    /// The raw `ffprobe` document, verbatim (`media_spec.probe`).
    pub probe: Option<String>,
    pub transform: Option<TransformPlan>,
    pub warnings: Vec<PreflightIssue>,
    pub hard_blockers: Vec<PreflightIssue>,
}

impl MediaFilePlan {
    fn empty(path: &str, file_type: &str) -> Self {
        Self {
            path: path.to_owned(),
            file_type: file_type.to_owned(),
            exists: None,
            is_file: None,
            size_bytes: None,
            duration_seconds: None,
            aspect_ratio: None,
            checks: Vec::new(),
            probe: None,
            transform: None,
            warnings: Vec::new(),
            hard_blockers: Vec::new(),
        }
    }
}

// ── Constraints ─────────────────────────────────────────────────────────────

/// The observed-versus-declared numbers a destination's plan carries.
#[derive(Debug, Clone, PartialEq)]
pub struct Constraints {
    pub observed_duration_seconds: Option<f64>,
    pub max_duration_seconds: Option<u32>,
    pub observed_aspect_ratio: Option<f64>,
    pub max_long_edge: u32,
    pub observed_bytes: Option<u64>,
    pub max_bytes: Option<u64>,
    pub containers: Vec<&'static str>,
    pub observed_characters: usize,
    pub max_characters: Option<usize>,
}

impl Constraints {
    #[must_use]
    pub fn empty() -> Self {
        Self {
            observed_duration_seconds: None,
            max_duration_seconds: None,
            observed_aspect_ratio: None,
            max_long_edge: 0,
            observed_bytes: None,
            max_bytes: None,
            containers: Vec::new(),
            observed_characters: 0,
            max_characters: None,
        }
    }
}

// ── The plan objects ────────────────────────────────────────────────────────

/// Normalized plan for one requested destination platform.
#[derive(Debug, Clone, PartialEq)]
pub struct PlatformPlan {
    pub platform: String,
    pub effective_caption: String,
    pub media: Vec<MediaFilePlan>,
    /// `None` for a destination with no ingest spec: Python's unknown-platform
    /// branch returns an empty `constraints` mapping, not a zeroed one.
    pub constraints: Option<Constraints>,
    pub readiness: PlatformReadiness,
    pub warnings: Vec<PreflightIssue>,
    pub hard_blockers: Vec<PreflightIssue>,
}

/// Complete normalized post plan, preserving requested platform order.
#[derive(Debug, Clone, PartialEq)]
pub struct PostPlanResult {
    pub platforms: Vec<PlatformPlan>,
    pub request_blockers: Vec<PreflightIssue>,
}

impl PostPlanResult {
    #[must_use]
    pub fn platform(&self, name: &str) -> Option<&PlatformPlan> {
        self.platforms.iter().find(|plan| plan.platform == name)
    }

    /// `PostPlanResult.ready`: no request blocker and every destination ready.
    #[must_use]
    pub fn ready(&self) -> bool {
        self.request_blockers.is_empty() && self.platforms.iter().all(PlatformPlan::ready)
    }

    /// `PostPlanResult.ok` — the same verdict under the name clients read.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.ready()
    }

    /// Request blockers first, then every destination's, in plan order.
    #[must_use]
    pub fn hard_blockers(&self) -> Vec<&PreflightIssue> {
        self.request_blockers
            .iter()
            .chain(
                self.platforms
                    .iter()
                    .flat_map(|plan| plan.hard_blockers.iter()),
            )
            .collect()
    }

    /// Every destination's warnings, in plan order.
    #[must_use]
    pub fn warnings(&self) -> Vec<&PreflightIssue> {
        self.platforms
            .iter()
            .flat_map(|plan| plan.warnings.iter())
            .collect()
    }

    /// The first hard blocker — the stable `{code, message}` error object.
    #[must_use]
    pub fn error(&self) -> Option<&PreflightIssue> {
        self.hard_blockers().into_iter().next()
    }
}

impl PlatformPlan {
    /// `PlatformPlan.ready`: nothing blocks it and readiness agrees.
    #[must_use]
    pub fn ready(&self) -> bool {
        self.hard_blockers.is_empty() && self.readiness.ready()
    }

    /// `PlatformPlan.transform`: the single-media transform, or `None`.
    ///
    /// Python returns it only when the plan carries exactly one media item, so a
    /// carousel does not report one item's decision as the whole plan's.
    #[must_use]
    pub fn transform(&self) -> Option<&TransformPlan> {
        if self.media.len() == 1 {
            self.media[0].transform.as_ref()
        } else {
            None
        }
    }

    /// `PlatformPlan.transforms`: every media item's transform, in order.
    #[must_use]
    pub fn transforms(&self) -> Vec<&TransformPlan> {
        self.media
            .iter()
            .filter_map(|item| item.transform.as_ref())
            .collect()
    }
}

impl MediaFilePlan {
    /// `MediaFilePlan.ok`: nothing hard-blocks this file.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.hard_blockers.is_empty()
    }
}

/// One destination's verdict in a resolved route plan.
#[derive(Debug, Clone, PartialEq)]
pub struct RouteDestination {
    pub platform: String,
    pub state: String,
    pub reason: String,
    pub content_type: Option<String>,
    pub connected: bool,
    pub envelope: Option<Envelope>,
    pub caption: String,
    /// `None` renders as `{}` — the unknown-destination shape.
    pub constraints: Option<Constraints>,
    pub blockers: Vec<PreflightIssue>,
    pub warnings: Vec<PreflightIssue>,
    pub transform: Option<TransformPlan>,
    pub transforms: Vec<TransformPlan>,
    pub visibility: Option<visibility::PlanEntry>,
}

impl RouteDestination {
    #[must_use]
    pub fn will_post(&self) -> bool {
        self.state == POSTED
    }

    #[must_use]
    pub fn is_failure(&self) -> bool {
        self.state == FAILED
    }

    #[must_use]
    pub fn connect_command(&self) -> Option<String> {
        if self.connected {
            None
        } else {
            Some(CONNECT_COMMAND.replace("{platform}", &self.platform))
        }
    }
}

/// A resolved, per-destination plan — the object post/schedule consume.
#[derive(Debug, Clone, PartialEq)]
pub struct RoutePlan {
    pub content_type: Option<String>,
    pub source: String,
    pub destinations: Vec<RouteDestination>,
    pub errors: Vec<Reason>,
    pub request_blockers: Vec<Reason>,
}

/// A stable `{code, message}` pair, in the order the payload carries it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reason {
    pub code: String,
    pub message: String,
}

impl Reason {
    #[must_use]
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_owned(),
            message: message.into(),
        }
    }
}

impl RoutePlan {
    /// The destinations a live run should attempt, in plan order.
    #[must_use]
    pub fn posting_destinations(&self) -> Vec<&str> {
        self.destinations
            .iter()
            .filter(|item| item.will_post())
            .map(|item| item.platform.as_str())
            .collect()
    }

    #[must_use]
    pub fn skipped_unconnected(&self) -> Vec<&str> {
        self.with_state(SKIPPED_UNCONNECTED)
    }

    #[must_use]
    pub fn unsupported(&self) -> Vec<&str> {
        self.with_state(UNSUPPORTED_FORMAT)
    }

    #[must_use]
    pub fn failed(&self) -> Vec<&str> {
        self.with_state(FAILED)
    }

    fn with_state(&self, state: &str) -> Vec<&str> {
        self.destinations
            .iter()
            .filter(|item| item.state == state)
            .map(|item| item.platform.as_str())
            .collect()
    }

    /// The one error a caller must act on (no destinations at all).
    #[must_use]
    pub fn actionable_error(&self) -> Option<&Reason> {
        self.errors
            .first()
            .or_else(|| self.request_blockers.first())
    }

    /// Whether at least one destination will be posted to.
    #[must_use]
    pub fn ready(&self) -> bool {
        !self.posting_destinations().is_empty()
    }

    #[must_use]
    pub fn ok(&self) -> bool {
        self.ready() && self.errors.is_empty()
    }
}

// ── The envelope document ───────────────────────────────────────────────────

/// A number that keeps Python's int/float distinction.
///
/// `destination_envelope()` multiplies a whole-megabyte cap into an `int` and a
/// fractional one (Bluesky's 2,000,000-byte lexicon limit) into a `float`, and
/// `json.dumps` renders those differently (`8388608` vs `2000000.0`). The
/// payload is byte-compared, so the distinction is load-bearing.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Number {
    Int(i64),
    Float(f64),
}

/// The `video` block of a destination envelope.
#[derive(Debug, Clone, PartialEq)]
pub struct VideoEnvelopeDoc {
    pub max_duration_seconds: Option<u32>,
    pub containers: Vec<&'static str>,
    pub video_codec: &'static str,
    pub pix_fmt: &'static str,
    pub max_bytes: Option<u64>,
    pub max_long_edge: u32,
    pub fps_cap: u32,
    pub audio_codec: &'static str,
}

/// The `image` block of a destination envelope.
#[derive(Debug, Clone, PartialEq)]
pub struct ImageEnvelopeDoc {
    pub containers: Vec<&'static str>,
    pub max_bytes: Option<Number>,
    pub aspect_min: Option<f64>,
    pub aspect_max: Option<f64>,
}

/// `xpst.content.destination_envelope()` — the publishing envelope for one
/// destination, or `None` if unknown.
#[derive(Debug, Clone, PartialEq)]
pub struct Envelope {
    pub platform: String,
    pub display_name: String,
    pub publishing: bool,
    /// Sorted, as Python's `sorted(item.value for item in profile.implemented)`.
    pub implemented: Vec<&'static str>,
    pub declared: Vec<&'static str>,
    pub caption_max_characters: Option<usize>,
    pub media_transport: &'static str,
    pub media_transport_requirement: Option<String>,
    pub video: Option<VideoEnvelopeDoc>,
    pub image: Option<ImageEnvelopeDoc>,
}

/// Build the envelope document for a destination, or `None` when unknown.
///
/// Reads the **Python** implemented set: a plan must describe what the shipped
/// engine would do, not what this workspace can execute.
#[must_use]
pub fn destination_envelope(platform: &str) -> Option<Envelope> {
    let profile = content_profile(platform)?;
    let spec = ingest_spec(profile.platform);
    let mut implemented: Vec<&'static str> = profile
        .python_implemented
        .iter()
        .map(|item| item.as_str())
        .collect();
    implemented.sort_unstable();
    let mut declared: Vec<&'static str> =
        profile.declared.iter().map(|item| item.as_str()).collect();
    declared.sort_unstable();

    let mut video = None;
    let mut image = None;
    if let Some(spec) = spec {
        if implemented.contains(&ContentType::Video.as_str()) {
            video = Some(VideoEnvelopeDoc {
                max_duration_seconds: spec.duration_cap_s,
                containers: spec.video_containers.to_vec(),
                video_codec: spec.video_codec,
                pix_fmt: spec.pix_fmt,
                max_bytes: spec.video_file_size_cap_bytes,
                max_long_edge: spec.video_long_edge,
                fps_cap: spec.fps_cap,
                audio_codec: spec.audio_codec,
            });
        }
        if implemented.contains(&ContentType::Image.as_str()) {
            image = Some(ImageEnvelopeDoc {
                containers: spec.image_containers.to_vec(),
                max_bytes: spec.image_max_bytes.map(|bytes| {
                    // Python: `image_mb * 1024 * 1024`, where `image_mb` is a
                    // float only when the cap is not a whole number of MiB.
                    if bytes % (1024 * 1024) == 0 {
                        Number::Int(bytes as i64)
                    } else {
                        Number::Float(bytes as f64)
                    }
                }),
                aspect_min: spec.image_aspect_min,
                aspect_max: spec.image_aspect_max,
            });
        }
    }

    Some(Envelope {
        platform: profile.platform.to_owned(),
        display_name: profile.display_name.to_owned(),
        publishing: profile.publishing,
        implemented,
        declared,
        caption_max_characters: text_limit(profile.platform),
        media_transport: profile.media_transport.as_str(),
        media_transport_requirement: media_transport_requirement(profile),
        video,
        image,
    })
}

fn media_transport_requirement(profile: &DestinationContentProfile) -> Option<String> {
    if profile.media_transport != MediaTransport::PublicUrl {
        return None;
    }
    let text = match profile.platform {
        "threads" => crate::content::THREADS_PUBLIC_URL_REQUIREMENT,
        _ => "",
    };
    if text.is_empty() {
        None
    } else {
        Some(text.to_owned())
    }
}

/// The stable code a destination reports when it cannot take a local file.
///
/// Python's `DestinationContentProfile.media_transport_error_code` carries a
/// value on every row (the generic code is the field default), so this returns
/// one for every known destination; only a destination that fetches media itself
/// ever reports it.
#[must_use]
pub fn media_transport_error_code(platform: &str) -> Option<&'static str> {
    let profile = content_profile(platform)?;
    Some(match profile.platform {
        "threads" => "THREADS_NEEDS_URL",
        _ => crate::content::MEDIA_TRANSPORT_ERROR_CODE,
    })
}

// ── Envelope summary and caption refusal ────────────────────────────────────

/// Byte ceiling in the largest whole unit (MB/GB/TB), for messages.
fn human_bytes(value: u64) -> Option<String> {
    if value == 0 {
        return None;
    }
    for (unit, size) in [
        ("TB", 1024_u64.pow(4)),
        ("GB", 1024_u64.pow(3)),
        ("MB", 1024_u64.pow(2)),
    ] {
        if value >= size {
            let amount = value as f64 / size as f64;
            return Some(if amount >= 10.0 {
                format!("{amount:.0} {unit}")
            } else {
                format!("{amount:.1} {unit}")
            });
        }
    }
    Some(format!("{value} B"))
}

fn upper_slash_join(containers: &[&str]) -> String {
    containers
        .iter()
        .map(|item| item.trim_start_matches('.').to_ascii_uppercase())
        .collect::<Vec<_>>()
        .join("/")
}

/// A float as Python's `str()` renders it: shortest round-trip, but an integral
/// value keeps its `.0`.
#[must_use]
pub fn python_float(value: f64) -> String {
    let text = format!("{value}");
    if text.contains('.') || text.contains('e') || text.contains("inf") || text.contains("NaN") {
        text
    } else {
        format!("{text}.0")
    }
}

/// One-line, human-readable envelope for a destination.
///
/// Used verbatim in refusal messages ("the message carries the envelope", R4
/// requirement 4) so a user learns the limit from the refusal instead of
/// guessing, and in the plan output.
#[must_use]
pub fn envelope_summary(platform: &str, content_type: Option<&str>) -> String {
    let Some(envelope) = destination_envelope(platform) else {
        return format!("{platform}: unknown destination (no declared envelope)");
    };
    let wanted = content_type.unwrap_or("").trim().to_ascii_lowercase();
    let mut parts: Vec<String> = Vec::new();
    // A destination asked for a format it does not take still gets its real
    // envelope shown (that is the point of refusing with the envelope), so a
    // text post aimed at a video-only destination names the video limits.
    let show_video = envelope.video.is_some()
        && (wanted == "video" || wanted == "carousel" || wanted != "image");
    if show_video {
        let video = envelope.video.as_ref().expect("checked above");
        let mut caps: Vec<String> = Vec::new();
        if let Some(duration) = video.max_duration_seconds {
            caps.push(format!("≤{duration}s"));
        }
        if !video.containers.is_empty() {
            caps.push(upper_slash_join(&video.containers));
        }
        if !video.video_codec.is_empty() {
            caps.push(video.video_codec.to_owned());
        }
        if let Some(bytes) = video.max_bytes {
            if let Some(size) = human_bytes(bytes) {
                caps.push(format!("≤{size}"));
            }
        }
        parts.push(format!("video {}", caps.join(", ")));
    }
    if wanted == "image" {
        if let Some(image) = envelope.image.as_ref() {
            let mut caps: Vec<String> = Vec::new();
            if !image.containers.is_empty() {
                caps.push(upper_slash_join(&image.containers));
            }
            if let Some(bytes) = image.max_bytes {
                let value = match bytes {
                    Number::Int(value) => value as u64,
                    Number::Float(value) => value as u64,
                };
                if let Some(size) = human_bytes(value) {
                    caps.push(format!("≤{size}"));
                }
            }
            if let (Some(min), Some(max)) = (image.aspect_min, image.aspect_max) {
                caps.push(format!(
                    "aspect {}:1–{}:1",
                    python_float(min),
                    python_float(max)
                ));
            }
            parts.push(format!("image {}", caps.join(", ")));
        }
    }
    if let Some(caption) = envelope.caption_max_characters {
        parts.push(format!("caption ≤{caption} chars"));
    }
    if parts.is_empty() {
        let list = if envelope.implemented.is_empty() {
            "nothing".to_owned()
        } else {
            envelope.implemented.join(", ")
        };
        return format!("{platform}: {list}");
    }
    format!("{platform}: {}", parts.join(", "))
}

/// The refusal message when a caption exceeds a destination's limit.
///
/// Refusing (not truncating) is the contract: the message names both numbers and
/// the destination's caption envelope, so the caller can shorten once instead of
/// discovering the limit per platform.
#[must_use]
pub fn text_refusal(platform: &str, caption: &str) -> Option<String> {
    let limit = text_limit(platform)?;
    let length = caption.chars().count();
    if length <= limit {
        return None;
    }
    Some(format!(
        "{platform} accepts {limit} characters and this caption is {length}; xPST refuses rather \
         than truncates. Shorten it to {limit} characters or use --caption-for \
         {platform}=<text>. Envelope — {}.",
        envelope_summary(platform, Some("text"))
    ))
}

// ── Target resolution (R3) ──────────────────────────────────────────────────

/// The destination set a request resolves to, and where it came from.
#[derive(Debug, Clone, PartialEq)]
pub struct TargetResolution {
    pub targets: Vec<String>,
    /// `"explicit"`, `"all"` or `"format_route"`.
    pub source: String,
    pub content_type: Option<String>,
    pub errors: Vec<Reason>,
}

/// Normalize an explicit target list (lowercase, deduped, order preserved).
#[must_use]
pub fn normalize_platforms(requested: Option<&[String]>) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    for platform in requested.unwrap_or(&[]) {
        let name = platform.trim().to_ascii_lowercase();
        if !name.is_empty() && !seen.contains(&name) {
            seen.push(name);
        }
    }
    seen
}

/// Destinations whose *implemented* capability accepts `content_type`.
///
/// This is the format half of the R4 route (`format route ∩ connected
/// accounts`), derived from the capability table — never a second table.
#[must_use]
pub fn format_route(content_type: &str) -> Vec<String> {
    let Ok(wanted) = coerce_content_type(content_type) else {
        return Vec::new();
    };
    crate::content::PUBLISH_DESTINATIONS
        .iter()
        .filter(|platform| {
            content_profile(platform)
                .is_some_and(|profile| profile.python_implemented.contains(&wanted))
        })
        .map(|platform| (*platform).to_owned())
        .collect()
}

/// Normalize a caller-stated content type: `None`/`"auto"`/empty → `None`.
#[must_use]
pub fn normalize_content_type(content_type: Option<&str>) -> Option<String> {
    let raw = content_type.unwrap_or("").trim().to_ascii_lowercase();
    if raw.is_empty() || raw == "auto" {
        return None;
    }
    match coerce_content_type(&raw) {
        Ok(value) => Some(value.as_str().to_owned()),
        Err(_) => Some(raw),
    }
}

/// Classify the requested format.
///
/// A caller-stated type wins (`--content-type`), an explicit `--format` wins over
/// inference, and otherwise the media list decides exactly as the content
/// contract does. `"auto"` (and an absent value) means "classify it".
#[must_use]
pub fn detect_content_type(media_paths: &[String], content_type: Option<&str>) -> String {
    let raw = content_type.unwrap_or("").trim().to_ascii_lowercase();
    if !raw.is_empty() && raw != "auto" {
        return match coerce_content_type(&raw) {
            Ok(value) => value.as_str().to_owned(),
            Err(_) => raw,
        };
    }
    let items: Vec<&String> = media_paths
        .iter()
        .filter(|item| !item.trim().is_empty())
        .collect();
    match items.len() {
        0 => ContentType::Text.as_str().to_owned(),
        1 => {
            if media_kind(items[0]) == MediaKind::Image {
                ContentType::Image.as_str().to_owned()
            } else {
                ContentType::Video.as_str().to_owned()
            }
        }
        _ => ContentType::Carousel.as_str().to_owned(),
    }
}

/// Resolve the destination set for a post, with no implied set.
///
/// * `requested` (an explicit `--platforms` argument) wins, including names that
///   are not connected — the plan then marks those `skipped_unconnected` instead
///   of dropping them.
/// * `all_connected` (the explicit `--all`) means every connected account that
///   accepts this format.
/// * otherwise the route is `format route ∩ connected accounts`.
///
/// Zero connected accounts produces ONE actionable error naming the connect
/// command (R3 rule 5); so does a format no connected account accepts.
#[must_use]
pub fn resolve_targets(
    connected: &[String],
    requested: Option<&[String]>,
    all_connected: bool,
    content_type: Option<&str>,
) -> TargetResolution {
    let content_type_value = normalize_content_type(content_type);
    let explicit = normalize_platforms(requested);
    if !explicit.is_empty() {
        return TargetResolution {
            targets: explicit,
            source: "explicit".to_owned(),
            content_type: content_type_value,
            errors: Vec::new(),
        };
    }
    if requested.is_some() {
        // A list was given but held no usable name (`--platforms ","`): that is a
        // malformed request, not "nothing is connected".
        return TargetResolution {
            targets: Vec::new(),
            source: "explicit".to_owned(),
            content_type: content_type_value,
            errors: vec![Reason::new(NO_DESTINATIONS_CODE, NO_DESTINATIONS_MESSAGE)],
        };
    }
    if connected.is_empty() {
        return TargetResolution {
            targets: Vec::new(),
            source: "format_route".to_owned(),
            content_type: content_type_value,
            errors: vec![Reason::new(
                NO_CONNECTED_ACCOUNTS_CODE,
                no_connected_accounts_message(),
            )],
        };
    }
    let route = match content_type_value.as_deref() {
        Some(value) => format_route(value),
        None => connected.to_vec(),
    };
    let targets: Vec<String> = route
        .into_iter()
        .filter(|platform| connected.contains(platform))
        .collect();
    if targets.is_empty() {
        let message =
            no_route_for_format_message(content_type_value.as_deref().unwrap_or("this"), connected);
        return TargetResolution {
            targets: Vec::new(),
            source: if all_connected { "all" } else { "format_route" }.to_owned(),
            content_type: content_type_value,
            errors: vec![Reason::new(NO_ROUTE_FOR_FORMAT_CODE, message)],
        };
    }
    TargetResolution {
        targets,
        source: if all_connected { "all" } else { "format_route" }.to_owned(),
        content_type: content_type_value,
        errors: Vec::new(),
    }
}

// ── The host seam ───────────────────────────────────────────────────────────

/// Everything machine-dependent the assembly needs, supplied by the caller.
///
/// The CLI implements this with the real config, credential store, quota ledger
/// and ffprobe; a test implements it with a fixture. Nothing in this module reads
/// a file, so the assembly itself is deterministic.
pub trait PreflightHost {
    /// The per-account opt-in flag (`accounts.<platform>.enabled`).
    fn enabled(&self, platform: &str) -> bool;
    /// Local-only auth readiness for one destination.
    fn auth(&self, platform: &str) -> AuthReadiness;
    /// Read-only quota snapshot for one destination.
    fn quota(&self, platform: &str) -> QuotaReadiness;
    /// Probe a local file and run the destination's ingest checks.
    fn verify_media(&self, path: &str, platform: &str, check_loudness: bool) -> MediaVerdict;
    /// The rendition decision for one file at one destination.
    fn transform(&self, path: &str, platform: &str) -> TransformPlan;
    /// The ban-risk advisory a destination on an unofficial auth path carries.
    fn risk_advisory(&self, platform: &str) -> Option<PreflightIssue>;
    /// The TikTok route inputs `publish_route` reads.
    fn tiktok_route(&self) -> Option<visibility::TiktokRoute>;
}

/// One request to plan, already normalized by the CLI.
#[derive(Debug, Clone, Default)]
pub struct PlanRequest {
    pub media_paths: Vec<String>,
    pub caption: String,
    pub per_platform_captions: Vec<(String, String)>,
    /// The caller's `--format` value (`None`/`"auto"` to classify).
    pub content_type: Option<String>,
    pub requested: Option<Vec<String>>,
    pub all_connected: bool,
    pub include_transform: bool,
    pub include_readiness: bool,
    pub probe_media: bool,
    pub visibility: Option<String>,
    /// Run the ffmpeg loudness measurement as part of each media verdict.
    ///
    /// `false` for the route plan (Python's `build_route_plan` passes
    /// `check_loudness=False`, so `preflight` never spawns ffmpeg twice) and
    /// `true` for `verify-media`, whose `PostPlanRequest` defaults it on.
    pub check_loudness: bool,
}

impl PlanRequest {
    fn override_for(&self, platform: &str) -> Option<&str> {
        self.per_platform_captions
            .iter()
            .find(|(name, _)| name == platform)
            .map(|(_, value)| value.as_str())
    }
}

/// Whether a content type requires media (the pre-contract default: yes).
///
/// An unrecognized or absent value keeps the legacy behaviour — a plan without a
/// file is blocked — so a typo can never turn into a media-less post.
#[must_use]
pub fn content_needs_media(content_type: Option<&str>) -> bool {
    let Some(value) = content_type else {
        return true;
    };
    match coerce_content_type(value) {
        Ok(kind) => !matches!(kind, ContentType::Text | ContentType::Thread),
        Err(_) => true,
    }
}

/// Whether a path is a remote URL (`http`/`https` with a host).
fn is_url(value: &str) -> bool {
    let Some((scheme, rest)) = value.split_once("://") else {
        return false;
    };
    if !matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https") {
        return false;
    }
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    !authority.is_empty()
}

/// `Path.expanduser()`: a leading `~` becomes `$HOME`.
fn expand_home(path: &str) -> String {
    if path == "~" {
        return std::env::var("HOME").unwrap_or_else(|_| path.to_owned());
    }
    if let Some(rest) = path.strip_prefix("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return format!("{home}/{rest}");
        }
    }
    path.to_owned()
}

// ── The assembly ────────────────────────────────────────────────────────────

/// Resolve the full plan for one request without uploading anything.
#[must_use]
pub fn build_route_plan(host: &dyn PreflightHost, request: &PlanRequest) -> RoutePlan {
    let detected = detect_content_type(&request.media_paths, request.content_type.as_deref());
    let connected = connected_destinations(host);
    let resolution = resolve_targets(
        &connected,
        request.requested.as_deref(),
        request.all_connected,
        Some(&detected),
    );
    if resolution.targets.is_empty() {
        return RoutePlan {
            content_type: Some(detected),
            source: resolution.source,
            destinations: Vec::new(),
            errors: resolution.errors,
            request_blockers: Vec::new(),
        };
    }

    let plan_media: Vec<String> = if request.probe_media {
        request.media_paths.clone()
    } else {
        Vec::new()
    };
    let plan = plan_post(
        host,
        &PlanRequest {
            media_paths: plan_media,
            content_type: Some(detected.clone()),
            // Python's `build_route_plan` pins this off: a route plan never
            // pays for the loudness subprocess, whatever the caller asked for.
            check_loudness: false,
            ..request.clone()
        },
        &resolution.targets,
    );

    let mut destinations: Vec<RouteDestination> = Vec::new();
    for platform in &resolution.targets {
        let platform_plan = plan.platform(platform);
        let profile = content_profile(platform);
        let connected = profile.is_some() && host.enabled(platform);
        let envelope = destination_envelope(platform);
        let effective_caption = request
            .override_for(platform)
            .unwrap_or(&request.caption)
            .to_owned();

        let mut blockers: Vec<PreflightIssue> = platform_plan
            .map(|plan| plan.hard_blockers.clone())
            .unwrap_or_default()
            .into_iter()
            .filter(|issue| {
                // A probe-less plan must not claim "media required" for a request
                // that carried files; the media verdict is simply unprobed.
                !(!request.probe_media && issue.code == "MEDIA_REQUIRED")
                    // "Connected" is the per-account opt-in flag (handled above);
                    // an enabled account whose credential looks missing is still
                    // attempted so the uploader reports the authoritative auth
                    // error. Quota is an execution-time gate with its own exit
                    // code. Both are reported as warnings, never as a skip.
                    && issue.code != "QUOTA_NOT_READY"
                    && issue.code != "AUTH_NOT_READY"
            })
            .collect();

        let mut warnings: Vec<PreflightIssue> = platform_plan
            .map(|plan| plan.warnings.clone())
            .unwrap_or_default();
        warnings.extend(
            platform_plan
                .map(|plan| plan.hard_blockers.clone())
                .unwrap_or_default()
                .into_iter()
                .filter(|issue| issue.code == "QUOTA_NOT_READY" || issue.code == "AUTH_NOT_READY"),
        );
        // A destination whose auth path is not the provider's official API says
        // so here. Advisories (not blockers): preflight, `post --dry-run`, the
        // HTTP planner and MCP all render this, so the ban risk reaches the human
        // at the moment of choosing.
        if let Some(advisory) = host.risk_advisory(platform) {
            warnings.push(advisory);
        }

        let transforms: Vec<TransformPlan> = platform_plan
            .map(|plan| {
                plan.media
                    .iter()
                    .filter_map(|item| item.transform.clone())
                    .collect()
            })
            .unwrap_or_default();

        let mut state = POSTED.to_owned();
        let mut reason = "will publish".to_owned();
        match profile {
            // A destination xPST has no capability profile for is refused as
            // unknown, never as capable and never as unconfigured.
            None => {
                state = UNSUPPORTED_FORMAT.to_owned();
                reason = format!("{platform} is not a known publishing destination.");
            }
            Some(known) => {
                if !connected {
                    state = SKIPPED_UNCONNECTED.to_owned();
                    reason = format!(
                        "{platform} is not connected in local configuration. Connect it with {}.",
                        connect_command(Some(platform))
                    );
                } else if !supports_content(known, &detected) {
                    state = UNSUPPORTED_FORMAT.to_owned();
                    reason = format!(
                        "{platform} cannot publish {detected} posts (supports: {}). Envelope — {}.",
                        supported_types(known),
                        envelope_summary(platform, Some(&detected))
                    );
                } else if !blockers.is_empty() {
                    state = state_for_code(&blockers[0].code).as_str().to_owned();
                    reason = blockers
                        .iter()
                        .map(|issue| issue.message.as_str())
                        .collect::<Vec<_>>()
                        .join("; ");
                    let caption_refusal = text_refusal(platform, &effective_caption);
                    if blockers
                        .iter()
                        .any(|issue| issue.code == "CAPTION_TOO_LONG")
                    {
                        if let Some(refusal) = caption_refusal {
                            // Refusal carries the envelope (R4 requirement 4)
                            // instead of a bare "too long" the caller has to
                            // decode.
                            reason = refusal;
                        }
                    }
                    if state == SKIPPED_UNCONNECTED {
                        reason.push_str(&format!(
                            " Connect with {}.",
                            connect_command(Some(platform))
                        ));
                    }
                    if state == UNSUPPORTED_FORMAT {
                        reason = format!(
                            "{reason} Envelope — {}.",
                            envelope_summary(platform, Some(&detected))
                        );
                    }
                }
            }
        }

        let route = visibility::publish_route(
            host.tiktok_route().as_ref(),
            platform,
            request.visibility.as_deref(),
        );
        let visibility_entry =
            visibility::plan_entry(platform, request.visibility.as_deref(), route, None, None);

        let transform = if transforms.len() == 1 {
            transforms.first().cloned()
        } else {
            None
        };
        let multi = if transforms.len() > 1 {
            transforms.clone()
        } else {
            Vec::new()
        };

        destinations.push(RouteDestination {
            platform: platform.clone(),
            state,
            reason,
            content_type: Some(detected.clone()),
            connected,
            envelope,
            caption: effective_caption,
            constraints: platform_plan.and_then(|plan| plan.constraints.clone()),
            blockers: std::mem::take(&mut blockers),
            warnings,
            transform,
            transforms: multi,
            visibility: visibility_entry,
        });
    }

    RoutePlan {
        content_type: Some(detected),
        source: resolution.source,
        destinations,
        errors: resolution.errors,
        request_blockers: plan
            .request_blockers
            .iter()
            .map(|issue| Reason::new(&issue.code, issue.message.clone()))
            .collect(),
    }
}

fn supports_content(profile: &DestinationContentProfile, content_type: &str) -> bool {
    match coerce_content_type(content_type) {
        Ok(kind) => profile.python_implemented.contains(&kind),
        Err(_) => false,
    }
}

fn supported_types(profile: &DestinationContentProfile) -> String {
    let mut values: Vec<&str> = profile
        .python_implemented
        .iter()
        .map(|item| item.as_str())
        .collect();
    values.sort_unstable();
    values.join(", ")
}

/// Publishing destinations enabled (connected) in local configuration.
///
/// `enabled` is xPST's "this account is connected and may publish" flag. A
/// destination that is enabled but has no usable credential is still listed here;
/// the plan reports it with the `AUTH_NOT_READY` advisory rather than pretending
/// it will publish.
#[must_use]
pub fn connected_destinations(host: &dyn PreflightHost) -> Vec<String> {
    crate::content::PUBLISH_DESTINATIONS
        .iter()
        .filter(|platform| host.enabled(platform))
        .map(|platform| (*platform).to_owned())
        .collect()
}

/// Build the per-destination plan for an explicit target list.
///
/// This is `PostPreflightService.plan()`: unknown platforms are represented by a
/// hard-blocked plan rather than being dropped, and a request with no destination
/// at all is refused once, here.
#[must_use]
pub fn plan_post(
    host: &dyn PreflightHost,
    request: &PlanRequest,
    targets: &[String],
) -> PostPlanResult {
    let mut platforms: Vec<PlatformPlan> = Vec::new();
    for platform in targets {
        platforms.push(plan_platform(host, platform, request));
    }
    let mut request_blockers: Vec<PreflightIssue> = Vec::new();
    if targets.iter().all(|platform| platform.trim().is_empty()) {
        request_blockers.push(PreflightIssue::blocker(
            NO_DESTINATIONS_CODE,
            NO_DESTINATIONS_MESSAGE,
        ));
    }
    PostPlanResult {
        platforms,
        request_blockers,
    }
}

fn plan_platform(host: &dyn PreflightHost, platform: &str, request: &PlanRequest) -> PlatformPlan {
    let caption = request
        .override_for(platform)
        .unwrap_or(&request.caption)
        .to_owned();
    let mut platform_issues: Vec<PreflightIssue> = Vec::new();

    if ingest_spec(platform).is_none() {
        platform_issues.push(PreflightIssue::blocker(
            "UNKNOWN_PLATFORM",
            format!("No media preflight specification exists for '{platform}'."),
        ));
        let readiness = readiness(host, platform, request.include_readiness);
        return PlatformPlan {
            platform: platform.to_owned(),
            effective_caption: caption,
            media: Vec::new(),
            constraints: None,
            readiness,
            warnings: Vec::new(),
            hard_blockers: platform_issues,
        };
    }

    let readiness = readiness(host, platform, request.include_readiness);
    if request.include_readiness {
        if !readiness.enabled {
            platform_issues.push(PreflightIssue::blocker(
                "PLATFORM_DISABLED",
                format!("{platform} is disabled in local configuration."),
            ));
        }
        if !readiness.auth.ready {
            platform_issues.push(PreflightIssue::blocker(
                "AUTH_NOT_READY",
                readiness.auth.detail.clone(),
            ));
        }
        if !readiness.quota.ready {
            platform_issues.push(PreflightIssue::blocker(
                "QUOTA_NOT_READY",
                readiness.quota.detail.clone(),
            ));
        }
    }

    let mut media_plans: Vec<MediaFilePlan> = Vec::new();
    for raw_path in &request.media_paths {
        let media_plan = plan_media(host, raw_path, platform, request);
        platform_issues.extend(media_plan.hard_blockers.clone());
        media_plans.push(media_plan);
    }

    // Upload-time visibility: a level the route that would actually run cannot
    // honour is a REFUSAL, because publishing anyway is a public post the caller
    // did not ask for. A destination with no such mode on any route is an
    // advisory: it still publishes, and the plan says it will be public.
    let mut platform_warnings: Vec<PreflightIssue> = Vec::new();
    if let Some(requested) = visibility::normalize(request.visibility.as_deref()) {
        let route =
            visibility::publish_route(host.tiktok_route().as_ref(), platform, Some(&requested));
        match visibility::refusal(platform, Some(&requested), route) {
            Some(refused) => platform_issues.push(PreflightIssue::blocker(
                visibility::VISIBILITY_UNAVAILABLE_CODE,
                refused,
            )),
            None => {
                if let Some(note) = visibility::advisory(platform, Some(&requested), route) {
                    platform_warnings.push(PreflightIssue::warning(
                        visibility::VISIBILITY_UNSUPPORTED_CODE,
                        note,
                    ));
                }
            }
        }
    }

    if request.media_paths.is_empty() && content_needs_media(request.content_type.as_deref()) {
        platform_issues.push(PreflightIssue::blocker(
            "MEDIA_REQUIRED",
            "At least one media path is required.",
        ));
    }

    // One source for the limit: the same number the sender refuses on, so a plan
    // and a publish cannot disagree.
    if let Some(limit) = text_limit(platform) {
        let length = caption.chars().count();
        if length > limit {
            platform_issues.push(PreflightIssue::blocker(
                "CAPTION_TOO_LONG",
                format!("Caption is {length} characters; {platform} allows {limit}."),
            ));
        }
    }

    let mut warnings = platform_warnings;
    for item in &media_plans {
        warnings.extend(item.warnings.clone());
    }
    let constraints = constraints(platform, &media_plans, &caption);

    PlatformPlan {
        platform: platform.to_owned(),
        effective_caption: caption,
        media: media_plans,
        constraints: Some(constraints),
        readiness,
        warnings,
        hard_blockers: platform_issues,
    }
}

fn readiness(host: &dyn PreflightHost, platform: &str, enabled: bool) -> PlatformReadiness {
    if !enabled {
        return PlatformReadiness {
            enabled: true,
            auth: AuthReadiness::unchecked(),
            quota: QuotaReadiness::unchecked(),
        };
    }
    let is_enabled = host.enabled(platform);
    let auth = host.auth(platform);
    let mut quota = host.quota(platform);
    if ingest_spec(platform).is_some() {
        if quota.ready {
            quota.detail = "Quota available.".to_owned();
        } else {
            quota.detail = quota.exhausted_detail();
        }
    } else {
        quota = QuotaReadiness {
            ready: true,
            daily: None,
            hourly: None,
            detail: "No quota limit is configured.".to_owned(),
        };
    }
    PlatformReadiness {
        enabled: is_enabled,
        auth,
        quota,
    }
}

fn plan_media(
    host: &dyn PreflightHost,
    raw_path: &str,
    platform: &str,
    request: &PlanRequest,
) -> MediaFilePlan {
    let display_path = raw_path;
    if is_url(display_path) {
        // A URL is only usable by a destination that fetches media itself; every
        // other uploader needs the bytes and would be handed a string it cannot
        // open. The answer comes from the destination's declared media transport.
        let profile = content_profile(platform);
        if profile.is_none_or(|profile| profile.media_transport != MediaTransport::PublicUrl) {
            let issue = PreflightIssue::blocker(
                "REMOTE_MEDIA_UNSUPPORTED",
                "Only a local media path is supported for this platform.",
            )
            .with_media_path(display_path);
            let mut plan = MediaFilePlan::empty(display_path, "remote_url");
            plan.hard_blockers.push(issue);
            return plan;
        }
        let warning = PreflightIssue::warning(
            "MEDIA_REMOTE_NOT_PROBED",
            "Remote media was not fetched or probed; no network call is made during planning.",
        )
        .with_media_path(display_path);
        let mut plan = MediaFilePlan::empty(display_path, "remote_url");
        plan.warnings.push(warning);
        return plan;
    }

    let path = expand_home(raw_path);
    let metadata = std::fs::metadata(&path);
    let (exists, is_file) = match &metadata {
        Ok(metadata) => (true, metadata.is_file()),
        Err(error) => {
            if error.kind() == std::io::ErrorKind::NotFound {
                (false, false)
            } else {
                let issue = PreflightIssue::blocker(
                    "MEDIA_STAT_FAILED",
                    format!("Could not inspect media path: {error}"),
                )
                .with_media_path(display_path);
                let mut plan = MediaFilePlan::empty(display_path, "unavailable");
                plan.exists = Some(false);
                plan.is_file = Some(false);
                plan.hard_blockers.push(issue);
                return plan;
            }
        }
    };

    if !exists {
        let issue = PreflightIssue::blocker(
            "MEDIA_NOT_FOUND",
            format!("Media path does not exist: {display_path}"),
        )
        .with_media_path(display_path);
        let mut plan = MediaFilePlan::empty(display_path, "missing");
        plan.exists = Some(false);
        plan.is_file = Some(false);
        plan.hard_blockers.push(issue);
        return plan;
    }
    if !is_file {
        let issue = PreflightIssue::blocker(
            "MEDIA_DIRECTORY",
            format!("Media path is not a regular file: {display_path}"),
        )
        .with_media_path(display_path);
        let mut plan = MediaFilePlan::empty(display_path, "directory");
        plan.exists = Some(true);
        plan.is_file = Some(false);
        plan.hard_blockers.push(issue);
        return plan;
    }

    let size_bytes = metadata.as_ref().ok().map(std::fs::Metadata::len);
    if size_bytes == Some(0) {
        let issue = PreflightIssue::blocker(
            "MEDIA_EMPTY",
            format!("Media file is empty: {display_path}"),
        )
        .with_media_path(display_path);
        let mut plan = MediaFilePlan::empty(display_path, "file");
        plan.exists = Some(true);
        plan.is_file = Some(true);
        plan.size_bytes = size_bytes;
        plan.hard_blockers.push(issue);
        return plan;
    }

    let verdict = host.verify_media(&path, platform, request.check_loudness);
    let mut warnings: Vec<PreflightIssue> = Vec::new();
    let mut blockers: Vec<PreflightIssue> = Vec::new();
    let mut modality_blocked = false;
    for check in &verdict.checks {
        if check.name == "modality" && check.status == CheckStatus::Error {
            modality_blocked = true;
        }
        let Some(issue) = issue_from_check(check, display_path) else {
            continue;
        };
        match check.status {
            CheckStatus::Error => blockers.push(issue),
            CheckStatus::Warn => warnings.push(issue),
            CheckStatus::Ok => {}
        }
    }

    // A destination that cannot publish this file's modality at all is already
    // fully explained by the one modality blocker: do not stack a second,
    // unrelated blocker on top.
    if !modality_blocked {
        if let Some((code, message)) = media_transport_blocker(platform, display_path) {
            blockers.push(PreflightIssue::blocker(code, message).with_media_path(display_path));
        }
    }

    let transform = if request.include_transform && !modality_blocked {
        Some(host.transform(&path, platform))
    } else {
        None
    };

    MediaFilePlan {
        path: display_path.to_owned(),
        file_type: "file".to_owned(),
        exists: Some(true),
        is_file: Some(true),
        size_bytes,
        duration_seconds: verdict.duration_seconds,
        aspect_ratio: verdict.aspect_ratio,
        checks: verdict.checks,
        probe: verdict.probe,
        transform,
        warnings,
        hard_blockers: blockers,
    }
}

fn issue_from_check(check: &Check, media_path: &str) -> Option<PreflightIssue> {
    let (code, severity) = match check.status {
        CheckStatus::Error => (
            format!("MEDIA_SPEC_{}", check.name.to_ascii_uppercase()),
            Severity::Blocker,
        ),
        CheckStatus::Warn => (
            if check.name == "probe" {
                "MEDIA_PROBE_FAILED".to_owned()
            } else {
                format!("MEDIA_SPEC_{}", check.name.to_ascii_uppercase())
            },
            Severity::Warning,
        ),
        CheckStatus::Ok => return None,
    };
    // The modality check carries its own plain-language, destination-naming
    // message and is a capability verdict, not a spec comparison — give it a
    // dedicated code so a client can branch on "this destination cannot take this
    // file type" without parsing prose.
    if check.name == "modality" {
        return Some(PreflightIssue {
            code: "MEDIA_MODALITY_UNSUPPORTED".to_owned(),
            message: check.detail.clone(),
            severity: Severity::Blocker,
            media_path: Some(media_path.to_owned()),
        });
    }
    Some(PreflightIssue {
        code,
        message: check.detail.clone(),
        severity,
        media_path: Some(media_path.to_owned()),
    })
}

/// The one refusal for a local file a destination cannot fetch itself.
///
/// Returns `None` when the destination uploads the bytes itself, when there is no
/// media, or when every media item is already a public URL.
#[must_use]
pub fn media_transport_blocker(platform: &str, media_path: &str) -> Option<(&'static str, String)> {
    let profile = content_profile(platform)?;
    if profile.media_transport == MediaTransport::LocalUpload {
        return None;
    }
    if media_path.trim().is_empty() || is_url(media_path) {
        return None;
    }
    let message = media_transport_requirement(profile).unwrap_or_else(|| {
        format!(
            "{} cannot take a local file: it fetches media from a URL you host, and xPST has no \
             server to provide one.",
            profile.display_name
        )
    });
    Some((
        media_transport_error_code(platform).unwrap_or("MEDIA_TRANSPORT"),
        message,
    ))
}

fn constraints(platform: &str, media: &[MediaFilePlan], caption: &str) -> Constraints {
    let Some(spec) = ingest_spec(platform) else {
        return Constraints::empty();
    };
    Constraints {
        observed_duration_seconds: media.iter().find_map(|item| item.duration_seconds),
        max_duration_seconds: spec.duration_cap_s,
        observed_aspect_ratio: media.iter().find_map(|item| item.aspect_ratio),
        max_long_edge: spec.video_long_edge,
        observed_bytes: media.iter().find_map(|item| item.size_bytes),
        max_bytes: spec.video_file_size_cap_bytes,
        containers: spec.video_containers.to_vec(),
        observed_characters: caption.chars().count(),
        max_characters: text_limit(platform),
    }
}

/// The canonical state a stable preflight code maps to, as the payload spells it.
#[must_use]
pub fn state_string(code: &str) -> &'static str {
    state_for_code(code).as_str()
}

/// The destination states the plan can emit, in canonical order.
pub const DESTINATION_STATES: [&str; 7] = [
    POSTED,
    SKIPPED_UNCONNECTED,
    UNSUPPORTED_FORMAT,
    REFUSED_BY_COMMUNITY,
    REFUSED_VISIBILITY,
    DEFERRED,
    FAILED,
];
