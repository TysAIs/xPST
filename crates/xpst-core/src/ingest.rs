//! Per-destination ingest specs: the numbers a file is measured against.
//!
//! Ported from `xpst.media.specs.PLATFORM_SPECS`. They are deliberately a
//! *separate* registry from the publish envelope ([`crate::envelope`]): the
//! envelope is the platform's documented ceiling that a sender enforces, while
//! an ingest spec is the local rule set a preflight compares a probed file
//! against (containers, codec, long edge, still-image limits). Both are pinned
//! row by row against `crates/xpst-core/tests/fixtures/route_plan.json`, which
//! is captured by running the Python tables.
//!
//! Nothing here reads a file or touches the network: a caller supplies the
//! measurement, this module supplies the numbers to judge it with.

use crate::content::{
    content_profile, text_limit, DestinationContentProfile, MediaTransport,
    THREADS_PUBLIC_URL_REQUIREMENT,
};
use crate::envelope::{video_envelope, VideoEnvelope};

/// One destination's ingest rules, as declared today.
#[derive(Debug, Clone, Copy)]
pub struct IngestSpec {
    pub platform: &'static str,
    /// The name used inside ingest messages (`X (Twitter)`, `Instagram Reels`,
    /// `Threads`). It is not always the capability profile's display name, and
    /// a refusal must name the destination the user chose.
    pub display_name: &'static str,
    /// Video containers this destination ingests natively.
    pub video_containers: &'static [&'static str],
    pub video_codec: &'static str,
    /// Long edge above which the destination re-encodes instead of refusing
    /// (an advisory, never a refusal).
    pub video_long_edge: u32,
    /// The byte ceiling a waiting uploader refuses on. `None` means the
    /// destination's limit is not verified, so nothing is refused on size.
    pub video_file_size_cap_bytes: Option<u64>,
    /// Still-image containers, empty when the destination publishes no images.
    pub image_containers: &'static [&'static str],
    pub image_max_bytes: Option<u64>,
    /// Width/height bounds for a still image (`None` = any aspect accepted).
    pub image_aspect_min: Option<f64>,
    pub image_aspect_max: Option<f64>,
}

impl IngestSpec {
    /// Whether this destination publishes video at all.
    ///
    /// This asks what the *destination* takes, so it reads `declared`: an
    /// engine-side implementation gap is not a property of the destination's
    /// ingestion, and the planner refuses such a destination outright rather
    /// than describing its envelope.
    #[must_use]
    pub fn publishes_video(&self) -> bool {
        content_profile(self.platform)
            .is_some_and(|profile| profile.declares(crate::content::ContentType::Video))
    }

    /// Whether this destination publishes still images at all.
    #[must_use]
    pub fn publishes_image(&self) -> bool {
        content_profile(self.platform)
            .is_some_and(|profile| profile.declares(crate::content::ContentType::Image))
    }

    /// The still-image byte ceiling text used in a refusal.
    ///
    /// A cap declared in whole megabytes is quoted in megabytes; a cap derived
    /// from a platform's decimal byte limit (Bluesky's lexicon says 2,000,000
    /// bytes) is quoted in bytes, so the number in the refusal is the
    /// platform's own ceiling rather than a rounded-off approximation of it.
    #[must_use]
    pub fn image_cap_text(&self) -> Option<String> {
        const MIB: u64 = 1024 * 1024;
        let bytes = self.image_max_bytes?;
        if bytes % MIB == 0 {
            Some(format!("{} MB", bytes / MIB))
        } else {
            Some(format!("{} bytes", thousands(bytes)))
        }
    }
}

/// Group an integer with `,` every three digits, as Python's `{:,}` does.
fn thousands(value: u64) -> String {
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, character) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(character);
    }
    out
}

const MIB: u64 = 1024 * 1024;
const GIB: u64 = 1024 * MIB;

const MP4_MOV: &[&str] = &[".mp4", ".mov"];

