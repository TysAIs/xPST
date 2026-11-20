//! Canonical publish vocabulary, ported from `src/xpst/content.py`.
//!
//! The Python module is the source of truth for what xPST can publish and to
//! which destination; this is a behaviour-for-behaviour port of the parts the
//! Rust engine needs first (content types, per-destination capability rows).
//! Every value here is asserted against a fixture captured by running the
//! Python functions (`crates/xpst-core/tests/content_parity.rs`), so a change on
//! either side turns into a red test rather than silent drift.
//!
//! Nothing in this module performs I/O: the capability questions are answered
//! before any network call, which is the whole point of the contract.

use std::fmt;

use crate::envelope::{self, VideoEnvelope};

/// The content types xPST can reason about.
///
/// `Thread` is a distinct type rather than an alias for `Text` because X
/// declares it by name: a text-only thread has no implementation, and saying so
/// explicitly is the point of the contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ContentType {
    Video,
    Image,
    Carousel,
    Text,
    Thread,
}

/// Canonical order used by the capability matrix, tests and surfaces.
pub const CONTENT_TYPES: [ContentType; 5] = [
    ContentType::Video,
    ContentType::Image,
    ContentType::Carousel,
    ContentType::Text,
    ContentType::Thread,
];

impl ContentType {
    /// The canonical spelling. It is the only one xPST ever emits.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Video => "video",
            Self::Image => "image",
            Self::Carousel => "carousel",
            Self::Text => "text",
            Self::Thread => "thread",
        }
    }

    /// Human-facing noun used in messages: `"text posts"`.
    #[must_use]
    pub fn label(self) -> String {
        format!("{} posts", self.as_str())
    }
}

impl fmt::Display for ContentType {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Accepted spellings mapped onto the canonical vocabulary. Aliases exist so a
/// caller saying "reel" or "photo" is understood rather than rejected; the
/// canonical `as_str` value is the only spelling xPST ever emits.
const CONTENT_TYPE_ALIASES: [(&str, ContentType); 20] = [
    ("video", ContentType::Video),
    ("videos", ContentType::Video),
    ("short", ContentType::Video),
    ("shorts", ContentType::Video),
    ("reel", ContentType::Video),
    ("reels", ContentType::Video),
    ("image", ContentType::Image),
    ("images", ContentType::Image),
    ("photo", ContentType::Image),
    ("photos", ContentType::Image),
    ("picture", ContentType::Image),
    ("carousel", ContentType::Carousel),
    ("album", ContentType::Carousel),
    ("gallery", ContentType::Carousel),
    ("text", ContentType::Text),
    ("status", ContentType::Text),
    ("post", ContentType::Text),
    ("tweet", ContentType::Text),
    ("thread", ContentType::Thread),
    ("threads", ContentType::Thread),
];

/// Base-class equivalent of the Python `ContentContractError`: an input that is
/// not part of the vocabulary is refused by name, never guessed at.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContentContractError {
    /// The content-type spelling is not in the vocabulary.
    UnknownContentType(String),
    /// The video-mode spelling is not `shorts` or `long`.
    UnknownVideoMode(String),
}

impl fmt::Display for ContentContractError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownContentType(value) => {
                let known = CONTENT_TYPES
                    .iter()
                    .map(|item| item.as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                write!(
                    formatter,
                    "Unknown content type '{value}': choose one of {known}."
                )
            }
            Self::UnknownVideoMode(value) => write!(
                formatter,
                "Unknown video mode '{value}': choose one of shorts, long."
            ),
        }
    }
}

impl std::error::Error for ContentContractError {}

/// Return the canonical [`ContentType`] for a caller-supplied value.
///
/// Dash and space spellings are understood (`"image-carousel"`), matching the
/// Python `coerce_content_type` key normalisation exactly.
pub fn coerce_content_type(value: &str) -> Result<ContentType, ContentContractError> {
    let key = value.trim().to_ascii_lowercase().replace(['-', ' '], "_");
    CONTENT_TYPE_ALIASES
        .iter()
        .find(|(label, _)| *label == key)
        .map(|(_, content_type)| *content_type)
        .ok_or_else(|| ContentContractError::UnknownContentType(value.to_owned()))
}

