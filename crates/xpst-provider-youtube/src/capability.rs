//! The YouTube capability declaration: every documented limit in one place.
//!
//! The registry in `xpst_core::envelope` is the planner's source of truth for
//! video limits. This module does not restate those numbers — it *reads* them
//! from the registry and adds only what the registry has no business knowing
//! (the upload endpoint, the OAuth scopes, and the per-project upload ceiling).
//! So the planner and this adapter cannot disagree: there is one number, and
//! [`capability`] copies it.
//!
//! The per-project ceiling is not a video envelope property, which is why it
//! lives here as a named constant rather than being derived:
//! `videos.insert` has had its own quota bucket since Google's 2026-06-01
//! granular-quota change — 100 calls a day per Cloud project, 1 unit each,
//! independent of the 10 000-unit pool every other endpoint shares
//! (<https://developers.google.com/youtube/v3/determine_quota_cost>).
//!
//! Sources: YouTube upload limits (12 hours / 256 GB), Shorts (≤ 60 s, square
//! or taller than wide), and the `videos.insert` quota bucket above.

use xpst_core::envelope::{video_envelope, VideoEnvelope, VideoMode};

/// The resumable upload endpoint. `uploadType=resumable` is appended by the
/// driver, because that query parameter is protocol, not configuration.
pub const UPLOAD_ENDPOINT: &str = "https://www.googleapis.com/upload/youtube/v3/videos";

/// Upload calls per day for one Cloud project (`videos.insert` bucket).
pub const UPLOADS_PER_DAY_PER_PROJECT: u32 = 100;

/// The quota bucket that ceiling belongs to.
pub const UPLOAD_QUOTA_BUCKET: &str = "videos.insert";

/// OAuth scopes the upload path needs. `.upload` is the minimum for
/// `videos.insert`; the other two match the Python adapter so a token minted
/// for either engine works for both.
pub const SCOPES: [&str; 3] = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
];

/// Google requires every chunk except the last to be a multiple of 256 KiB
/// (262 144 bytes). A request that ignores this is rejected with a 400, so the
/// driver refuses a bad chunk size instead of discovering it from the network.
pub const CHUNK_GRANULARITY_BYTES: u64 = 256 * 1024;

/// Chunk size for Shorts: small uploads, one round trip is usually enough.
pub const SHORTS_CHUNK_BYTES: u64 = 256 * 1024;

/// Chunk size for long-form: 8 MiB keeps a 12-hour upload to a few thousand
/// requests instead of tens of thousands.
pub const LONG_FORM_CHUNK_BYTES: u64 = 8 * 1024 * 1024;

/// `snippet.title` is capped at 100 characters by the API.
pub const MAX_TITLE_CHARS: usize = 100;
/// `snippet.description` is capped at 5 000 characters by the API. An
/// over-length description is refused with that number, never truncated.
pub const MAX_DESCRIPTION_CHARS: usize = 5_000;
/// `snippet.tags` allows at most 15 entries.
pub const MAX_TAGS: usize = 15;
/// Science & Technology — the category the Python adapter uses.
pub const DEFAULT_CATEGORY_ID: &str = "28";

/// The `part` parameter: the two writable parts this adapter sends.
pub const UPLOAD_PARTS: &str = "snippet,status";

/// Everything the adapter enforces, in one value.
///
/// Constructed only by [`capability`], which reads the video limits from the
/// shared registry. Fields are public so a planner can read them without a
/// call into this crate's internals.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Capability {
    /// Video envelope ceiling from the registry (256 GB).
    pub max_bytes: u64,
    /// Absolute duration ceiling from the registry (12 h).
    pub max_duration_seconds: u32,
    /// Short-form ceiling from the registry (60 s).
    pub shorts_max_duration_seconds: u32,
    /// Short-form aspect ceiling from the registry (width/height ≤ 1.0).
    pub shorts_max_aspect_ratio: f64,
    /// Uploads a day for one Cloud project (100, `videos.insert` bucket).
    pub uploads_per_day_per_project: u32,
    pub max_title_chars: usize,
    pub max_description_chars: usize,
    pub max_tags: usize,
    /// Chunks must be a multiple of this; the last chunk may be any size.
    pub chunk_granularity_bytes: u64,
    pub shorts_chunk_bytes: u64,
    pub long_form_chunk_bytes: u64,
    pub containers: &'static [&'static str],
    pub modes: &'static [VideoMode],
    pub endpoint: &'static str,
    pub scopes: &'static [&'static str],
}

impl Capability {
    /// The chunk size this mode uploads in.
    #[must_use]
    pub const fn chunk_size(self, mode: VideoMode) -> u64 {
        match mode {
            VideoMode::Shorts => self.shorts_chunk_bytes,
            VideoMode::Long => self.long_form_chunk_bytes,
        }
    }

