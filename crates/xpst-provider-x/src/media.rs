//! Media bytes and the 5 MiB segmentation X's chunked upload expects.
//!
//! X's v1.1 `media/upload.json` takes a file as `INIT` (total bytes) followed
//! by `APPEND` segments of at most 5 MiB, then `FINALIZE`. The segment size is
//! the vendor's, not a tuning knob — this module exists so the client never has
//! a second opinion about it, and so a 300 MB video is never held in memory at
//! once.
//!
//! No path or filename is hashed into anything public: the media identity used
//! for duplicate detection is supplied by the caller (see
//! [`crate::duplicate`]), not inferred from where the file happens to live.

use std::fmt;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use crate::error::XError;

/// The segment size X documents for chunked video upload (5 MiB).
pub const MEDIA_CHUNK_BYTES: usize = 5 * 1024 * 1024;

/// Where the bytes come from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MediaSource {
    /// In-memory bytes, used by probes and tests.
    Bytes(Vec<u8>),
    /// A file read one segment at a time; never loaded whole.
    File(PathBuf),
}

/// A media attachment: bytes plus the MIME type X is told.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Media {
    pub source: MediaSource,
    pub media_type: String,
}

impl Media {
    #[must_use]
    pub fn new(source: MediaSource, media_type: impl Into<String>) -> Self {
        Self {
            source,
            media_type: media_type.into(),
        }
    }

    /// Video held in memory; the MIME type X expects for MP4.
    #[must_use]
    pub fn video_bytes(bytes: impl Into<Vec<u8>>) -> Self {
        Self::new(MediaSource::Bytes(bytes.into()), "video/mp4")
    }

    /// A file on disk, with the MIME type inferred from its extension.
    ///
    /// The file is not opened here, so constructing a request cannot fail; a
    /// missing file becomes [`XError::MediaIo`] at upload time.
    #[must_use]
    pub fn from_path(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let media_type = media_type_for(&path).to_string();
        Self::new(MediaSource::File(path), media_type)
    }

    #[must_use]
    pub fn bytes_len(&self) -> Option<u64> {
        match &self.source {
            MediaSource::Bytes(bytes) => Some(bytes.len() as u64),
            MediaSource::File(_) => None,
        }
    }

    /// Total size of the media, read from disk for file sources.
    pub fn total_bytes(&self) -> Result<u64, XError> {
        match &self.source {
            MediaSource::Bytes(bytes) => Ok(bytes.len() as u64),
            MediaSource::File(path) => {
                std::fs::metadata(path)
                    .map(|meta| meta.len())
                    .map_err(|error| XError::MediaIo {
                        detail: format!("{}: {error}", path.display()),
                    })
            }
        }
    }

    /// Number of `APPEND` segments this media needs.
    pub fn segment_count(&self) -> Result<usize, XError> {
        let total = self.total_bytes()?;
        if total == 0 {
            return Ok(0);
        }
        let chunks = (total as usize).div_ceil(MEDIA_CHUNK_BYTES);
        Ok(chunks)
    }

    /// Read one segment, in order. `index` is the `segment_index` X receives.
    pub fn read_segment(&self, index: usize) -> Result<Vec<u8>, XError> {
        let offset = index
            .checked_mul(MEDIA_CHUNK_BYTES)
            .ok_or_else(|| XError::MediaIo {
                detail: format!("segment index {index} overflows the offset"),
            })?;
        match &self.source {
            MediaSource::Bytes(bytes) => {
                if offset >= bytes.len() {
                    return Ok(Vec::new());
                }
                let end = (offset + MEDIA_CHUNK_BYTES).min(bytes.len());
                Ok(bytes[offset..end].to_vec())
            }
            MediaSource::File(path) => {
                let mut file = File::open(path).map_err(|error| XError::MediaIo {
                    detail: format!("{}: {error}", path.display()),
                })?;
                file.seek(SeekFrom::Start(offset as u64))
                    .map_err(|error| XError::MediaIo {
                        detail: format!("{}: {error}", path.display()),
                    })?;
                let mut buffer = Vec::with_capacity(MEDIA_CHUNK_BYTES);
                file.take(MEDIA_CHUNK_BYTES as u64)
                    .read_to_end(&mut buffer)
                    .map_err(|error| XError::MediaIo {
                        detail: format!("{}: {error}", path.display()),
                    })?;
                Ok(buffer)
            }
        }
    }

    /// Filename reported in the multipart segment. Deliberately generic: the
    /// user's filename is not part of the protocol and need not be exposed.
    #[must_use]
    pub fn upload_name(&self) -> &'static str {
        if self.media_type.starts_with("video/") {
            "media.mp4"
        } else if self.media_type == "image/png" {
            "media.png"
        } else if self.media_type == "image/webp" {
            "media.webp"
        } else if self.media_type.starts_with("image/") {
            "media.jpg"
        } else {
            "media.bin"
        }
    }
}

impl fmt::Display for Media {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let size = self
            .bytes_len()
            .map_or_else(|| "file".to_string(), |len| format!("{len} bytes"));
        write!(f, "{} ({size})", self.media_type)
    }
}

fn media_type_for(path: &Path) -> &'static str {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "mp4" | "m4v" => "video/mp4",
        "mov" => "video/quicktime",
        "png" => "image/png",
        "webp" => "image/webp",
        "jpg" | "jpeg" => "image/jpeg",
        _ => "application/octet-stream",
    }
}