/// How a destination can receive the bytes of a media file.
///
/// This is the second half of a capability question: a destination can support
/// a content type and still be unable to take the file xPST has, because it
/// fetches the media itself instead of accepting an upload.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MediaTransport {
    /// xPST sends the bytes (multipart, resumable, chunked).
    LocalUpload,
    /// The destination retrieves the media from a public URL, which a local
    /// tool cannot provide: no local file can ever be published there.
    PublicUrl,
}

impl MediaTransport {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::LocalUpload => "local_upload",
            Self::PublicUrl => "public_url",
        }
    }
}

/// Stable code for "this destination cannot take the local file you gave it".
pub const MEDIA_TRANSPORT_ERROR_CODE: &str = "content_type.media_transport";

/// The exact requirement for a destination that fetches media itself.
///
/// Copied verbatim from `xpst.content.THREADS_PUBLIC_URL_REQUIREMENT`, which is
/// grounded in Meta's Threads API docs: the container takes a `video_url`, so
/// the media must already be on a public server. It is quoted in refusal
/// messages instead of paraphrased — a user acting on the refusal needs the
/// platform's own constraint, not xPST's summary of it.
pub const THREADS_PUBLIC_URL_REQUIREMENT: &str = "Threads has no upload endpoint: its API fetches your media from a public server, and xPST is a local tool with no server to host a file for it. A local file cannot be published to Threads, and xPST cannot deliver a media URL either yet — post this content from the Threads app instead.";

/// Maximum number of media items one post may carry (`MAX_MEDIA_ITEMS`).
pub const MAX_MEDIA_ITEMS: usize = 10;

/// File suffixes xPST treats as video (classification is by extension only —
/// no ffprobe, no I/O — so it is safe in a side-effect-free preflight).
pub const VIDEO_EXTENSIONS: [&str; 8] = [
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v",
];

/// File suffixes xPST treats as still images.
pub const IMAGE_EXTENSIONS: [&str; 7] = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"];

/// What kind of media a path carries, as far as the contract can tell.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MediaKind {
    Video,
    Image,
    /// Neither a known video nor a known image suffix: the caller's content
    /// type decides what to attempt, never an invented guess.
    Unknown,
}

impl MediaKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Video => "video",
            Self::Image => "image",
            Self::Unknown => "unknown",
        }
    }
}

impl fmt::Display for MediaKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// Classify one media path as video, image or unknown, by extension.
///
/// Mirrors `xpst.content.media_kind`: only the final dotted suffix counts, and
/// hidden-file names without a second dot have no suffix.
#[must_use]
pub fn media_kind(path: &str) -> MediaKind {
    let suffix = file_suffix(path);
    if VIDEO_EXTENSIONS.contains(&suffix.as_str()) {
        MediaKind::Video
    } else if IMAGE_EXTENSIONS.contains(&suffix.as_str()) {
        MediaKind::Image
    } else {
        MediaKind::Unknown
    }
}

/// Lowercase dotted suffix of a path, or `""` — `pathlib.Path(...).suffix`.
#[must_use]
pub fn file_suffix(path: &str) -> String {
    let name = path.rsplit(['/', '\\']).next().unwrap_or(path);
    match name.rfind('.') {
        // `.bashrc` is a name, not an extension.
        Some(0) | None => String::new(),
        Some(index) => name[index..].to_ascii_lowercase(),
    }
}

/// Maximum characters one post's text may carry, per destination.
///
/// This is THE single source for the limit: the uploaders import it, each
/// provider manifest reports it, and a refusal quotes it. A destination absent
/// from this table has no verified limit, so nothing is checked for it — xPST
/// does not invent a character count it has not confirmed.
pub const TEXT_LIMITS: [(&str, usize); 6] = [
    ("x", 280),
    ("threads", 500),
    ("instagram", 2200),
    ("tiktok", 2200),
    ("messenger", 640),
    ("bluesky", 300),
];

