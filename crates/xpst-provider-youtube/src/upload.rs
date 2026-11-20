//! The resumable upload protocol, against Google's documented behaviour.
//!
//! There is no maintained crate for this protocol, so it is implemented here
//! from the documentation rather than adapted from someone else's types:
//!
//! 1. **Initiate** — `POST {endpoint}?uploadType=resumable&part=snippet,status`
//!    with the video metadata as JSON. The reply's `Location` header is the
//!    session URI. One call covers both Shorts and long-form uploads; nothing
//!    about the protocol differs between them except the chunk size.
//! 2. **Upload in chunks** — `PUT {session}` with
//!    `Content-Range: bytes {start}-{end}/{total}`. Every chunk except the last
//!    must be a multiple of 256 KiB, which [`UploadPlan`] enforces before the
//!    first byte goes out.
//! 3. **Resume** — `308 Resume Incomplete` means "keep going". Its `Range`
//!    header (`bytes=0-N`, inclusive) says how much the server holds, so the
//!    next request starts at `N+1`. A 308 with no `Range` means nothing landed
//!    and the upload restarts from zero. A chunk whose *response* is lost is
//!    simply re-sent: the server answers 308 with what it already has, which is
//!    why the driver never needs a separate progress probe on that path.
//! 4. **Finalise** — when a 308 reports every byte received, or when the total
//!    is unknown-but-complete, `PUT {session}` with `Content-Range: bytes
//!    */{total}` and an empty body ends the session and returns the video
//!    resource. A 200/201 to any chunk ends it too.
//!
//! Failure classification is deliberately not "status ≠ 200":
//!
//! | Condition | Class | Why |
//! |---|---|---|
//! | 5xx, 429, transport error on a chunk | retryable | transient by definition |
//! | transport error on the finalise request | **unconfirmed** | no response arrived, so the session may have closed and published; re-sending could post a second video |
//! | 403 `quotaExceeded` / `dailyLimitExceeded` / `uploadLimitExceeded` | permanent | the bucket resets tomorrow; retrying today only burns requests |
//! | 403 `rateLimitExceeded` | retryable | Google documents this one as short-term throttling |
//! | 403 anything else, 400 | permanent | policy or a malformed request; a retry re-fails |
//! | 401 | needs re-auth | the token is no good, not the request |
//! | 404/410 | restart only when provably safe, otherwise **unconfirmed** | the session is gone; re-sending every byte can publish a second video |
//!
//! Two rules follow from that table and are not negotiable here:
//!
//! * **A status is a fact only when it was received.** The driver never quotes
//!   an HTTP status it did not see, and never invents an answer to a request
//!   that produced no response. Such an attempt is [`FailureClass::Unconfirmed`]:
//!   the video may be live, so the caller must reconcile — read the destination
//!   back — before retrying, which is the same vocabulary
//!   `xpst_pipeline::outcome::DestinationOutcome::Unconfirmed` uses, for the
//!   same reason.
//! * **A lost session is not a licence to re-upload.** A 404/410 cannot be
//!   resumed, but starting over is only allowed when this adapter can prove
//!   nothing was published: either no content byte was ever put on the wire, or
//!   every content-bearing request got a response and none of them was a
//!   completion. Anything else consults the [`Reconciler`] seam; with no
//!   evidence of non-publication the answer is unconfirmed, never a silent
//!   re-send of the whole file.
//!
//! # Metadata: refuse, never truncate
//!
//! YouTube caps `snippet.title` at 100 characters, `snippet.description` at
//! 5 000 characters and `snippet.tags` at 15 entries. Content past a cap is
//! **refused** with the measured length and the limit in the message — never
//! trimmed, because a trimmed title is silently a different video. The Shorts
//! classification marker ` #Shorts` is part of the payload contract, so its ten
//! characters are reserved *before* the title is measured: a Short's title text
//! gets 90 characters, and no title can lose the marker that makes it a Short.
//!
//! Google answers a spent quota with **403**, not 429, so the *body* has to be
//! read to tell a quota refusal from a policy refusal — the status alone only
//! says "permanent". Both are permanent, but the code and the message differ,
//! and a user needs to know which ("quota resets tomorrow" is not "this channel
//! may not upload this").
//!
//! `Retry-After` (delta-seconds or an HTTP date) is honoured exactly when it is
//! within [`RetryPolicy::max_retry_after`]; beyond that the upload is reported
//! as retryable-with-delay instead of parking a task for an hour, which is what
//! the persistent cooldown layer exists to do.

use std::fmt;
use std::future::Future;
use std::time::{Duration, SystemTime};

use serde::Deserialize;
use xpst_core::envelope::VideoMode;

use crate::capability::{
    capability, Capability, CapabilityError, DEFAULT_CATEGORY_ID, MAX_DESCRIPTION_CHARS, MAX_TAGS,
    MAX_TITLE_CHARS, UPLOADS_PER_DAY_PER_PROJECT, UPLOAD_ENDPOINT, UPLOAD_PARTS,
};
use crate::mode::post_url;
use crate::token::{AccessToken, TokenError, TokenSource};
use crate::transport::{HttpRequest, HttpResponse, MediaSource, Method, Transport};

/// Stable failure/refusal codes. These are what MCP and the CLI branch on.
pub const CODE_QUOTA_EXCEEDED: &str = "YOUTUBE_QUOTA_EXCEEDED";
pub const CODE_POLICY_REFUSED: &str = "YOUTUBE_POLICY_REFUSED";
pub const CODE_REQUEST_INVALID: &str = "YOUTUBE_REQUEST_INVALID";
pub const CODE_AUTH_EXPIRED: &str = "YOUTUBE_AUTH_EXPIRED";
pub const CODE_AUTH_CONFIG: &str = "YOUTUBE_AUTH_CONFIG";
pub const CODE_TOKEN_UNAVAILABLE: &str = "YOUTUBE_TOKEN_UNAVAILABLE";
pub const CODE_RATE_LIMITED: &str = "YOUTUBE_RATE_LIMITED";
pub const CODE_SERVER_ERROR: &str = "YOUTUBE_SERVER_ERROR";
pub const CODE_NETWORK_ERROR: &str = "YOUTUBE_NETWORK_ERROR";
pub const CODE_UPLOAD_UNVERIFIED: &str = "YOUTUBE_UPLOAD_UNVERIFIED";
pub const CODE_PROTOCOL_ERROR: &str = "YOUTUBE_PROTOCOL_ERROR";
pub const CODE_MEDIA_UNREADABLE: &str = "YOUTUBE_MEDIA_UNREADABLE";
/// No response arrived for a request that may have published; the outcome is
/// unknown and only a read-back settles it.
pub const CODE_UPLOAD_UNCONFIRMED: &str = "YOUTUBE_UPLOAD_UNCONFIRMED";
/// `snippet.title` longer than the character budget, after the Shorts marker
/// has been reserved.
pub const CODE_TITLE_TOO_LONG: &str = "YOUTUBE_TITLE_TOO_LONG";
/// `snippet.description` longer than the documented 5 000 characters.
pub const CODE_DESCRIPTION_TOO_LONG: &str = "YOUTUBE_DESCRIPTION_TOO_LONG";
/// More than the documented 15 `snippet.tags` entries.
pub const CODE_TOO_MANY_TAGS: &str = "YOUTUBE_TOO_MANY_TAGS";

