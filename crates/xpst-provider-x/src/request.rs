//! What the caller is asking X to publish.
//!
//! A request carries its own identity: the account, the text verbatim, optional
//! media, and the asset key that makes cross-account duplicate detection
//! possible. Nothing is inferred from a filesystem path and nothing is edited —
//! the text that is priced is the text that is sent.

use crate::cost::Cost;
use crate::media::Media;

/// One planned post.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishRequest {
    /// X user id of the publishing account. An id, not a handle: handles change.
    pub account: String,
    /// The text exactly as the user wrote it.
    pub text: String,
    /// Media bytes, when the post is not text-only.
    pub media: Option<Media>,
    /// Content identity used by the cross-account duplicate rule. Required for
    /// media posts; ignored for text-only posts.
    pub asset_key: Option<String>,
}

impl PublishRequest {
    /// A text-only post.
    #[must_use]
    pub fn text(account: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            account: account.into(),
            text: text.into(),
            media: None,
            asset_key: None,
        }
    }

    #[must_use]
    pub fn with_media(mut self, media: Media) -> Self {
        self.media = Some(media);
        self
    }

    #[must_use]
    pub fn with_asset_key(mut self, asset_key: impl Into<String>) -> Self {
        self.asset_key = Some(asset_key.into());
        self
    }

    /// The measured length, counted the way the reference implementation counts
    /// it: Unicode scalar values (`len(text)` in Python).
    #[must_use]
    pub fn text_length(&self) -> usize {
        self.text.chars().count()
    }

    /// Whether the text contains a URL, and therefore costs the URL price.
    #[must_use]
    pub fn has_url(&self) -> bool {
        Cost::for_text(&self.text).has_url
    }

    /// This post's price, before anything is sent.
    #[must_use]
    pub fn cost(&self) -> Cost {
        Cost::for_text(&self.text)
    }

    /// Whether the post has no publishable content at all.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.text.trim().is_empty() && self.media.is_none()
    }
}