/// The verified maximum text length for a destination, or `None`.
#[must_use]
pub fn text_limit(platform: &str) -> Option<usize> {
    let key = platform.trim().to_ascii_lowercase();
    TEXT_LIMITS
        .iter()
        .find(|(name, _)| *name == key)
        .map(|(_, limit)| *limit)
}

/// One destination's content capabilities: what it claims versus what the
/// engine can really do.
#[derive(Debug, Clone, Copy)]
pub struct DestinationContentProfile {
    pub platform: &'static str,
    pub display_name: &'static str,
    /// `false` for a messaging-only destination (Messenger), which is never a
    /// publishing target.
    pub publishing: bool,
    pub media_transport: MediaTransport,
    /// Labels the provider manifest declares (may be false claims).
    pub declared: &'static [ContentType],
    /// Content types **this workspace** has real publishing code for.
    ///
    /// Validation uses this set, never `declared`. It is empty for a
    /// destination that is declared but has no provider crate here (TikTok,
    /// Threads, Facebook, Rumble, Messenger): the planner refuses those with
    /// [`crate::plan::UNIMPLEMENTED_DESTINATION_CODE`] instead of accepting a
    /// destination nothing can publish to. [`provider_crate`] is the
    /// machine-checkable half of the same claim, and
    /// `tests/capability_truth.rs` fails if the two ever disagree.
    pub implemented: &'static [ContentType],
}

impl DestinationContentProfile {
    /// Whether the engine has a real implementation for this content type.
    #[must_use]
    pub fn supports(&self, content_type: ContentType) -> bool {
        self.implemented.contains(&content_type)
    }

    /// Whether this destination declares the type in its manifest.
    #[must_use]
    pub fn declares(&self, content_type: ContentType) -> bool {
        self.declared.contains(&content_type)
    }

    /// False declarations: claimed in the manifest, no code behind them.
    #[must_use]
    pub fn declared_but_unimplemented(&self) -> Vec<ContentType> {
        self.declared
            .iter()
            .copied()
            .filter(|item| !self.implemented.contains(item))
            .collect()
    }

    /// Whether a local file can reach this destination at all.
    #[must_use]
    pub fn accepts_local_media(&self) -> bool {
        self.media_transport == MediaTransport::LocalUpload
    }

    /// The declared video envelope for this destination, if it publishes video.
    #[must_use]
    pub fn video_envelope(&self) -> Option<&'static VideoEnvelope> {
        envelope::video_envelope(self.platform)
    }
}