/// Body reasons that mean "the daily bucket is spent".
const QUOTA_REASONS: [&str; 3] = ["quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"];

/// Body reasons that are short-term throttling despite the 403.
const THROTTLE_REASONS: [&str; 2] = ["rateLimitExceeded", "userRateLimitExceeded"];

// ---------------------------------------------------------------------------
// Retry-After
// ---------------------------------------------------------------------------

// Parsed by the shared HTTP vocabulary rather than duplicated here: the same
// header arrives on every destination and one implementation is one behaviour.
// Only the *decision* to wait stays local, in `RetryPolicy`.
pub use xpst_provider::http::RetryAfter;

// ---------------------------------------------------------------------------
// Response classification
// ---------------------------------------------------------------------------

/// A Google error body, as far as this adapter needs it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GoogleError {
    /// `error.errors[].reason`, in the order the API listed them.
    pub reasons: Vec<String>,
    /// `error.message`.
    pub message: String,
    /// `error.status` (`PERMISSION_DENIED`, …), when present.
    pub status: Option<String>,
}

#[derive(Deserialize)]
struct ErrorEnvelope {
    error: ErrorBody,
}

#[derive(Deserialize)]
struct ErrorBody {
    #[serde(default)]
    message: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    errors: Vec<ErrorDetail>,
}

#[derive(Deserialize)]
struct ErrorDetail {
    #[serde(default)]
    reason: Option<String>,
}

/// Parse an API error body. `None` when the body is not Google's JSON shape —
/// callers must then fall back to the raw text rather than assume "no reason".
#[must_use]
pub fn parse_google_error(body: &[u8]) -> Option<GoogleError> {
    let envelope: ErrorEnvelope = serde_json::from_slice(body).ok()?;
    Some(GoogleError {
        reasons: envelope
            .error
            .errors
            .into_iter()
            .filter_map(|detail| detail.reason)
            .collect(),
        message: envelope.error.message.unwrap_or_default(),
        status: envelope.error.status,
    })
}

/// What a response from the upload session means.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResponseKind {
    /// The session is finished; this is the video resource.
    Complete(Vec<u8>),
    /// Keep going from this offset (`None` = nothing received, start at zero).
    Incomplete(Option<u64>),
    /// Too many requests: wait, then continue.
    Throttled(Option<RetryAfter>),
    /// A server or connection problem that may not repeat.
    Retryable {
        status: u16,
        message: String,
        retry_after: Option<RetryAfter>,
    },
    /// The session is gone; open a new one and start from zero.
    SessionExpired,
    /// The token is not usable; a human must re-authorise.
    NeedsReauth { message: String },
    /// Do not retry this upload.
    Permanent {
        code: &'static str,
        message: String,
        status: u16,
        retry_after: Option<RetryAfter>,
    },
    /// The reply does not fit the documented protocol.
    Protocol { message: String },
}

/// Classify one response. Public so the classification can be asserted
/// directly, without a driver or a fake transport in the way.
#[must_use]
pub fn classify_response(response: &HttpResponse, total_bytes: u64) -> ResponseKind {
    let status = response.status;
    let retry_after = response.header("Retry-After").and_then(RetryAfter::parse);
    match status {
        200 | 201 => ResponseKind::Complete(response.body.clone()),
        308 => {
            let next = response
                .header("Range")
                .and_then(|value| parse_range_next_offset(value, total_bytes));
            ResponseKind::Incomplete(next)
        }
        429 => ResponseKind::Throttled(retry_after),
        401 => ResponseKind::NeedsReauth {
            message:
                "YouTube rejected the access token (401). Run 'xpst auth youtube' to reconnect."
                    .to_owned(),
        },
        403 => classify_forbidden(response, retry_after),
        404 | 410 => ResponseKind::SessionExpired,
        500..=599 => ResponseKind::Retryable {
            status,
            message: format!(
                "YouTube returned HTTP {status}: {}",
                truncate(&response.body_text())
            ),
            retry_after,
        },
        400 => ResponseKind::Permanent {
            code: CODE_REQUEST_INVALID,
            message: format!(
                "YouTube rejected the request (400): {}",
                api_message(response)
            ),
            status,
            retry_after,
        },
        // 405/409/413/415/416 and friends: the request itself is wrong, so a
        // retry would fail identically.
        405 | 409 | 413 | 415 | 416 => ResponseKind::Permanent {
            code: CODE_REQUEST_INVALID,
            message: format!(
                "YouTube rejected the request (HTTP {status}): {}",
                api_message(response)
            ),
            status,
            retry_after,
        },
        _ => ResponseKind::Protocol {
            message: format!(
                "unexpected HTTP {status} from the upload session: {}",
                truncate(&response.body_text())
            ),
        },
    }
}

fn classify_forbidden(response: &HttpResponse, retry_after: Option<RetryAfter>) -> ResponseKind {
    let parsed = parse_google_error(&response.body);
    let reasons = parsed
        .as_ref()
        .map(|error| error.reasons.clone())
        .unwrap_or_default();
    let api_text = parsed
        .as_ref()
        .map(|error| error.message.clone())
        .unwrap_or_else(|| truncate(&response.body_text()));
    let lowered = format!("{} {}", reasons.join(" "), api_text).to_ascii_lowercase();

    let is_quota_reason = reasons
        .iter()
        .any(|reason| QUOTA_REASONS.contains(&reason.as_str()));
    let is_throttle = reasons
        .iter()
        .any(|reason| THROTTLE_REASONS.contains(&reason.as_str()));
    // The body's `reason` is authoritative when it lists one: a throttle body
    // routinely mentions the word "quota" in prose, and a per-second limit must
    // not be mistaken for a spent daily bucket. The keyword fallback exists only
    // for a body with no structured reason at all.
    let is_quota = is_quota_reason || (reasons.is_empty() && lowered.contains("quota"));

    if is_throttle && !is_quota {
        // 403 rate limiting is documented as short-term, so it is retried.
        return ResponseKind::Throttled(retry_after);
    }
    if is_quota {
        let hint = retry_after
            .map(|value| format!(" ({})", value.describe(SystemTime::now())))
            .unwrap_or_default();
        return ResponseKind::Permanent {
            code: CODE_QUOTA_EXCEEDED,
            message: format!(
                "Daily quota limit reached — the `videos.insert` bucket allows \
                 {UPLOADS_PER_DAY_PER_PROJECT} uploads a day per Cloud project, and it is spent.{hint}"
            ),
            status: 403,
            retry_after,
        };
    }
    let reason_text = if reasons.is_empty() {
        String::new()
    } else {
        format!(", reason {}", reasons.join(", "))
    };
    ResponseKind::Permanent {
        code: CODE_POLICY_REFUSED,
        message: format!("YouTube refused this upload (403{reason_text}): {api_text}"),
        status: 403,
        retry_after,
    }
}

