//! The Bluesky product contract as data: what the destination accepts, and what
//! this adapter refuses.
//!
//! Every number enforced by [`crate::BlueskyProvider`] is read from here, so a
//! refusal message and the capability a caller reads can never disagree. The
//! values come from the AT Protocol lexicons, not from us:
//!
//! | limit | value | source |
//! |---|---|---|
//! | text | 300 graphemes | `app.bsky.feed.post` `text` field |
//! | text | 3,000 bytes | `app.bsky.feed.post` `text` field |
//! | images per post | 4 | `app.bsky.embed.images` `images` array |
//! | bytes per image | 2,000,000 | `app.bsky.embed.images#image` `image` |
//!
//! Video is **not** a record this adapter can write: Bluesky takes video only
//! through a separate video service, asynchronously, under a service-auth
//! token. [`VideoRoute`] says so in data instead of pretending otherwise.

use core::fmt;

use unicode_segmentation::UnicodeSegmentation;

/// The AT Protocol collection every record this adapter writes belongs to.
pub const POST_COLLECTION: &str = "app.bsky.feed.post";

/// Lexicon cap on `app.bsky.feed.post#text`, counted in grapheme clusters.
pub const MAX_GRAPHEMES: usize = 300;

/// Lexicon cap on `app.bsky.feed.post#text`, counted in UTF-8 bytes.
///
/// The two caps are both real: a post has to satisfy the grapheme count *and*
/// the byte count, which is why a string of short graphemes can be under 300
/// graphemes and still be refused.
pub const MAX_TEXT_BYTES: usize = 3_000;

/// Lexicon cap on the `app.bsky.embed.images#images` array.
pub const MAX_IMAGES_PER_POST: usize = 4;

/// Lexicon cap on a single uploaded image blob.
pub const MAX_IMAGE_BYTES: usize = 2_000_000;

/// Bluesky's video upload service. Video is never a PDS record write.
pub const VIDEO_SERVICE: &str = "https://video.bsky.app";

/// Image types the PDS accepts as blobs. Anything else is refused by name
/// rather than uploaded and rejected upstream.
pub const ACCEPTED_IMAGE_MIME_TYPES: &[&str] =
    &["image/jpeg", "image/png", "image/webp", "image/gif"];

/// Text ceilings for one post.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextCapability {
    /// Maximum grapheme clusters.
    pub max_graphemes: usize,
    /// Maximum UTF-8 bytes.
    pub max_bytes: usize,
}

/// Image ceilings for one post.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImageCapability {
    /// Maximum images attached to one post.
    pub max_images_per_post: usize,
    /// Maximum size of one uploaded image.
    pub max_bytes_per_image: usize,
    /// MIME types the adapter will upload.
    pub accepted_mime_types: &'static [&'static str],
}

/// How video is (not) published.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VideoRoute {
    /// This adapter cannot publish video at all.
    Unsupported,
    /// Video exists on the platform, but only through a separate service that
    /// this adapter does not drive.
    VideoService {
        /// The service a video has to be published through.
        service: &'static str,
    },
}

impl VideoRoute {
    /// The declared behaviour, for a refusal message that has to explain it.
    pub fn describe(self) -> &'static str {
        match self {
            Self::Unsupported => "video is not supported",
            Self::VideoService { .. } => {
                "video is published only through Bluesky's video service, \
                 not through the post record this adapter writes"
            }
        }
    }
}

/// Everything this adapter claims it can do, with the numbers it enforces.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlueskyCapabilities {
    /// Text ceilings.
    pub text: TextCapability,
    /// Image ceilings.
    pub images: ImageCapability,
    /// Whether a native multi-image post exists.
    pub carousel: bool,
    /// Whether a published post can be deleted.
    pub delete: bool,
    /// Whether deleting is reversible. It is not: AT Protocol has no unpublish.
    pub soft_delete: bool,
    /// Where video goes.
    pub video: VideoRoute,
}

impl BlueskyCapabilities {
    /// The reference declaration: the only place these numbers are written.
    pub const REFERENCE: Self = Self {
        text: TextCapability {
            max_graphemes: MAX_GRAPHEMES,
            max_bytes: MAX_TEXT_BYTES,
        },
        images: ImageCapability {
            max_images_per_post: MAX_IMAGES_PER_POST,
            max_bytes_per_image: MAX_IMAGE_BYTES,
            accepted_mime_types: ACCEPTED_IMAGE_MIME_TYPES,
        },
        carousel: true,
        delete: true,
        soft_delete: false,
        video: VideoRoute::VideoService {
            service: VIDEO_SERVICE,
        },
    };
}