/// THE capability table, mirroring `DESTINATION_CONTENT_PROFILES`.
///
/// `declared` is what the platform's own manifest claims and matches Python's
/// table exactly. `implemented` is the honest half: it lists only the content
/// types a crate **in this workspace** can actually publish. TikTok, Threads,
/// Facebook, Rumble and Messenger are declared-but-unimplemented here — Python
/// has adapters for them, this engine has none — so they carry `&[]` and the
/// planner refuses them rather than accepting a destination nothing can publish
/// to.
pub static DESTINATION_CONTENT_PROFILES: [DestinationContentProfile; 9] = [
    DestinationContentProfile {
        platform: "youtube",
        display_name: "YouTube",
        publishing: true,
        media_transport: MediaTransport::LocalUpload,
        declared: &[ContentType::Video],
        implemented: &[ContentType::Video],
    },
    DestinationContentProfile {
        platform: "x",
        display_name: "X",
        publishing: true,
        media_transport: MediaTransport::LocalUpload,
        declared: &[
            ContentType::Video,
            ContentType::Carousel,
            ContentType::Text,
            ContentType::Image,
        ],
        implemented: &[
            ContentType::Video,
            ContentType::Carousel,
            ContentType::Text,
            ContentType::Image,
        ],
    },
    DestinationContentProfile {
        platform: "instagram",
        display_name: "Instagram Reels",
        publishing: true,
        media_transport: MediaTransport::LocalUpload,
        declared: &[
            ContentType::Video,
            ContentType::Image,
            ContentType::Carousel,
        ],
        implemented: &[
            ContentType::Video,
            ContentType::Carousel,
            ContentType::Image,
        ],
    },
    DestinationContentProfile {
        platform: "tiktok",
        display_name: "TikTok",
        publishing: true,
        media_transport: MediaTransport::LocalUpload,
        declared: &[ContentType::Video],
        implemented: &[],
    },
    DestinationContentProfile {
        platform: "threads",
        display_name: "Threads",
        publishing: true,
        media_transport: MediaTransport::PublicUrl,
        declared: &[ContentType::Video, ContentType::Text],
        implemented: &[],
    },
    DestinationContentProfile {
        platform: "facebook",
        display_name: "Facebook Page",
        publishing: true,
        media_transport: MediaTransport::LocalUpload,
        declared: &[ContentType::Video],
        implemented: &[],
    },
    DestinationContentProfile {
        platform: "rumble",
        display_name: "Rumble",
        publishing: true,
        media_transport: MediaTransport::LocalUpload,
        declared: &[ContentType::Video],
        implemented: &[],
    },
    DestinationContentProfile {
        platform: "bluesky",
        display_name: "Bluesky",
        publishing: true,
        media_transport: MediaTransport::LocalUpload,
        declared: &[ContentType::Text, ContentType::Image, ContentType::Carousel],
        implemented: &[ContentType::Text, ContentType::Image, ContentType::Carousel],
    },
    DestinationContentProfile {
        platform: "messenger",
        display_name: "Messenger",
        publishing: false,
        media_transport: MediaTransport::LocalUpload,
        declared: &[ContentType::Text],
        implemented: &[],
    },
];

/// The workspace crate that publishes to a destination, when one exists.
///
/// This is the machine-checkable form of "implemented": a destination is only
/// implemented if a crate in this workspace carries a real publish path for it.
/// `None` is a truthful answer, not an error — the destination is declared and
/// this engine cannot publish to it yet.
pub const DESTINATION_PROVIDER_CRATES: [(&str, Option<&str>); 9] = [
    ("youtube", Some("xpst-provider-youtube")),
    ("x", Some("xpst-provider-x")),
    ("instagram", Some("xpst-provider-instagram")),
    // No Rust provider exists for these. Python does; this engine does not, and
    // saying so is the point of the table.
    ("tiktok", None),
    ("threads", None),
    ("facebook", None),
    ("rumble", None),
    ("bluesky", Some("xpst-provider-bluesky")),
    ("messenger", None),
];

/// The provider crate for a destination, or `None` when nothing here publishes
/// to it.
#[must_use]
pub fn provider_crate(platform: &str) -> Option<&'static str> {
    let key = platform.trim().to_ascii_lowercase();
    DESTINATION_PROVIDER_CRATES
        .iter()
        .find(|(name, _)| *name == key)
        .and_then(|(_, krate)| *krate)
}

/// Return the capability profile for a destination, or `None` if unknown.
///
/// An unknown destination is *not* assumed capable.
#[must_use]
pub fn content_profile(platform: &str) -> Option<&'static DestinationContentProfile> {
    let key = platform.trim().to_ascii_lowercase();
    DESTINATION_CONTENT_PROFILES
        .iter()
        .find(|profile| profile.platform == key)
}

/// Content types the destination's manifest declares (may be false).
#[must_use]
pub fn declared_content_types(platform: &str) -> Vec<ContentType> {
    content_profile(platform).map_or_else(Vec::new, |profile| profile.declared.to_vec())
}

/// Content types the destination can actually publish today.
#[must_use]
pub fn implemented_content_types(platform: &str) -> Vec<ContentType> {
    content_profile(platform).map_or_else(Vec::new, |profile| profile.implemented.to_vec())
}