fn api_message(response: &HttpResponse) -> String {
    parse_google_error(&response.body)
        .map(|error| error.message)
        .filter(|message| !message.is_empty())
        .unwrap_or_else(|| truncate(&response.body_text()))
}

/// `bytes=0-1048575` → the next offset (`1048576`), clamped to `total`.
///
/// The end of the range is inclusive: it is the last byte the server holds, not
/// the count. Getting that wrong by one byte is a silent corruption, so the
/// conversion is a named function with its own tests.
#[must_use]
pub fn parse_range_next_offset(value: &str, total: u64) -> Option<u64> {
    let rest = value.trim().strip_prefix("bytes=")?;
    let (start, end) = rest.trim().split_once('-')?;
    let start: u64 = start.trim().parse().ok()?;
    let end: u64 = end.trim().parse().ok()?;
    if end < start {
        return None;
    }
    Some(end.saturating_add(1).min(total))
}

/// Shorten a message for a log line or a refusal, on a character boundary.
fn truncate(text: &str) -> String {
    const LIMIT: usize = 300;
    let cleaned = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if cleaned.chars().count() <= LIMIT {
        return cleaned;
    }
    let mut shortened: String = cleaned.chars().take(LIMIT).collect();
    shortened.push('…');
    shortened
}

// ---------------------------------------------------------------------------
// Metadata
// ---------------------------------------------------------------------------

/// `status.privacyStatus`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Privacy {
    Public,
    Unlisted,
    Private,
}

impl Privacy {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Public => "public",
            Self::Unlisted => "unlisted",
            Self::Private => "private",
        }
    }

    /// Parse a caller-supplied value; `None` for anything that is not one of
    /// the three the API accepts (the caller refuses it, never coerces).
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "public" => Some(Self::Public),
            "unlisted" => Some(Self::Unlisted),
            "private" => Some(Self::Private),
            _ => None,
        }
    }
}

/// The Shorts classification marker. It is part of the payload contract — a
/// Short without it is just a vertical video — so it is reserved out of the
/// title budget before any user text is measured.
pub const SHORTS_MARKER: &str = " #Shorts";

/// Why a title, description or tag set cannot be uploaded as given.
///
/// Every refusal carries the number that was **measured** and the number the
/// limit allows, so the caller can shorten by exactly the right amount instead
/// of guessing. Nothing in this adapter trims user content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MetadataRefusal {
    code: &'static str,
    field: &'static str,
    measured: usize,
    limit: usize,
    message: String,
}

impl MetadataRefusal {
    /// The stable code (`YOUTUBE_TITLE_TOO_LONG`, …).
    #[must_use]
    pub const fn code(&self) -> &'static str {
        self.code
    }

    /// The metadata field the refusal is about (`snippet.title`, …).
    #[must_use]
    pub const fn field(&self) -> &'static str {
        self.field
    }

    /// What was measured, in characters (or entries, for tags).
    #[must_use]
    pub const fn measured(&self) -> usize {
        self.measured
    }

    /// The limit that measurement exceeded.
    #[must_use]
    pub const fn limit(&self) -> usize {
        self.limit
    }

    /// The message without the code prefix.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }

    /// Always `false`: a refusal is a decision about this input, not a
    /// transient failure. It lifts when the caller shortens the text.
    #[must_use]
    pub const fn retryable(&self) -> bool {
        false
    }

    fn title(measured: usize, limit: usize) -> Self {
        let message = if limit < MAX_TITLE_CHARS {
            format!(
                "the title is {measured} characters and the limit is {limit}: YouTube allows \
                 {MAX_TITLE_CHARS} characters, and the {} characters of '{SHORTS_MARKER}' are \
                 reserved out of that budget first so the Shorts classification marker always \
                 fits. xPST refuses rather than trimming your words — shorten the title and post \
                 again.",
                SHORTS_MARKER.chars().count()
            )
        } else {
            format!(
                "the title is {measured} characters and the limit is {MAX_TITLE_CHARS}. xPST \
                 refuses rather than trimming your words — shorten the title and post again."
            )
        };
        Self {
            code: CODE_TITLE_TOO_LONG,
            field: "snippet.title",
            measured,
            limit,
            message,
        }
    }

    fn description(measured: usize) -> Self {
        Self {
            code: CODE_DESCRIPTION_TOO_LONG,
            field: "snippet.description",
            measured,
            limit: MAX_DESCRIPTION_CHARS,
            message: format!(
                "the description is {measured} characters and the limit is \
                 {MAX_DESCRIPTION_CHARS}. xPST refuses rather than trimming your words — shorten \
                 the description and post again."
            ),
        }
    }

    fn tags(measured: usize) -> Self {
        Self {
            code: CODE_TOO_MANY_TAGS,
            field: "snippet.tags",
            measured,
            limit: MAX_TAGS,
            message: format!(
                "the upload carries {measured} tags and the limit is {MAX_TAGS}. xPST refuses \
                 rather than dropping tags silently — remove {} tag(s) or use fewer hashtags in \
                 the caption.",
                measured.saturating_sub(MAX_TAGS)
            ),
        }
    }
}

impl fmt::Display for MetadataRefusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for MetadataRefusal {}

/// The character budgets that apply to one mode.
///
/// `title_chars` already has the Shorts marker reserved out of it, so a caller
/// that wants to pre-check its own text uses these numbers rather than the raw
/// platform constants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MetadataLimits {
    pub title_chars: usize,
    pub description_chars: usize,
    pub max_tags: usize,
}

/// The video resource fields this adapter writes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoMetadata {
    pub title: String,
    pub description: String,
    pub tags: Vec<String>,
    pub category_id: String,
    pub privacy: Privacy,
    pub made_for_kids: bool,
}

impl VideoMetadata {
    /// The character budget for a title in this mode, with the Shorts marker
    /// already deducted.
    #[must_use]
    pub fn limits(mode: VideoMode) -> MetadataLimits {
        MetadataLimits {
            title_chars: title_limit(mode, false),
            description_chars: MAX_DESCRIPTION_CHARS,
            max_tags: MAX_TAGS,
        }
    }

    /// Derive the metadata from a caption, as the Python adapter does: the
    /// first line is the title, the rest the description, `#hashtags` become
    /// tags, and a Shorts upload carries `#Shorts` in the title so YouTube
    /// classifies it as short-form. A long-form upload deliberately does not —
    /// that tag is how a landscape video used to be published as a Short.
    ///
    /// Caption text past a limit is **refused** with the measured length, never
    /// cut: the Python adapter truncated, which silently published a different
    /// video than the one the user described.
    pub fn from_caption(caption: &str, mode: VideoMode) -> Result<Self, MetadataRefusal> {
        let mut lines = caption.splitn(2, '\n');
        let first = lines.next().unwrap_or_default();
        let title = if first.trim().is_empty() {
            "New video".to_owned()
        } else {
            first.to_owned()
        };
        let description = lines.next().unwrap_or(caption).to_owned();

        let base: &[&str] = match mode {
            VideoMode::Shorts => &["Shorts", "AI", "tech", "video"],
            VideoMode::Long => &["AI", "tech", "video"],
        };
        let mut tags: Vec<String> = Vec::new();
        for tag in hashtags(caption)
            .into_iter()
            .chain(base.iter().map(|tag| (*tag).to_owned()))
        {
            if !tags
                .iter()
                .any(|existing| existing.eq_ignore_ascii_case(&tag))
            {
                tags.push(tag);
            }
        }

        let metadata = Self {
            title,
            description,
            tags,
            category_id: DEFAULT_CATEGORY_ID.to_owned(),
            privacy: Privacy::Public,
            made_for_kids: false,
        }
        .with_classification_marker(mode)?;
        metadata.validate(mode)?;
        Ok(metadata)
    }