    /// The duration ceiling this mode is held to.
    #[must_use]
    pub const fn duration_ceiling(self, mode: VideoMode) -> u32 {
        match mode {
            VideoMode::Shorts => self.shorts_max_duration_seconds,
            VideoMode::Long => self.max_duration_seconds,
        }
    }

    /// Internal contradictions, as stable codes. Empty means the declaration
    /// is self-consistent.
    ///
    /// This is the guard that keeps "[`Capability`] says X" from quietly
    /// becoming a second source of truth: every rule that must hold for the
    /// driver to be correct is checked here rather than assumed.
    #[must_use]
    pub fn gaps(self) -> Vec<&'static str> {
        let mut gaps = Vec::new();
        if self.shorts_max_duration_seconds == 0 {
            gaps.push("shorts_ceiling_unset");
        }
        if self.shorts_max_duration_seconds > self.max_duration_seconds {
            gaps.push("shorts_ceiling_above_long_ceiling");
        }
        if self.max_bytes == 0 {
            gaps.push("byte_ceiling_unset");
        }
        if !(self.shorts_max_aspect_ratio > 0.0 && self.shorts_max_aspect_ratio <= 1.0) {
            gaps.push("shorts_aspect_rule_missing");
        }
        if self.chunk_granularity_bytes == 0
            || !self
                .shorts_chunk_bytes
                .is_multiple_of(self.chunk_granularity_bytes)
            || !self
                .long_form_chunk_bytes
                .is_multiple_of(self.chunk_granularity_bytes)
        {
            gaps.push("chunk_size_not_a_granularity_multiple");
        }
        if self.uploads_per_day_per_project == 0 {
            gaps.push("upload_ceiling_unset");
        }
        if !self.modes.contains(&VideoMode::Shorts) || !self.modes.contains(&VideoMode::Long) {
            gaps.push("both_modes_must_be_declared");
        }
        gaps
    }
}

/// Why a capability declaration could not be built.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum CapabilityError {
    #[error("the capability registry has no YouTube video envelope")]
    MissingEnvelope,
    #[error("the YouTube envelope is missing a documented limit: {0}")]
    MissingLimit(&'static str),
}

/// The registry row this adapter enforces.
///
/// Failure means the shared registry lost YouTube — that is a programming
/// error or a bad merge, never something to paper over with a default, so this
/// returns an error rather than a plausible-looking guess.
pub fn envelope() -> Result<&'static VideoEnvelope, CapabilityError> {
    video_envelope("youtube").ok_or(CapabilityError::MissingEnvelope)
}

/// Build the capability declaration from the shared registry.
pub fn capability() -> Result<Capability, CapabilityError> {
    let envelope = envelope()?;
    let max_bytes = envelope
        .max_bytes
        .ok_or(CapabilityError::MissingLimit("max_bytes"))?;
    let max_duration_seconds = envelope
        .max_duration_seconds
        .ok_or(CapabilityError::MissingLimit("max_duration_seconds"))?;
    let shorts_max_duration_seconds = envelope
        .shorts_max_duration_seconds
        .ok_or(CapabilityError::MissingLimit("shorts_max_duration_seconds"))?;
    let shorts_max_aspect_ratio = envelope
        .shorts_max_aspect_ratio
        .ok_or(CapabilityError::MissingLimit("shorts_max_aspect_ratio"))?;
    Ok(Capability {
        max_bytes,
        max_duration_seconds,
        shorts_max_duration_seconds,
        shorts_max_aspect_ratio,
        uploads_per_day_per_project: UPLOADS_PER_DAY_PER_PROJECT,
        max_title_chars: MAX_TITLE_CHARS,
        max_description_chars: MAX_DESCRIPTION_CHARS,
        max_tags: MAX_TAGS,
        chunk_granularity_bytes: CHUNK_GRANULARITY_BYTES,
        shorts_chunk_bytes: SHORTS_CHUNK_BYTES,
        long_form_chunk_bytes: LONG_FORM_CHUNK_BYTES,
        containers: envelope.containers,
        modes: envelope.modes,
        endpoint: UPLOAD_ENDPOINT,
        scopes: &SCOPES,
    })
}

/// The capability declaration, or the reason it could not be built.
///
/// A convenience for callers that would rather have a clean error than an
/// unwrap.
pub fn capability_or_gaps() -> Result<Capability, Vec<&'static str>> {
    match capability() {
        Ok(capability) => {
            let gaps = capability.gaps();
            if gaps.is_empty() {
                Ok(capability)
            } else {
                Err(gaps)
            }
        }
        Err(error) => {
            // Surface the registry defect through the same channel: an empty
            // capability is exactly what the driver must never receive.
            let code = match error {
                CapabilityError::MissingEnvelope => "registry_youtube_envelope_missing",
                CapabilityError::MissingLimit(_) => "registry_limit_missing",
            };
            Err(vec![code])
        }
    }
}