/// THE ingest table, mirroring `xpst.media.specs.PLATFORM_SPECS`.
pub static INGEST_SPECS: [IngestSpec; 8] = [
    IngestSpec {
        platform: "youtube",
        display_name: "YouTube",
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 1920,
        video_file_size_cap_bytes: Some(256 * GIB),
        image_containers: &[],
        image_max_bytes: None,
        image_aspect_min: None,
        image_aspect_max: None,
    },
    IngestSpec {
        platform: "tiktok",
        display_name: "TikTok",
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 1920,
        video_file_size_cap_bytes: Some(GIB),
        image_containers: &[],
        image_max_bytes: None,
        image_aspect_min: None,
        image_aspect_max: None,
    },
    IngestSpec {
        platform: "instagram",
        display_name: "Instagram Reels",
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 1920,
        video_file_size_cap_bytes: Some(4 * GIB),
        image_containers: &[".jpg", ".jpeg"],
        image_max_bytes: Some(8 * MIB),
        // Meta publishes JPEG only, 8 MB maximum, aspect within 4:5–1.91:1.
        image_aspect_min: Some(0.8),
        image_aspect_max: Some(1.91),
    },
    IngestSpec {
        platform: "x",
        display_name: "X (Twitter)",
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 1920,
        video_file_size_cap_bytes: Some(512 * MIB),
        image_containers: &[".jpg", ".jpeg", ".png", ".webp"],
        image_max_bytes: Some(5 * MIB),
        // Single image post: ≤ 5 MB, JPG/PNG/WEBP, aspect ratio 1:3–3:1.
        image_aspect_min: Some(1.0 / 3.0),
        image_aspect_max: Some(3.0),
    },
    IngestSpec {
        platform: "bluesky",
        display_name: "Bluesky",
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 4000,
        video_file_size_cap_bytes: None,
        image_containers: &[".jpg", ".jpeg", ".png", ".webp", ".gif"],
        // The lexicon caps a blob at 2,000,000 decimal bytes, and it has no
        // aspect-ratio bound (the app re-encodes instead).
        image_max_bytes: Some(2_000_000),
        image_aspect_min: None,
        image_aspect_max: None,
    },
    IngestSpec {
        platform: "threads",
        display_name: "Threads",
        // Threads ingests through Instagram's video pipeline as a copy, never
        // as an alias, and publishes video only.
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 1920,
        video_file_size_cap_bytes: Some(4 * GIB),
        image_containers: &[],
        image_max_bytes: None,
        image_aspect_min: None,
        image_aspect_max: None,
    },
    IngestSpec {
        platform: "facebook",
        display_name: "Facebook Page",
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 1920,
        video_file_size_cap_bytes: Some(10 * GIB),
        image_containers: &[],
        image_max_bytes: None,
        image_aspect_min: None,
        image_aspect_max: None,
    },
    IngestSpec {
        platform: "rumble",
        display_name: "Rumble",
        video_containers: MP4_MOV,
        video_codec: "h264",
        video_long_edge: 1920,
        // Per-plan limits behind a partner-gated API: nothing is verified, so
        // nothing is refused on size.
        video_file_size_cap_bytes: None,
        image_containers: &[],
        image_max_bytes: None,
        image_aspect_min: None,
        image_aspect_max: None,
    },
];

/// Return the ingest spec for a destination, or `None` if it declares none.
#[must_use]
pub fn ingest_spec(platform: &str) -> Option<&'static IngestSpec> {
    let key = platform.trim().to_ascii_lowercase();
    INGEST_SPECS.iter().find(|spec| spec.platform == key)
}

/// Display names of every destination that can publish `modality`, deduped.
///
/// Used only for the sentence that tells a user where such a file *can* go —
/// the same global statement Python's
/// `modality_unsupported_message` makes, so a refusal for a single candidate
/// destination never claims "xPST has no image publish path" while other
/// destinations plainly have one.
#[must_use]
pub fn destinations_accepting(modality: &str) -> Vec<&'static str> {
    let mut names: Vec<&'static str> = Vec::new();
    for spec in &INGEST_SPECS {
        let accepts = match modality {
            "image" => spec.publishes_image(),
            "video" => spec.publishes_video(),
            _ => false,
        };
        if accepts && !names.contains(&spec.display_name) {
            names.push(spec.display_name);
        }
    }
    names
}

/// Everything the route planner needs to know about one candidate destination.
///
/// `profile` is `None` for a destination xPST has no declared capability for
/// (a third-party destination): that is reported as unknown-platform, never as
/// capable and never as unconfigured.
#[derive(Debug, Clone)]
pub struct DestinationCapability {
    pub platform: String,
    pub profile: Option<DestinationProfile>,
    /// The per-account opt-in flag. A disabled destination is refused *as
    /// disabled*; it is never reported as "unconfigured" or as unconnected.
    pub enabled: bool,
    /// Whether a usable credential was found. An enabled destination whose
    /// credential looks missing is still attempted in Python — it travels as an
    /// advisory, never as a refusal.
    pub authenticated: bool,
    /// The readiness detail to quote when `authenticated` is false. Empty means
    /// the caller supplied none, and no advisory is claimed.
    pub auth_detail: String,
}