    /// Add the Shorts classification marker when the title lacks it.
    ///
    /// This is the one place the adapter writes to user text, and it only ever
    /// *adds* the contractually required marker — after checking there is room
    /// for it. A title with no room is refused with both numbers rather than
    /// having the marker (or the title's tail) cut off.
    pub fn with_classification_marker(self, mode: VideoMode) -> Result<Self, MetadataRefusal> {
        if mode != VideoMode::Shorts || has_shorts_marker(&self.title) {
            return Ok(self);
        }
        let measured = self.title.chars().count();
        let limit = title_limit(mode, false);
        if measured > limit {
            return Err(MetadataRefusal::title(measured, limit));
        }
        let mut title = self.title;
        title.push_str(SHORTS_MARKER);
        Ok(Self { title, ..self })
    }

    /// Check every field against the documented limit for this mode.
    ///
    /// [`VideoMetadata`] has public fields, so a caller can build one by hand;
    /// the plan and the driver both call this rather than trusting the struct.
    pub fn validate(&self, mode: VideoMode) -> Result<(), MetadataRefusal> {
        let limit = title_limit(mode, has_shorts_marker(&self.title));
        let measured = self.title.chars().count();
        if measured > limit {
            return Err(MetadataRefusal::title(measured, limit));
        }
        let measured = self.description.chars().count();
        if measured > MAX_DESCRIPTION_CHARS {
            return Err(MetadataRefusal::description(measured));
        }
        if self.tags.len() > MAX_TAGS {
            return Err(MetadataRefusal::tags(self.tags.len()));
        }
        Ok(())
    }

    /// Override `status.privacyStatus`.
    #[must_use]
    pub fn with_privacy(mut self, privacy: Privacy) -> Self {
        self.privacy = privacy;
        self
    }

    /// The `videos.insert` request body.
    #[must_use]
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "snippet": {
                "title": self.title,
                "description": self.description,
                "tags": self.tags,
                "categoryId": self.category_id,
            },
            "status": {
                "privacyStatus": self.privacy.as_str(),
                "selfDeclaredMadeForKids": self.made_for_kids,
            }
        })
    }
}

/// Whether the title already carries the Shorts marker, case-insensitively.
#[must_use]
pub fn has_shorts_marker(title: &str) -> bool {
    title.to_ascii_lowercase().contains("#shorts")
}

/// The character budget for a title.
///
/// The platform ceiling is [`MAX_TITLE_CHARS`]; when the marker still has to be
/// appended it is reserved first, which is what makes "the marker never gets
/// cut" a property of the measurement rather than a hope about ordering.
fn title_limit(mode: VideoMode, marker_present: bool) -> usize {
    if mode == VideoMode::Shorts && !marker_present {
        MAX_TITLE_CHARS.saturating_sub(SHORTS_MARKER.chars().count())
    } else {
        MAX_TITLE_CHARS
    }
}

/// `#tag` extractions, in order, without the `#`.
///
/// Only text that follows a `#` is collected: a stray word is not a hashtag,
/// and collecting it would put the title's first word into the tag list.
fn hashtags(caption: &str) -> Vec<String> {
    let mut found = Vec::new();
    let mut current = String::new();
    let mut in_tag = false;
    for character in caption.chars() {
        if character == '#' {
            if in_tag && !current.is_empty() {
                found.push(std::mem::take(&mut current));
            }
            in_tag = true;
            current.clear();
            continue;
        }
        if !in_tag {
            continue;
        }
        if character.is_alphanumeric() || character == '_' {
            current.push(character);
        } else {
            if !current.is_empty() {
                found.push(std::mem::take(&mut current));
            }
            in_tag = false;
        }
    }
    if in_tag && !current.is_empty() {
        found.push(current);
    }
    found
}

// ---------------------------------------------------------------------------
// Upload plan
// ---------------------------------------------------------------------------

/// Why a plan cannot be built.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PlanError {
    #[error(transparent)]
    Capability(#[from] CapabilityError),
    /// The metadata exceeds a documented limit. Refused, never trimmed.
    #[error(transparent)]
    Metadata(#[from] MetadataRefusal),
    #[error("chunk size must be greater than zero")]
    ChunkEmpty,
    #[error(
        "chunk size {chunk_size} is not a multiple of {granularity} bytes — every chunk but the \
         last must be, and Google rejects the request otherwise"
    )]
    ChunkNotAligned { chunk_size: u64, granularity: u64 },
}

/// Everything the driver needs to send one video.
#[derive(Debug, Clone, PartialEq)]
pub struct UploadPlan {
    pub mode: VideoMode,
    pub metadata: VideoMetadata,
    pub content_type: String,
    pub total_bytes: u64,
    pub chunk_size: u64,
}

impl UploadPlan {
    /// Build a plan with the mode's documented chunk size.
    pub fn new(
        mode: VideoMode,
        metadata: VideoMetadata,
        total_bytes: u64,
    ) -> Result<Self, PlanError> {
        let capability = capability()?;
        Self::with_capability(capability, mode, metadata, total_bytes)
    }

    /// Build a plan against an explicit capability declaration (used by tests
    /// that must not depend on the registry being present).
    pub fn with_capability(
        capability: Capability,
        mode: VideoMode,
        metadata: VideoMetadata,
        total_bytes: u64,
    ) -> Result<Self, PlanError> {
        // The marker is part of the Shorts payload contract, so it is added (and
        // its room reserved) before anything else looks at the title; then every
        // field is checked against its limit, which is what makes an over-length
        // post a refusal instead of a quiet trim.
        let metadata = metadata.with_classification_marker(mode)?;
        metadata.validate(mode)?;
        Self {
            mode,
            metadata,
            content_type: "video/mp4".to_owned(),
            total_bytes,
            chunk_size: capability.chunk_size(mode),
        }
        .validate(capability)
    }

    /// Override the chunk size, refusing an unaligned one.
    pub fn with_chunk_size(mut self, chunk_size: u64) -> Result<Self, PlanError> {
        let capability = capability()?;
        self.chunk_size = chunk_size;
        self.validate(capability)
    }

    fn validate(self, capability: Capability) -> Result<Self, PlanError> {
        if self.chunk_size == 0 {
            return Err(PlanError::ChunkEmpty);
        }
        if !self
            .chunk_size
            .is_multiple_of(capability.chunk_granularity_bytes)
        {
            return Err(PlanError::ChunkNotAligned {
                chunk_size: self.chunk_size,
                granularity: capability.chunk_granularity_bytes,
            });
        }
        Ok(self)
    }

