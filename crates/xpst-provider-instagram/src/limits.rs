//! Instagram's documented limits, enforced conservatively.
//!
//! Three of these numbers are published inconsistently by Meta. Each one is
//! resolved *downwards* and the reason is recorded next to the constant, because
//! a limit that changes without its justification is a limit nobody can review.

use std::path::Path;

use xpst_core::content::text_limit;
use xpst_core::envelope::video_envelope;
use xpst_core::ingest::ingest_spec;

use crate::error::Refusal;
use crate::media::{MediaMeasurements, MediaSource};

/// The platform key used by the shared capability table.
pub const PLATFORM: &str = "instagram";

/// Caption ceiling.
///
/// Read from the shared table (`xpst_core::content::text_limit`) rather than
/// restated, so the preflight, the capability document and this enforcement
/// cannot drift apart. The fallback exists only so this function is total.
#[must_use]
pub fn max_caption_chars() -> usize {
    text_limit(PLATFORM).unwrap_or(2200)
}

/// Hashtags per caption.
///
/// The IG User Media reference states it directly: "Maximum 2200 characters,
/// 30 hashtags, and 20 @ tags".
pub const MAX_HASHTAGS: usize = 30;

/// `@` mentions per caption, from the same sentence as [`MAX_HASHTAGS`].
pub const MAX_MENTIONS: usize = 20;

/// Carousel children — **10, not 20**.
///
/// Meta's Content Publishing guide says "up to 20 images, videos, or a
/// combination of the two" twice in its carousel section, and then says
/// "Carousels are limited to 20". The IG User Media reference's `children` row
/// says "An array of up to 10 container IDs … Carousels can have up to 10 total
/// images, videos, or a mix of the two". Two endpoints, two answers, same day.
///
/// We take 10. A carousel that is refused locally costs one edit; a carousel
/// sent to a platform whose own reference says 10 risks the platform dropping
/// items — and a silently shortened carousel is a published post that no longer
/// says what its author wrote.
pub const MAX_CAROUSEL_CHILDREN: usize = 10;

/// A carousel is at least two items; one item is an image or a Reel.
pub const MIN_CAROUSEL_CHILDREN: usize = 2;

/// API-published posts per 24 hours — **50, not 100**.
///
/// The Content Publishing guide's "Rate Limit" section says 100 API-published
/// posts per 24 hours (carousels counting as one). Its carousel section says
/// "Accounts are limited to 50 published posts within a 24-hour period", and the
/// `content_publishing_limit` reference documents `config.quota_total` as
/// "currently 50" with a 86,400-second window.
///
/// We take 50 because that is the number the platform's *own counter* reports:
/// publishing at 51 would be refused by Meta anyway, after the container was
/// created and the video uploaded. Stopping at the lower documented figure turns
/// a late, expensive failure into a refusal before upload. Being wrong in this
/// direction costs one post; being wrong in the other costs a wasted upload and
/// an intermittent error nobody can reproduce.
pub const MAX_POSTS_PER_DAY: u32 = 50;

/// Whether a caption is longer than Instagram accepts.
#[must_use]
pub fn caption_violation(caption: &str) -> Option<Refusal> {
    let length = caption.chars().count();
    let limit = max_caption_chars();
    (length > limit).then(|| {
        Refusal::new(
            "instagram.caption.too_long",
            format!(
                "The caption is {length} characters; Instagram accepts {limit}. \
                 Shorten it — xPST will not silently truncate a caption."
            ),
        )
    })
}

/// Number of hashtag tokens in a caption.
///
/// Counts every `#` that starts a token, without de-duplicating: Instagram's
/// ceiling is about how many tags the caption carries, and a caption that spells
/// the same tag twice still carries it twice. Over-counting refuses early, which
/// is the safe direction.
#[must_use]
pub fn hashtag_count(caption: &str) -> usize {
    let mut rest = caption.chars().peekable();
    let mut count = 0;
    while let Some(character) = rest.next() {
        if character == '#'
            && rest
                .peek()
                .is_some_and(|next| next.is_alphanumeric() || *next == '_')
        {
            count += 1;
        }
    }
    count
}