impl Default for BlueskyCapabilities {
    fn default() -> Self {
        Self::REFERENCE
    }
}

/// Why a publish never happened. Every one of these is a refusal, not a
/// truncation: the caller gets the post back untouched and has to decide.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefusalCode {
    /// The post has no text and no image.
    EmptyText,
    /// More grapheme clusters than the lexicon allows.
    TextTooManyGraphemes,
    /// More UTF-8 bytes than the lexicon allows.
    TextTooManyBytes,
    /// No images at all were supplied.
    NoImages,
    /// More images than one post can carry.
    TooManyImages,
    /// One image is above the per-image byte cap.
    ImageTooLarge,
    /// One image is not a type the PDS accepts.
    ImageUnsupportedType,
    /// Video was requested, and video is not a record this adapter writes.
    VideoNeedsVideoService,
}

impl RefusalCode {
    /// The stable machine code carried in logs and metrics.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::EmptyText => "empty_text",
            Self::TextTooManyGraphemes => "text_too_many_graphemes",
            Self::TextTooManyBytes => "text_too_many_bytes",
            Self::NoImages => "no_images",
            Self::TooManyImages => "too_many_images",
            Self::ImageTooLarge => "image_too_large",
            Self::ImageUnsupportedType => "image_unsupported_type",
            Self::VideoNeedsVideoService => "video_needs_video_service",
        }
    }
}

/// A refusal: a machine code, a message that names the measured number *and*
/// the limit, and those two numbers as fields so a caller never has to parse
/// prose out of a log line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    /// Why.
    pub code: RefusalCode,
    /// What to tell a human, quoting what was measured and what is allowed.
    pub message: String,
    /// The number that was measured, when one exists.
    pub measured: Option<usize>,
    /// The ceiling it broke, when one exists.
    pub limit: Option<usize>,
}