    /// Number of chunk requests the upload will take.
    #[must_use]
    pub const fn chunk_count(&self) -> u64 {
        if self.total_bytes == 0 {
            return 0;
        }
        self.total_bytes.div_ceil(self.chunk_size)
    }
}

// ---------------------------------------------------------------------------
// Retry policy and sleeper
// ---------------------------------------------------------------------------

/// How hard the driver tries, and how long it is willing to wait.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetryPolicy {
    /// Consecutive failed attempts before giving up. Reset by progress: a
    /// 12-hour upload must not fail because it had five bad chunks over three
    /// thousand.
    pub max_attempts: u32,
    pub base_delay: Duration,
    pub max_delay: Duration,
    /// A server-supplied wait longer than this is reported instead of slept, so
    /// the caller's persistent cooldown owns the wait.
    pub max_retry_after: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 5,
            base_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(30),
            max_retry_after: Duration::from_secs(300),
        }
    }
}

impl RetryPolicy {
    /// Exponential backoff with a cap.
    #[must_use]
    pub fn backoff(&self, failed_attempts: u32) -> Duration {
        let exponent = failed_attempts.saturating_sub(1).min(16);
        let scaled = self
            .base_delay
            .checked_mul(1u32 << exponent)
            .unwrap_or(self.max_delay);
        scaled.min(self.max_delay)
    }
}

/// Waits between attempts. Injectable so a test can assert the delay without
/// spending it.
pub trait Sleeper {
    fn sleep(&self, duration: Duration) -> impl Future<Output = ()> + Send;
}

/// The real one. `tokio::time::sleep` yields to the runtime, which is the
/// point: the Python adapter had to hand its blocking loop to a thread pool.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemSleeper;

impl SystemSleeper {
    async fn wait(duration: Duration) {
        tokio::time::sleep(duration).await;
    }
}

impl Sleeper for SystemSleeper {
    fn sleep(&self, duration: Duration) -> impl Future<Output = ()> + Send {
        Self::wait(duration)
    }
}

// ---------------------------------------------------------------------------
// Failures
// ---------------------------------------------------------------------------

/// The three honest answers to "should this be tried again?" — plus the one
/// that says the question cannot be answered yet.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureClass {
    /// Retrying cannot succeed; the input or the account has to change.
    Permanent,
    /// Transient, or the retry budget was exhausted on something transient.
    Retryable,
    /// The grant is gone: only a human re-authorisation fixes it.
    NeedsReauth,
    /// The attempt may have landed and no response said otherwise. Retrying
    /// blind is how a second video gets published, so the caller reconciles
    /// (reads the channel back) first. Same fact the pipeline states as
    /// `DestinationOutcome::Unconfirmed`.
    Unconfirmed,
}

/// One failed upload, with the code, the class and any server-supplied delay.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UploadFailure {
    pub class: FailureClass,
    pub code: &'static str,
    pub message: String,
    /// HTTP status, when there was a response.
    pub status: Option<u16>,
    /// `Retry-After`, when the server sent one.
    pub retry_after: Option<Duration>,
    /// HTTP requests issued before giving up, initiation included.
    pub requests: u32,
}

impl UploadFailure {
    fn permanent(code: &'static str, message: impl Into<String>, requests: u32) -> Self {
        Self {
            class: FailureClass::Permanent,
            code,
            message: message.into(),
            status: None,
            retry_after: None,
            requests,
        }
    }

    fn retryable_failure(
        code: &'static str,
        message: impl Into<String>,
        status: Option<u16>,
        retry_after: Option<Duration>,
        requests: u32,
    ) -> Self {
        Self {
            class: FailureClass::Retryable,
            code,
            message: message.into(),
            status,
            retry_after,
            requests,
        }
    }

    fn needs_reauth_failure(message: impl Into<String>, requests: u32) -> Self {
        Self {
            class: FailureClass::NeedsReauth,
            code: CODE_AUTH_EXPIRED,
            message: message.into(),
            status: Some(401),
            retry_after: None,
            requests,
        }
    }

    /// An attempt that may have landed. `status` stays `None` on purpose: no
    /// response arrived, so quoting one would be a fabricated fact.
    fn unconfirmed_failure(message: impl Into<String>, requests: u32) -> Self {
        Self {
            class: FailureClass::Unconfirmed,
            code: CODE_UPLOAD_UNCONFIRMED,
            message: message.into(),
            status: None,
            retry_after: None,
            requests,
        }
    }

    /// A metadata refusal, as an upload failure: permanent, no requests spent.
    fn refused(refusal: &MetadataRefusal) -> Self {
        Self::permanent(refusal.code(), refusal.message().to_owned(), 0)
    }

    fn from_token(error: TokenError, requests: u32) -> Self {
        match error {
            TokenError::NeedsReauth { detail } => Self {
                class: FailureClass::NeedsReauth,
                code: CODE_AUTH_EXPIRED,
                message: format!("Run 'xpst auth youtube' to reconnect. {detail}"),
                status: None,
                retry_after: None,
                requests,
            },
            TokenError::Transient { detail } => {
                Self::retryable_failure(CODE_TOKEN_UNAVAILABLE, detail, None, None, requests)
            }
            TokenError::Fatal { detail } => Self::permanent(CODE_AUTH_CONFIG, detail, requests),
        }
    }

    /// Whether re-running the upload may succeed without any human action.
    ///
    /// Deliberately false for [`FailureClass::Unconfirmed`]: a blind retry of an
    /// attempt that may have published is how a duplicate video appears.
    #[must_use]
    pub const fn retryable(&self) -> bool {
        matches!(self.class, FailureClass::Retryable)
    }

    /// Whether the user has to re-authorise before anything else happens.
    #[must_use]
    pub const fn needs_reauth(&self) -> bool {
        matches!(self.class, FailureClass::NeedsReauth)
    }

    /// Whether this attempt's outcome is unknown and must be reconciled
    /// (read back) before anything is retried.
    #[must_use]
    pub const fn unconfirmed(&self) -> bool {
        matches!(self.class, FailureClass::Unconfirmed)
    }
}

impl fmt::Display for UploadFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for UploadFailure {}

/// A published video.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UploadOutcome {
    pub video_id: String,
    pub url: String,
    pub mode: VideoMode,
}

// ---------------------------------------------------------------------------
// Reconciling an upload whose session was lost
// ---------------------------------------------------------------------------

/// What a read-back established about an upload whose session died.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReconcileVerdict {
    /// Positive evidence that nothing was published, so starting the upload
    /// again cannot create a duplicate.
    NotPublished,
    /// The upload is already live under this id.
    Published { video_id: String },
    /// Nothing conclusive. Not permission to re-upload.
    Unknown { detail: String },
}

/// What the reconcile seam is asked about.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconcileRequest<'a> {
    /// The metadata the lost upload was carrying: a read-back has to match on
    /// something, and this is what the channel would show.
    pub metadata: &'a VideoMetadata,
    pub mode: VideoMode,
    /// Content bytes this adapter is known to have put on the wire.
    pub bytes_submitted: u64,
    pub total_bytes: u64,
    /// The status that ended the session (404 or 410).
    pub status: u16,
}