/// Number of `@` mention tokens in a caption, counted like [`hashtag_count`].
#[must_use]
pub fn mention_count(caption: &str) -> usize {
    let mut rest = caption.chars().peekable();
    let mut count = 0;
    while let Some(character) = rest.next() {
        if character == '@'
            && rest
                .peek()
                .is_some_and(|next| next.is_alphanumeric() || *next == '_' || *next == '.')
        {
            count += 1;
        }
    }
    count
}

/// Whether a caption carries too many hashtags.
#[must_use]
pub fn hashtag_violation(caption: &str) -> Option<Refusal> {
    let count = hashtag_count(caption);
    (count > MAX_HASHTAGS).then(|| {
        Refusal::new(
            "instagram.hashtag.too_many",
            format!(
                "The caption has {count} hashtags; Instagram accepts {MAX_HASHTAGS}. \
                 Remove {}.",
                count - MAX_HASHTAGS
            ),
        )
    })
}

/// Whether a caption carries too many `@` mentions.
#[must_use]
pub fn mention_violation(caption: &str) -> Option<Refusal> {
    let count = mention_count(caption);
    (count > MAX_MENTIONS).then(|| {
        Refusal::new(
            "instagram.mention.too_many",
            format!("The caption has {count} @mentions; Instagram accepts {MAX_MENTIONS}."),
        )
    })
}

/// Whether a carousel has an unusable number of children.
#[must_use]
pub fn carousel_violation(children: usize) -> Option<Refusal> {
    if children > MAX_CAROUSEL_CHILDREN {
        return Some(Refusal::new(
            "instagram.carousel.too_many",
            format!(
                "A carousel can hold at most {MAX_CAROUSEL_CHILDREN} items on Instagram \
                 (got {children}). Split it into two posts — xPST will not drop items silently."
            ),
        ));
    }
    if children < MIN_CAROUSEL_CHILDREN {
        return Some(Refusal::new(
            "instagram.carousel.too_few",
            format!(
                "A carousel needs at least {MIN_CAROUSEL_CHILDREN} items (got {children}). \
                 Post a single image or a Reel instead."
            ),
        ));
    }
    None
}

/// Whether this account has used up the 24-hour publishing allowance.
#[must_use]
pub fn daily_cap_violation(posts_in_last_24h: u32) -> Option<Refusal> {
    (posts_in_last_24h >= MAX_POSTS_PER_DAY).then(|| {
        Refusal::new(
            "instagram.daily_cap.reached",
            format!(
                "This account has already published {posts_in_last_24h} posts in the last 24 \
                 hours and xPST stops at {MAX_POSTS_PER_DAY}. Wait for the window to roll over \
                 — retrying now fails after the upload."
            ),
        )
    })
}

