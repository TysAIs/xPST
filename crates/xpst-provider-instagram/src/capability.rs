//! What this destination can and cannot do, declared before anything is sent.
//!
//! The declaration exists so a caller can answer "can Instagram take this?"
//! without attempting a publish. The case that motivated it: Meta's feed-image
//! path takes a URL it fetches itself, so a local image can never be published
//! there — and learning that after an upload, as a provider error, is both
//! expensive and confusing. Asking the declaration first produces the refusal at
//! planning time, in words that name the alternative.

use crate::error::Refusal;
use crate::limits;
use crate::media::MediaSource;

/// How a content kind gets its bytes to Instagram.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MediaRequirement {
    /// The bytes can be uploaded (video's resumable endpoint) or fetched from a
    /// URL (the `video_url` form).
    LocalFileOrUrl,
    /// Meta fetches the media itself; only a public URL works.
    PublicUrlOnly,
}

impl MediaRequirement {
    /// Whether this requirement can carry that source.
    #[must_use]
    pub fn accepts(self, source: &MediaSource) -> bool {
        match self {
            Self::LocalFileOrUrl => true,
            Self::PublicUrlOnly => source.public_url().is_some(),
        }
    }

    /// The refusal for a source this requirement cannot carry.
    ///
    /// `what` names the thing the user asked for (`"a feed image"`), so the
    /// sentence reads like an answer to their question rather than a spec dump.
    #[must_use]
    pub fn refusal(self, source: &MediaSource, what: &str) -> Option<Refusal> {
        if self.accepts(source) {
            return None;
        }
        Some(Refusal::new(
            "instagram.image.needs_public_url",
            format!(
                "Instagram cannot take {what} from this computer. For feed images Meta downloads \
                 the picture from a public web address itself — there is no way to send it an \
                 image file, so {} would be refused after the upload started. Put the image at a \
                 public URL (any host, or a CDN) and post that URL, or publish the photo from the \
                 Instagram app.",
                source.describe()
            ),
        ))
    }
}

/// Instagram's publishing capability, as data.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CapabilityDeclaration {
    pub platform: &'static str,
    pub display_name: &'static str,
    /// Reels and other video: local resumable upload, or `video_url`.
    pub video_source: MediaRequirement,
    /// Feed image: public URL only.
    pub image_source: MediaRequirement,
    /// Carousel: each child follows its own kind's requirement, so the
    /// declaration states the compatible rule rather than a single answer.
    pub carousel_source: MediaRequirement,
    pub max_caption_chars: usize,
    pub max_hashtags: usize,
    pub max_mentions: usize,
    pub max_carousel_children: usize,
    pub max_posts_per_day: u32,
    /// One sentence on the access gate, so a caller knows this is not a
    /// self-serve API.
    pub note: &'static str,
}

/// The declaration for this provider.
#[must_use]
pub fn declaration() -> CapabilityDeclaration {
    CapabilityDeclaration {
        platform: limits::PLATFORM,
        display_name: "Instagram Reels",
        video_source: MediaRequirement::LocalFileOrUrl,
        image_source: MediaRequirement::PublicUrlOnly,
        // Carousel children are created one at a time, each by its own kind's
        // rule: an image child needs a public URL, a video child can be
        // uploaded. A single "carousel_source" answer would have to be false for
        // one of them, so it declares the looser rule and the per-item check is
        // what actually refuses.
        carousel_source: MediaRequirement::LocalFileOrUrl,
        max_caption_chars: limits::max_caption_chars(),
        max_hashtags: limits::MAX_HASHTAGS,
        max_mentions: limits::MAX_MENTIONS,
        max_carousel_children: limits::MAX_CAROUSEL_CHILDREN,
        max_posts_per_day: limits::MAX_POSTS_PER_DAY,
        note: "Instagram publishing needs an Instagram professional account and a Meta app with \
               instagram_content_publish; posting is gated, not self-serve.",
    }
}

/// The refusal for asking this destination to publish a local image.
///
/// Public because the preflight surfaces (CLI, HTTP, MCP) must reach the same
/// sentence without going through a publisher.
#[must_use]
pub fn local_image_refusal(source: &MediaSource) -> Option<Refusal> {
    declaration()
        .image_source
        .refusal(source, "a feed image from a local file")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn the_declaration_refuses_a_local_image_and_explains_why() {
        let refusal = local_image_refusal(&MediaSource::LocalFile(PathBuf::from("/tmp/p.jpg")))
            .expect("a local image must be refused by the declaration");
        assert_eq!(refusal.code, "instagram.image.needs_public_url");
        assert!(refusal.message.contains("public URL"));
        assert!(refusal.message.contains("Instagram app"));
    }

    #[test]
    fn a_public_image_url_is_accepted_by_the_declaration() {
        let source = MediaSource::PublicUrl("https://cdn.example.com/p.jpg".to_string());
        assert!(local_image_refusal(&source).is_none());
    }

    #[test]
    fn video_accepts_a_local_file() {
        let source = MediaSource::LocalFile(PathBuf::from("/tmp/clip.mp4"));
        assert!(declaration().video_source.accepts(&source));
    }

    #[test]
    fn the_declaration_reports_the_conservative_numbers() {
        let declaration = declaration();
        assert_eq!(declaration.max_carousel_children, 10);
        assert_eq!(declaration.max_posts_per_day, 50);
        assert_eq!(declaration.max_caption_chars, 2200);
        assert_eq!(declaration.max_hashtags, 30);
    }
}