impl Refusal {
    fn new(
        code: RefusalCode,
        message: String,
        measured: Option<usize>,
        limit: Option<usize>,
    ) -> Self {
        Self {
            code,
            message,
            measured,
            limit,
        }
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

/// Both text measurements, taken once so a caller cannot disagree with us about
/// which one tripped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextMeasurement {
    /// Grapheme clusters, per UAX #29.
    pub graphemes: usize,
    /// UTF-8 bytes.
    pub bytes: usize,
}

/// Measure a post body the way the lexicon does.
pub fn measure_text(text: &str) -> TextMeasurement {
    TextMeasurement {
        graphemes: text.graphemes(true).count(),
        bytes: text.len(),
    }
}

/// `2000000` renders as `2,000,000` so a refusal quotes the number a reader
/// sees in the docs.
pub fn thousands(value: usize) -> String {
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

fn length_refusal(measured: TextMeasurement) -> Option<Refusal> {
    if measured.graphemes > MAX_GRAPHEMES {
        return Some(Refusal::new(
            RefusalCode::TextTooManyGraphemes,
            format!(
                "BLUESKY_TEXT_TOO_LONG: the text is {} graphemes; Bluesky accepts at most {} \
                 graphemes per post. xPST refuses rather than truncating — remove at least {} \
                 grapheme(s) and post again.",
                thousands(measured.graphemes),
                thousands(MAX_GRAPHEMES),
                thousands(measured.graphemes - MAX_GRAPHEMES)
            ),
            Some(measured.graphemes),
            Some(MAX_GRAPHEMES),
        ));
    }
    if measured.bytes > MAX_TEXT_BYTES {
        return Some(Refusal::new(
            RefusalCode::TextTooManyBytes,
            format!(
                "BLUESKY_TEXT_TOO_LONG_BYTES: the text is {} bytes of UTF-8; Bluesky accepts at \
                 most {} bytes per post. xPST refuses rather than truncating — shorten it by at \
                 least {} byte(s) and post again.",
                thousands(measured.bytes),
                thousands(MAX_TEXT_BYTES),
                thousands(measured.bytes - MAX_TEXT_BYTES)
            ),
            Some(measured.bytes),
            Some(MAX_TEXT_BYTES),
        ));
    }
    None
}

/// Check a text-only post: it must have content, and it must fit both caps.
pub fn check_text(text: &str) -> Result<TextMeasurement, Refusal> {
    if text.trim().is_empty() {
        return Err(Refusal::new(
            RefusalCode::EmptyText,
            "BLUESKY_EMPTY_TEXT: a Bluesky post needs text or an image; this one has neither. \
             xPST refuses to write a blank record."
                .to_owned(),
            Some(0),
            None,
        ));
    }
    let measured = measure_text(text);
    match length_refusal(measured) {
        Some(refusal) => Err(refusal),
        None => Ok(measured),
    }
}

/// Check an image caption. An empty caption is legitimate — a post may carry
/// only an embed — so only the lengths are enforced.
pub fn check_caption(caption: &str) -> Result<TextMeasurement, Refusal> {
    let measured = measure_text(caption);
    match length_refusal(measured) {
        Some(refusal) => Err(refusal),
        None => Ok(measured),
    }
}

/// Refuse an image count a single post cannot carry, before anything is uploaded.
pub fn check_image_count(count: usize) -> Result<(), Refusal> {
    if count == 0 {
        return Err(Refusal::new(
            RefusalCode::NoImages,
            "BLUESKY_NO_IMAGES: an image post needs at least one image; this request has none."
                .to_owned(),
            Some(0),
            Some(MAX_IMAGES_PER_POST),
        ));
    }
    if count > MAX_IMAGES_PER_POST {
        return Err(Refusal::new(
            RefusalCode::TooManyImages,
            format!(
                "BLUESKY_TOO_MANY_IMAGES: this request carries {} images; Bluesky attaches at \
                 most {} images per post. xPST will not drop the extras — split it into two \
                 posts.",
                thousands(count),
                thousands(MAX_IMAGES_PER_POST)
            ),
            Some(count),
            Some(MAX_IMAGES_PER_POST),
        ));
    }
    Ok(())
}

/// The MIME type implied by a filename, or `None` when Bluesky would not take it.
pub fn mime_for_filename(filename: &str) -> Option<&'static str> {
    let extension = filename
        .rsplit_once('.')
        .map(|(_, ext)| ext.to_ascii_lowercase())?;
    match extension.as_str() {
        "jpg" | "jpeg" => Some("image/jpeg"),
        "png" => Some("image/png"),
        "webp" => Some("image/webp"),
        "gif" => Some("image/gif"),
        _ => None,
    }
}

/// Check one image against the type and byte caps.
///
/// Returns the MIME type to upload it as. Both checks happen before any network
/// call, so a refused image never becomes a half-published post.
pub fn check_image(filename: &str, bytes: &[u8]) -> Result<&'static str, Refusal> {
    let Some(mime) = mime_for_filename(filename) else {
        let extension = filename
            .rsplit_once('.')
            .map(|(_, ext)| ext.to_ascii_lowercase())
            .unwrap_or_else(|| "no extension".to_owned());
        return Err(Refusal::new(
            RefusalCode::ImageUnsupportedType,
            format!(
                "BLUESKY_IMAGE_REJECTED: Bluesky does not accept {extension} images: it takes \
                 JPG, PNG, WEBP or GIF. Convert {filename} and try again."
            ),
            None,
            None,
        ));
    };
    if bytes.len() > MAX_IMAGE_BYTES {
        return Err(Refusal::new(
            RefusalCode::ImageTooLarge,
            format!(
                "BLUESKY_IMAGE_TOO_LARGE: {filename} is {} bytes; Bluesky accepts at most {} \
                 bytes ({}) per image. xPST will not re-encode it silently — resize the file and \
                 try again.",
                thousands(bytes.len()),
                thousands(MAX_IMAGE_BYTES),
                "2 MB"
            ),
            Some(bytes.len()),
            Some(MAX_IMAGE_BYTES),
        ));
    }
    Ok(mime)
}

/// The refusal a video request gets, naming the service that would have to do
/// the work.
pub fn video_refusal() -> Refusal {
    Refusal::new(
        RefusalCode::VideoNeedsVideoService,
        format!(
            "BLUESKY_VIDEO_NEEDS_VIDEO_SERVICE: Bluesky does not take video through \
             {POST_COLLECTION}. Video goes to Bluesky's video service at {VIDEO_SERVICE} as an \
             asynchronous job under a service-auth token, which this adapter does not drive. xPST \
             will not silently publish the post without it — route the video to a destination \
             that takes video directly."
        ),
        None,
        None,
    )
}