/// Establishes, after a session is lost, whether a video was already created.
///
/// The adapter cannot answer this itself: it holds no read path and no channel
/// identity, and the lost session URI is gone. So it asks, and treats "no
/// answer" as [`ReconcileVerdict::Unknown`] — which is reported as unconfirmed
/// rather than resolved by re-uploading the file.
pub trait Reconciler {
    fn reconcile(
        &self,
        request: &ReconcileRequest<'_>,
    ) -> impl Future<Output = ReconcileVerdict> + Send;
}

/// The default: no read-back is available, so nothing is ever proven and a lost
/// session is reported as unconfirmed.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoReconciler;

impl Reconciler for NoReconciler {
    async fn reconcile(&self, _request: &ReconcileRequest<'_>) -> ReconcileVerdict {
        ReconcileVerdict::Unknown {
            detail: "no reconcile probe is configured for this upload".to_owned(),
        }
    }
}

/// The shared instance `Uploader::new` borrows when the caller supplies none.
static NO_RECONCILER: NoReconciler = NoReconciler;

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

fn initiate_request(endpoint: &str, token: &AccessToken, plan: &UploadPlan) -> HttpRequest {
    let url = format!("{endpoint}?uploadType=resumable&part={UPLOAD_PARTS}");
    // Serialising a `serde_json::Value` cannot fail.
    let body = serde_json::to_vec(&plan.metadata.to_json()).unwrap_or_default();
    HttpRequest::new(Method::Post, url)
        .with_header("Authorization", token.authorization_value())
        .with_header("Content-Type", "application/json; charset=UTF-8")
        .with_header("X-Upload-Content-Length", plan.total_bytes.to_string())
        .with_header("X-Upload-Content-Type", plan.content_type.clone())
        .with_body(body)
}

fn chunk_request(session: &str, offset: u64, len: u64, total: u64, body: Vec<u8>) -> HttpRequest {
    let end = offset + len - 1;
    HttpRequest::new(Method::Put, session)
        .with_header("Content-Type", "application/octet-stream")
        .with_header("Content-Length", len.to_string())
        .with_header("Content-Range", format!("bytes {offset}-{end}/{total}"))
        .with_body(body)
}

/// The status/finalise request: `bytes */{total}`, no body.
///
/// Google documents this as the way to finish when every byte is already in but
/// the session is still open, and it doubles as a progress query.
fn status_request(session: &str, total: u64) -> HttpRequest {
    HttpRequest::new(Method::Put, session)
        .with_header("Content-Length", "0")
        .with_header("Content-Range", format!("bytes */{total}"))
}

// ---------------------------------------------------------------------------
// Driver
// ---------------------------------------------------------------------------

/// How many times a lost session may be replaced. Each replacement re-sends the
/// whole file, so it is allowed at most once, and only on proof that nothing
/// was published.
const MAX_SESSION_RESTARTS: u32 = 1;

/// Which request the loop just issued. The same 308 means different things for
/// a chunk and for a finalise, so the driver tracks it rather than inferring it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Attempt {
    Chunk,
    Finalise,
}

/// How a lost session is resolved. Restarting is only ever chosen on proof.
enum SessionLoss {
    /// Nothing was published, and this adapter can say why.
    NothingPublished,
    Published(String),
    Unknown(String),
}

/// Runs the resumable protocol. Holds no state between uploads.
pub struct Uploader<'a, T, K, S, R = NoReconciler> {
    transport: &'a T,
    tokens: &'a K,
    sleeper: &'a S,
    reconciler: &'a R,
    policy: RetryPolicy,
}

impl<'a, T, K, S> Uploader<'a, T, K, S, NoReconciler> {
    /// An uploader with no read-back seam: a lost session that cannot be proven
    /// safe is reported as unconfirmed, never silently re-uploaded.
    #[must_use]
    pub fn new(transport: &'a T, tokens: &'a K, sleeper: &'a S) -> Self {
        Self {
            transport,
            tokens,
            sleeper,
            reconciler: &NO_RECONCILER,
            policy: RetryPolicy::default(),
        }
    }