/// Whether a feed image violates Instagram's still-image contract.
///
/// The numbers come from the shared ingest table (`xpst_core::ingest`, itself
/// ported from `src/xpst/media/specs.py`): JPEG/JPEG only, 8 MB, aspect ratio
/// between 4:5 (0.8) and 1.91:1. Meta's media reference additionally mentions
/// PNG in its image specification while the publishing guide's Limitations say
/// "JPEG is the only image format supported"; the narrower reading is the one
/// that refuses early instead of failing after the upload.
#[must_use]
pub fn image_violation(path: Option<&Path>, measurements: &MediaMeasurements) -> Option<Refusal> {
    let spec = ingest_spec(PLATFORM)?;

    if measurements.bytes == Some(0) {
        return Some(Refusal::new(
            "instagram.image.empty",
            "The image file is empty; there is nothing for Instagram to fetch.",
        ));
    }

    if let Some(path) = path {
        if let Some(suffix) = path.extension() {
            let dotted = format!(".{}", suffix.to_string_lossy().to_ascii_lowercase());
            if !spec
                .image_containers
                .iter()
                .any(|allowed| *allowed == dotted)
            {
                return Some(Refusal::new(
                    "instagram.image.unsupported_container",
                    format!(
                        "Instagram feed images must be {}; this one is {dotted}.",
                        spec.image_containers.join(" or ")
                    ),
                ));
            }
        }
    }

    if let (Some(bytes), Some(limit)) = (measurements.bytes, spec.image_max_bytes) {
        if bytes > limit {
            return Some(Refusal::new(
                "instagram.image.too_large",
                format!(
                    "The image is {:.1} MB; Instagram accepts at most {:.1} MB.",
                    bytes as f64 / (1024.0 * 1024.0),
                    limit as f64 / (1024.0 * 1024.0)
                ),
            ));
        }
    }

    if let Some(aspect) = measurements.aspect() {
        let minimum = spec.image_aspect_min.unwrap_or(0.0);
        let maximum = spec.image_aspect_max.unwrap_or(f64::INFINITY);
        // Float comparison with a hair of tolerance: 1080/1350 must not be
        // refused because it landed on 0.7999999999999999.
        if aspect + 1e-9 < minimum || aspect - 1e-9 > maximum {
            return Some(Refusal::new(
                "instagram.image.aspect",
                format!(
                    "The image's aspect ratio is {aspect:.2}:1; Instagram accepts 4:5 (0.80) to \
                     1.91:1. Crop it into that range — Instagram would otherwise reject it or \
                     crop it for you."
                ),
            ));
        }
    }

    None
}

/// Whether a video violates Instagram's video envelope.
///
/// Duration and size come from the shared envelope (`xpst_core::envelope`,
/// ported from `xpst.content.VIDEO_ENVELOPES`): Reels up to 15 minutes, 4 GB,
/// MP4/MOV.
#[must_use]
pub fn video_violation(path: Option<&Path>, measurements: &MediaMeasurements) -> Option<Refusal> {
    let envelope = video_envelope(PLATFORM)?;

    if measurements.bytes == Some(0) {
        return Some(Refusal::new(
            "instagram.video.empty",
            "The video file is empty; there is nothing to upload.",
        ));
    }

    if let Some(path) = path {
        if let Some(suffix) = path.extension() {
            let dotted = format!(".{}", suffix.to_string_lossy().to_ascii_lowercase());
            if !envelope.containers.iter().any(|allowed| *allowed == dotted) {
                return Some(Refusal::new(
                    "instagram.video.unsupported_container",
                    format!(
                        "Instagram Reels must be {}; this file is {dotted}.",
                        envelope.containers.join(" or ")
                    ),
                ));
            }
        }
    }

    if let (Some(seconds), Some(limit)) =
        (measurements.duration_seconds, envelope.max_duration_seconds)
    {
        if seconds > f64::from(limit) {
            return Some(Refusal::new(
                "instagram.video.too_long",
                format!(
                    "The video is {:.0}s; Instagram Reels stop at {limit}s.",
                    seconds
                ),
            ));
        }
    }

    if let (Some(bytes), Some(limit)) = (measurements.bytes, envelope.max_bytes) {
        if bytes > limit {
            return Some(Refusal::new(
                "instagram.video.too_large",
                format!(
                    "The video is {:.2} GB; Instagram accepts at most {:.2} GB.",
                    bytes as f64 / (1024.0 * 1024.0 * 1024.0),
                    limit as f64 / (1024.0 * 1024.0 * 1024.0)
                ),
            ));
        }
    }

    None
}

/// Every caption rule, in the order a user would fix them.
#[must_use]
pub fn caption_check(caption: &str) -> Option<Refusal> {
    caption_violation(caption)
        .or_else(|| hashtag_violation(caption))
        .or_else(|| mention_violation(caption))
}

/// Refusal for a media file with no bytes.
///
/// A zero-byte upload would create a container, send nothing, and then wait for
/// processing that can never finish — a five-minute timeout for a mistake that
/// is visible in a file listing.
#[must_use]
pub fn empty_media_refusal() -> Refusal {
    Refusal::new(
        "instagram.media.empty",
        "That media file is empty, so there is nothing to upload.",
    )
}