/// One destination's declared publishing capability.
#[derive(Debug, Clone)]
pub struct DestinationProfile {
    pub display_name: String,
    /// `false` for a messaging-only destination (Messenger), which is never a
    /// publishing target.
    pub publishing: bool,
    pub declared: Vec<crate::content::ContentType>,
    pub implemented: Vec<crate::content::ContentType>,
    pub envelope: Option<VideoEnvelope>,
    pub media_transport: MediaTransport,
    /// The platform's own requirement text for a destination that fetches
    /// media itself; `None` when it uploads the bytes.
    pub media_transport_requirement: Option<String>,
    /// The stable code such a refusal reports (`THREADS_NEEDS_URL`).
    pub media_transport_error_code: Option<String>,
    pub ingest: Option<&'static IngestSpec>,
    pub text_limit: Option<usize>,
}

impl DestinationProfile {
    /// Whether the engine has a real implementation for this content type.
    #[must_use]
    pub fn supports(&self, content_type: crate::content::ContentType) -> bool {
        self.implemented.contains(&content_type)
    }

    /// Whether the destination declares the type in its manifest.
    #[must_use]
    pub fn declares(&self, content_type: crate::content::ContentType) -> bool {
        self.declared.contains(&content_type)
    }
}

impl DestinationCapability {
    /// Build the capability row for a built-in destination.
    ///
    /// Returns `None` for a platform xPST has no capability profile for; use
    /// [`DestinationCapability::undeclared`] for that case so the planner can
    /// report it as unknown rather than silently dropping it.
    #[must_use]
    pub fn built_in(platform: &str, enabled: bool, authenticated: bool) -> Option<Self> {
        let profile = content_profile(platform)?;
        Some(Self {
            platform: profile.platform.to_owned(),
            profile: Some(profile_data(profile)),
            enabled,
            authenticated,
            auth_detail: String::new(),
        })
    }

    /// A candidate destination xPST has no declared capability for.
    #[must_use]
    pub fn undeclared(platform: &str, enabled: bool, authenticated: bool) -> Self {
        Self {
            platform: platform.trim().to_ascii_lowercase(),
            profile: None,
            enabled,
            authenticated,
            auth_detail: String::new(),
        }
    }

    /// A built-in destination whose credential is missing, quoting the given
    /// readiness detail in the advisory.
    #[must_use]
    pub fn with_auth_detail(mut self, detail: impl Into<String>) -> Self {
        self.auth_detail = detail.into();
        self
    }
}

fn profile_data(profile: &DestinationContentProfile) -> DestinationProfile {
    let platform = profile.platform;
    let envelope = video_envelope(platform).copied();
    let requirement = if profile.media_transport == MediaTransport::PublicUrl {
        Some(match platform {
            "threads" => THREADS_PUBLIC_URL_REQUIREMENT.to_owned(),
            _ => String::new(),
        })
        .filter(|text| !text.is_empty())
    } else {
        None
    };
    let transport_code = if profile.media_transport == MediaTransport::PublicUrl {
        Some(match platform {
            "threads" => "THREADS_NEEDS_URL".to_owned(),
            _ => crate::content::MEDIA_TRANSPORT_ERROR_CODE.to_owned(),
        })
    } else {
        None
    };
    DestinationProfile {
        display_name: profile.display_name.to_owned(),
        publishing: profile.publishing,
        declared: profile.declared.to_vec(),
        implemented: profile.implemented.to_vec(),
        envelope,
        media_transport: profile.media_transport,
        media_transport_requirement: requirement,
        media_transport_error_code: transport_code,
        ingest: ingest_spec(platform),
        text_limit: text_limit(platform),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thousands_groups_like_python() {
        assert_eq!(thousands(2_000_000), "2,000,000");
        assert_eq!(thousands(999), "999");
        assert_eq!(thousands(1000), "1,000");
    }

    #[test]
    fn image_cap_text_matches_declared_units() {
        assert_eq!(
            ingest_spec("bluesky").unwrap().image_cap_text().as_deref(),
            Some("2,000,000 bytes")
        );
        assert_eq!(
            ingest_spec("instagram")
                .unwrap()
                .image_cap_text()
                .as_deref(),
            Some("8 MB")
        );
    }
}