    /// Supply the seam that answers "was a video already created for this
    /// upload?" after a session is lost.
    #[must_use]
    pub fn with_reconciler<R>(self, reconciler: &'a R) -> Uploader<'a, T, K, S, R>
    where
        R: Reconciler,
    {
        Uploader {
            transport: self.transport,
            tokens: self.tokens,
            sleeper: self.sleeper,
            reconciler,
            policy: self.policy,
        }
    }
}

impl<'a, T, K, S, R> Uploader<'a, T, K, S, R>
where
    T: Transport,
    K: TokenSource,
    S: Sleeper,
    R: Reconciler,
{
    #[must_use]
    pub fn with_policy(mut self, policy: RetryPolicy) -> Self {
        self.policy = policy;
        self
    }

    /// Upload one video, resuming through 308s and retrying what is worth
    /// retrying.
    pub async fn upload<M: MediaSource>(
        &self,
        media: &M,
        plan: &UploadPlan,
    ) -> Result<UploadOutcome, UploadFailure> {
        if media.total_bytes() != plan.total_bytes {
            return Err(UploadFailure::permanent(
                CODE_PROTOCOL_ERROR,
                format!(
                    "the plan states {} bytes but the media source reports {}",
                    plan.total_bytes,
                    media.total_bytes()
                ),
                0,
            ));
        }

        // Refuse over-length metadata before a single byte goes out. The plan
        // already checked it, but the fields are public: a caller may have
        // edited them since, and the title is what decides whether YouTube
        // classifies the upload as a Short.
        plan.metadata
            .validate(plan.mode)
            .map_err(|refusal| UploadFailure::refused(&refusal))?;

        let token = self
            .tokens
            .access_token()
            .await
            .map_err(|error| UploadFailure::from_token(error, 0))?;

        let mut requests: u32 = 0;
        let mut restarts: u32 = 0;
        let mut session = self.open_session(&token, plan, &mut requests).await?;

        let mut offset: u64 = 0;
        let mut failures: u32 = 0;
        // The highest content byte this adapter has put on the wire for the
        // current session, and whether any of those requests went unanswered.
        // Together they are what decides whether a lost session may be
        // restarted or has to be reported as unconfirmed.
        let mut sent_high_water: u64 = 0;
        let mut unanswered_send = false;

        loop {
            let (request, attempt) = if offset < plan.total_bytes {
                let len = plan.chunk_size.min(plan.total_bytes - offset);
                let bytes = match media.read_range(offset, len).await {
                    Ok(bytes) if bytes.len() as u64 == len => bytes,
                    Ok(bytes) => {
                        return Err(UploadFailure::permanent(
                            CODE_MEDIA_UNREADABLE,
                            format!(
                                "asked the media source for {len} bytes at offset {offset}, got {}",
                                bytes.len()
                            ),
                            requests,
                        ))
                    }
                    Err(error) => {
                        return Err(UploadFailure::permanent(
                            CODE_MEDIA_UNREADABLE,
                            format!("could not read the video at offset {offset}: {error}"),
                            requests,
                        ))
                    }
                };
                // Recorded before the send, because the point of the high-water
                // mark is what *may* have reached the server.
                sent_high_water = sent_high_water.max(offset + len);
                (
                    chunk_request(&session, offset, len, plan.total_bytes, bytes),
                    Attempt::Chunk,
                )
            } else {
                (
                    status_request(&session, plan.total_bytes),
                    Attempt::Finalise,
                )
            };

            requests += 1;
            let response = match self.transport.send(request).await {
                Ok(response) => response,
                Err(error) => {
                    if attempt == Attempt::Finalise {
                        // No response arrived, so nobody can say whether the
                        // session closed and published the video. That is not a
                        // status, and it is not retryable: re-sending the
                        // finalise could create a second video. Report the
                        // unknown and stop.
                        return Err(UploadFailure::unconfirmed_failure(
                            format!(
                                "no response arrived for the final `Content-Range: bytes */{}` \
                                 request ({error}); the server may have closed the session and \
                                 published the video. The outcome is unconfirmed: read the \
                                 channel back before retrying, because a blind retry can publish \
                                 a second video.",
                                plan.total_bytes
                            ),
                            requests,
                        ));
                    }
                    if !error.retryable {
                        return Err(UploadFailure::permanent(
                            CODE_NETWORK_ERROR,
                            error.message,
                            requests,
                        ));
                    }
                    // We cannot know whether the chunk landed. Re-sending it is
                    // safe: the server answers 308 with the range it holds and
                    // the loop resumes from there. It is remembered, though —
                    // if this session later disappears, this request may have
                    // been the one that carried the last byte.
                    unanswered_send = true;
                    self.wait_before_retry(
                        None,
                        &mut failures,
                        CODE_NETWORK_ERROR,
                        error.message,
                        requests,
                    )
                    .await?;
                    continue;
                }
            };

            match classify_response(&response, plan.total_bytes) {
                ResponseKind::Complete(body) => {
                    return finish(&body, plan, requests);
                }
                ResponseKind::Incomplete(next) => {
                    let next = next.unwrap_or(0);
                    if attempt == Attempt::Finalise
                        && plan.total_bytes > 0
                        && next >= plan.total_bytes
                    {
                        // A received fact this time: the finalise *was* answered
                        // 308 while the Range reported every byte held.
                        return Err(UploadFailure::permanent(
                            CODE_PROTOCOL_ERROR,
                            format!(
                                "the final `Content-Range: bytes */{}` request was answered with \
                                 308 rather than the video resource, although the Range header \
                                 reports every byte held — the session never returned the created \
                                 video",
                                plan.total_bytes
                            ),
                            requests,
                        ));
                    }
                    if next > offset {
                        failures = 0;
                        offset = next;
                    } else if next < offset {
                        // The server reports holding less than we thought: it is
                        // the authority on what landed, so follow it back.
                        offset = next;
                    } else {
                        // Nothing moved. Count it, so a session that answers 308
                        // with the same offset forever cannot spin here.
                        failures += 1;
                        if failures >= self.policy.max_attempts {
                            return Err(UploadFailure::retryable_failure(
                                CODE_PROTOCOL_ERROR,
                                format!(
                                    "the upload session made no progress at byte {offset} across \
                                     {} consecutive requests",
                                    self.policy.max_attempts
                                ),
                                None,
                                None,
                                requests,
                            ));
                        }
                    }
                }
                ResponseKind::Throttled(hint) => {
                    // Unlike a missing response, this answer is known to be a
                    // refusal: nothing completed, so re-sending the same request
                    // (a chunk or the finalise) cannot duplicate anything.
                    self.wait_before_retry(
                        hint,
                        &mut failures,
                        CODE_RATE_LIMITED,
                        "YouTube is rate-limiting this upload (HTTP 429)".to_owned(),
                        requests,
                    )
                    .await?;
                }
                ResponseKind::Retryable {
                    status,
                    message,
                    retry_after,
                } => {
                    self.wait_before_retry(
                        retry_after,
                        &mut failures,
                        CODE_SERVER_ERROR,
                        message,
                        requests,
                    )
                    .await
                    .map_err(|mut failure| {
                        failure.status = Some(status);
                        failure
                    })?;
                }
                ResponseKind::SessionExpired => {
                    restarts += 1;
                    if restarts > MAX_SESSION_RESTARTS {
                        return Err(UploadFailure::permanent(
                            CODE_PROTOCOL_ERROR,
                            format!(
                                "the upload session expired {restarts} times; refusing to open \
                                 another"
                            ),
                            requests,
                        ));
                    }
                    match self
                        .session_loss(plan, sent_high_water, unanswered_send, response.status)
                        .await
                    {
                        SessionLoss::NothingPublished => {
                            failures = 0;
                            offset = 0;
                            sent_high_water = 0;
                            unanswered_send = false;
                            session = self.open_session(&token, plan, &mut requests).await?;
                            continue;
                        }
                        SessionLoss::Published(video_id) => {
                            return outcome_from_id(&video_id, plan, requests).map_err(|failure| {
                                UploadFailure::unconfirmed_failure(
                                    format!(
                                        "a read-back reported that this upload is live, but its \
                                         video id is unusable: {}",
                                        failure.message
                                    ),
                                    requests,
                                )
                            })
                        }
                        SessionLoss::Unknown(detail) => {
                            return Err(UploadFailure::unconfirmed_failure(detail, requests))
                        }
                    }
                }
                ResponseKind::NeedsReauth { message } => {
                    return Err(UploadFailure::needs_reauth_failure(message, requests));
                }
                ResponseKind::Permanent {
                    code,
                    message,
                    status,
                    retry_after,
                } => {
                    let mut failure = UploadFailure::permanent(code, message, requests);
                    failure.status = Some(status);
                    failure.retry_after = retry_after.map(|value| value.delay(SystemTime::now()));
                    return Err(failure);
                }
                ResponseKind::Protocol { message } => {
                    return Err(UploadFailure::permanent(
                        CODE_PROTOCOL_ERROR,
                        message,
                        requests,
                    ));
                }
            }
        }
    }

    async fn open_session(
        &self,
        token: &AccessToken,
        plan: &UploadPlan,
        requests: &mut u32,
    ) -> Result<String, UploadFailure> {
        let endpoint = capability().map_or(UPLOAD_ENDPOINT, |capability| capability.endpoint);
        let mut failures: u32 = 0;
        loop {
            *requests += 1;
            let response = match self
                .transport
                .send(initiate_request(endpoint, token, plan))
                .await
            {
                Ok(response) => response,
                Err(error) => {
                    if !error.retryable {
                        return Err(UploadFailure::permanent(
                            CODE_NETWORK_ERROR,
                            error.message,
                            *requests,
                        ));
                    }
                    self.wait_before_retry(
                        None,
                        &mut failures,
                        CODE_NETWORK_ERROR,
                        error.message,
                        *requests,
                    )
                    .await?;
                    continue;
                }
            };

            // A successful initiation is 200 + `Location`. It is handled before
            // the generic classifier because "200 with a session URI" and
            // "200 with a video resource" are different answers.
            if (200..300).contains(&response.status) {
                let Some(location) = response.header("Location") else {
                    return Err(UploadFailure::permanent(
                        CODE_PROTOCOL_ERROR,
                        format!(
                            "the initiation answered HTTP {} without a Location header, so the \
                             upload has no session URI",
                            response.status
                        ),
                        *requests,
                    ));
                };
                if !location.starts_with("https://") && !location.starts_with("http://") {
                    return Err(UploadFailure::permanent(
                        CODE_PROTOCOL_ERROR,
                        format!("the session URI is not absolute: {location}"),
                        *requests,
                    ));
                }
                return Ok(location.to_owned());
            }

            match classify_response(&response, plan.total_bytes) {
                ResponseKind::Throttled(hint) => {
                    self.wait_before_retry(
                        hint,
                        &mut failures,
                        CODE_RATE_LIMITED,
                        "the initiation was rate-limited".to_owned(),
                        *requests,
                    )
                    .await?;
                }
                ResponseKind::Retryable {
                    status,
                    message,
                    retry_after,
                } => {
                    self.wait_before_retry(
                        retry_after,
                        &mut failures,
                        CODE_SERVER_ERROR,
                        message,
                        *requests,
                    )
                    .await
                    .map_err(|mut failure| {
                        failure.status = Some(status);
                        failure
                    })?;
                }
                ResponseKind::NeedsReauth { message } => {
                    return Err(UploadFailure::needs_reauth_failure(message, *requests));
                }
                ResponseKind::Permanent {
                    code,
                    message,
                    status,
                    retry_after,
                } => {
                    let mut failure = UploadFailure::permanent(code, message, *requests);
                    failure.status = Some(status);
                    failure.retry_after = retry_after.map(|value| value.delay(SystemTime::now()));
                    return Err(failure);
                }
                ResponseKind::SessionExpired => {
                    return Err(UploadFailure::permanent(
                        CODE_PROTOCOL_ERROR,
                        "the initiation answered 404/410; there is no session to resume",
                        *requests,
                    ));
                }
                ResponseKind::Protocol { message } => {
                    return Err(UploadFailure::permanent(
                        CODE_PROTOCOL_ERROR,
                        message,
                        *requests,
                    ));
                }
                // A 308 or a 2xx-that-was-not-2xx cannot happen here; the 2xx
                // case returned above and 308 is not a documented initiation
                // answer.
                ResponseKind::Complete(_) | ResponseKind::Incomplete(_) => {
                    return Err(UploadFailure::permanent(
                        CODE_PROTOCOL_ERROR,
                        format!(
                            "the initiation answered HTTP {} in a shape this protocol does not \
                             define",
                            response.status
                        ),
                        *requests,
                    ));
                }
            }
        }
    }

    /// Decide what a lost session means, before any byte is re-sent.
    ///
    /// Restarting from zero is only chosen on proof, and there are exactly two
    /// proofs available locally:
    ///
    /// 1. no content byte was ever put on the wire, so no video can exist; or
    /// 2. every content-bearing request received a response, and none of those
    ///    responses was a completion — publication only ever follows a request
    ///    whose response says so.
    ///
    /// Anything else is genuinely unknown: a request we sent may have been the
    /// one that carried the last byte, and the session is now gone. Only the
    /// caller's read-back can settle that, and "no answer" is reported as
    /// unconfirmed rather than resolved by re-uploading.
    async fn session_loss(
        &self,
        plan: &UploadPlan,
        sent_high_water: u64,
        unanswered_send: bool,
        status: u16,
    ) -> SessionLoss {
        if sent_high_water == 0 {
            return SessionLoss::NothingPublished;
        }
        if !unanswered_send {
            return SessionLoss::NothingPublished;
        }
        let request = ReconcileRequest {
            metadata: &plan.metadata,
            mode: plan.mode,
            bytes_submitted: sent_high_water,
            total_bytes: plan.total_bytes,
            status,
        };
        match self.reconciler.reconcile(&request).await {
            ReconcileVerdict::NotPublished => SessionLoss::NothingPublished,
            ReconcileVerdict::Published { video_id } => SessionLoss::Published(video_id),
            ReconcileVerdict::Unknown { detail } => SessionLoss::Unknown(format!(
                "the upload session answered HTTP {status} after {sent_high_water} of {} bytes \
                 had gone out, and one of those requests never came back with a response — so a \
                 video may already have been created ({detail}). Refusing to re-upload the file, \
                 which could publish a second video: read the channel back first.",
                plan.total_bytes
            )),
        }
    }

    /// Sleep before the next attempt, or give up with an honest class.
    async fn wait_before_retry(
        &self,
        hint: Option<RetryAfter>,
        failures: &mut u32,
        code: &'static str,
        message: String,
        requests: u32,
    ) -> Result<(), UploadFailure> {
        *failures += 1;
        if *failures >= self.policy.max_attempts {
            return Err(UploadFailure::retryable_failure(
                code,
                format!(
                    "gave up after {} consecutive attempts: {message}",
                    self.policy.max_attempts
                ),
                None,
                None,
                requests,
            ));
        }
        let delay = match hint {
            Some(value) => {
                let delay = value.delay(SystemTime::now());
                if delay > self.policy.max_retry_after {
                    return Err(UploadFailure::retryable_failure(
                        code,
                        format!(
                            "the server asked for a {} s wait ({}), which is beyond this \
                             uploader's {} s window; parking the upload instead of holding a task",
                            delay.as_secs(),
                            value.describe(SystemTime::now()),
                            self.policy.max_retry_after.as_secs()
                        ),
                        None,
                        Some(delay),
                        requests,
                    ));
                }
                delay
            }
            None => self.policy.backoff(*failures),
        };
        self.sleeper.sleep(delay).await;
        Ok(())
    }
}

fn finish(body: &[u8], plan: &UploadPlan, requests: u32) -> Result<UploadOutcome, UploadFailure> {
    let resource: serde_json::Value = serde_json::from_slice(body).map_err(|error| {
        UploadFailure::permanent(
            CODE_PROTOCOL_ERROR,
            format!("the completed upload did not return a JSON resource: {error}"),
            requests,
        )
    })?;
    let raw = resource
        .get("id")
        .and_then(|value| value.as_str())
        .unwrap_or_default();
    outcome_from_id(raw, plan, requests)
}

/// Turn a video id into an outcome, refusing an id that cannot be one.
fn outcome_from_id(
    raw: &str,
    plan: &UploadPlan,
    requests: u32,
) -> Result<UploadOutcome, UploadFailure> {
    let raw = raw.trim();
    let lowered = raw.to_ascii_lowercase();
    let unusable = raw.is_empty()
        || raw.contains("://")
        || raw.chars().any(char::is_whitespace)
        || matches!(lowered.as_str(), "none" | "null" | "undefined" | "unknown");
    if unusable {
        return Err(UploadFailure::permanent(
            CODE_UPLOAD_UNVERIFIED,
            format!(
                "the upload response did not contain a usable video id ({raw:?}); publication is \
                 unverified"
            ),
            requests,
        ));
    }
    Ok(UploadOutcome {
        url: post_url(raw, plan.mode),
        video_id: raw.to_owned(),
        mode: plan.mode,
    })
}