/// The media check for one source, by kind.
#[must_use]
pub fn media_check(source: &MediaSource, measurements: &MediaMeasurements) -> Option<Refusal> {
    match source {
        MediaSource::LocalFile(path) => {
            // A local file's size is knowable without the caller, and a size
            // limit that is only enforced when a caller volunteers a number is
            // not enforced. Metadata is cheap and reads no bytes.
            let mut measured = *measurements;
            if measured.bytes.is_none() {
                measured.bytes = std::fs::metadata(path).ok().map(|metadata| metadata.len());
            }
            if is_image_path(path) {
                image_violation(Some(path), &measured)
            } else {
                video_violation(Some(path), &measured)
            }
        }
        // A URL is measured by whoever uploaded it; only the format-free rules
        // can be checked, and the platform's own limits still apply.
        MediaSource::PublicUrl(_) => None,
    }
}

/// Whether a path names a still image (extension only, no I/O).
#[must_use]
pub fn is_image_path(path: &Path) -> bool {
    path.extension()
        .map(|suffix| suffix.to_string_lossy().to_ascii_lowercase())
        .is_some_and(|suffix| matches!(suffix.as_str(), "jpg" | "jpeg" | "png" | "webp" | "gif"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_caption_limit_is_the_shared_table_number() {
        assert_eq!(max_caption_chars(), 2200);
    }

    #[test]
    fn caption_length_is_counted_in_characters_not_bytes() {
        let caption = "é".repeat(2200);
        assert!(caption_violation(&caption).is_none(), "2200 chars must fit");
        let caption = "é".repeat(2201);
        assert!(caption_violation(&caption).is_some());
    }

    #[test]
    fn hashtags_are_counted_without_deduplication() {
        assert_eq!(hashtag_count("#a #b #c"), 3);
        assert_eq!(hashtag_count("#a #a"), 2);
        assert_eq!(hashtag_count("no tags here"), 0);
        assert_eq!(hashtag_count("# not a tag"), 0);
    }

    #[test]
    fn thirty_hashtags_fit_and_thirty_one_do_not() {
        let ok = (0..30)
            .map(|index| format!("#tag{index}"))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(hashtag_violation(&ok).is_none());
        let too_many = format!("{ok} #again");
        assert!(hashtag_violation(&too_many).is_some());
    }

    #[test]
    fn carousel_bounds_are_ten_and_two() {
        assert!(carousel_violation(10).is_none());
        assert!(carousel_violation(11).is_some());
        assert!(carousel_violation(2).is_none());
        assert!(carousel_violation(1).is_some());
    }

    #[test]
    fn daily_cap_refuses_at_fifty_not_above_it() {
        assert!(daily_cap_violation(49).is_none());
        assert!(daily_cap_violation(50).is_some());
    }

    #[test]
    fn image_aspect_bounds_are_the_documented_ones() {
        let portrait = MediaMeasurements {
            width: Some(1080),
            height: Some(1350),
            ..MediaMeasurements::default()
        };
        assert!(image_violation(None, &portrait).is_none());

        let too_tall = MediaMeasurements {
            width: Some(1080),
            height: Some(1920),
            ..MediaMeasurements::default()
        };
        assert!(image_violation(None, &too_tall).is_some());

        let too_wide = MediaMeasurements {
            width: Some(1920),
            height: Some(900),
            ..MediaMeasurements::default()
        };
        assert!(image_violation(None, &too_wide).is_some());
    }

    #[test]
    fn video_limits_come_from_the_shared_envelope() {
        let too_long = MediaMeasurements {
            duration_seconds: Some(901.0),
            ..MediaMeasurements::default()
        };
        assert!(video_violation(None, &too_long).is_some());

        let fine = MediaMeasurements {
            duration_seconds: Some(900.0),
            ..MediaMeasurements::default()
        };
        assert!(video_violation(None, &fine).is_none());
    }
}
