//! What the caller has, versus what Instagram will take.
//!
//! A media source is either a local file or a public URL, and *both* the
//! capability question ("can this path carry that source at all?") and the
//! measurement question ("is this file inside the platform's limits?") are
//! asked before any request is made.

use std::path::{Path, PathBuf};

use crate::error::Refusal;

/// Where the bytes for one item live.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MediaSource {
    /// A file on this machine. Instagram accepts it for video (the resumable
    /// upload endpoint takes the bytes), never for a feed image.
    LocalFile(PathBuf),
    /// An `http(s)` URL Meta fetches itself. Required for images, and the only
    /// way to publish video without uploading the bytes.
    PublicUrl(String),
}

impl MediaSource {
    /// The URL, when this source is one.
    #[must_use]
    pub fn public_url(&self) -> Option<&str> {
        match self {
            Self::PublicUrl(url) => Some(url.as_str()),
            Self::LocalFile(_) => None,
        }
    }

    /// The local path, when this source is one.
    #[must_use]
    pub fn local_path(&self) -> Option<&Path> {
        match self {
            Self::LocalFile(path) => Some(path.as_path()),
            Self::PublicUrl(_) => None,
        }
    }

    /// A label that is safe to put in a message: the file name or the host of
    /// the URL.
    #[must_use]
    pub fn describe(&self) -> String {
        match self {
            Self::LocalFile(path) => path.file_name().map_or_else(
                || path.display().to_string(),
                |name| name.to_string_lossy().into_owned(),
            ),
            Self::PublicUrl(url) => host_of(url).unwrap_or_else(|| url.clone()),
        }
    }

    /// The bytes this source stands for.
    ///
    /// Only meaningful for a local file; a URL has no bytes here by design —
    /// fetching it is the platform's job, and pretending we had them would make
    /// the image path look like an upload path.
    pub fn read_bytes(&self) -> Result<Vec<u8>, Refusal> {
        match self {
            Self::LocalFile(path) => std::fs::read(path).map_err(|error| {
                Refusal::new(
                    "instagram.media.unreadable",
                    format!("Could not read {}: {error}", path.display()),
                )
            }),
            Self::PublicUrl(url) => Err(Refusal::new(
                "instagram.media.not_local",
                format!("{url} is a URL, so there are no local bytes to upload."),
            )),
        }
    }
}

fn host_of(url: &str) -> Option<String> {
    let without_scheme = url.split_once("://").map_or(url, |(_, rest)| rest);
    without_scheme
        .split('/')
        .next()
        .filter(|host| !host.is_empty())
        .map(ToString::to_string)
}

/// What was measured about a piece of media.
///
/// Every field is optional because this crate never runs `ffprobe`: the caller
/// measures, and a value it did not measure is `None` rather than a guess. A
/// limit whose measurement is absent is not enforced — refusing on an invented
/// number is worse than letting the platform answer.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct MediaMeasurements {
    pub bytes: Option<u64>,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub duration_seconds: Option<f64>,
}

impl MediaMeasurements {
    /// Measurements with only a byte count known.
    #[must_use]
    pub fn from_bytes(bytes: u64) -> Self {
        Self {
            bytes: Some(bytes),
            ..Self::default()
        }
    }

    /// Aspect ratio (width / height) when both were measured.
    #[must_use]
    pub fn aspect(&self) -> Option<f64> {
        match (self.width, self.height) {
            (Some(width), Some(height)) if width > 0 && height > 0 => {
                Some(f64::from(width) / f64::from(height))
            }
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn describe_never_leaks_a_full_path() {
        let source = MediaSource::LocalFile(PathBuf::from("/home/someone/videos/clip.mp4"));
        assert_eq!(source.describe(), "clip.mp4");
        let url = MediaSource::PublicUrl("https://cdn.example.com/a/b/c.jpg".to_string());
        assert_eq!(url.describe(), "cdn.example.com");
    }

    #[test]
    fn aspect_needs_both_dimensions() {
        let measurements = MediaMeasurements {
            width: Some(1080),
            height: None,
            ..MediaMeasurements::default()
        };
        assert_eq!(measurements.aspect(), None);
    }
}
