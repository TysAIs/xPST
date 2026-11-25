//! Per-destination ingest checks: what a probed file is measured against.
//!
//! Ported from `xpst/media/specs.py` (`verify_media`) and
//! `xpst/media/image_header.py`. The numbers themselves live in
//! `xpst_core::ingest`; this module runs the probe and turns the result into the
//! ordered check list the preflight reports.
//!
//! `verify_media` is MODALITY-AWARE: a file's modality (video/image) is decided
//! first, and only the checks that apply to that modality run. A JPEG is not
//! measured against the video container and video-stream rules; when its
//! destination cannot publish images it produces ONE error that names the
//! destination instead of a pair of raw spec violations.
//!
//! Never panics: a probe failure degrades to a warning so a pre-flight hiccup can
//! never block a legitimate upload. Hard errors (unsupported modality, wrong
//! container, no video stream, oversized file) mean the platform would reject or
//! irrecoverably mangle the file.
//!
//! `check_loudness` runs the ffmpeg `loudnorm` analysis pass
//! ([`crate::loudness`]) and adds the `loudness` check when the measurement
//! succeeds, exactly as `xpst/media/specs.py` does. The route planner always
//! passes `false` (`build_route_plan` sets `check_loudness=False`), so
//! `preflight` never pays for the extra subprocess; `verify-media` defaults to
//! `true`, which is the command that needs it.
//!
//! The probe document itself is carried on the verdict as the raw `ffprobe`
//! stdout text. `MediaReport.to_dict(include_probe=True)` publishes it verbatim,
//! so its key order is ffprobe's — a `serde_json::Value` round trip would sort
//! it (`Map` is a `BTreeMap` without `preserve_order`) and change the bytes. The
//! CLI parses the text into `xpst_config::Value`, whose `Mapping` keeps document
//! order.
use std::path::Path;
use std::process::Command;

use serde_json::Value;
use xpst_core::content::{ContentType, MediaKind};
use xpst_core::ingest::{ingest_spec, IngestSpec};
use xpst_core::plan::modality_unsupported_message;
use xpst_core::preflight::{Check, CheckStatus, MediaVerdict};

/// Name of the check that reports "this destination cannot take this modality".
pub const MODALITY_CHECK: &str = "modality";

/// The modality a path declares, from its extension alone.
///
/// `None` for anything that is not a known media file, so a caller can decide on
/// its own policy instead of receiving a wrong modality.
#[must_use]
pub fn detect_modality(value: &str) -> Option<MediaKind> {
    let suffix = normalize_suffix(value);
    if xpst_core::content::VIDEO_EXTENSIONS.contains(&suffix.as_str()) {
        return Some(MediaKind::Video);
    }
    if xpst_core::content::IMAGE_EXTENSIONS.contains(&suffix.as_str()) {
        return Some(MediaKind::Image);
    }
    None
}

/// A lower-cased dotted suffix for a path or a bare suffix.
///
/// A bare suffix (`.jpg`) is a hidden filename to `Path`, so it is handled first,
/// exactly as `xpst.media.modality.normalize_suffix` does.
#[must_use]
pub fn normalize_suffix(value: &str) -> String {
    let raw = value.trim();
    if raw.starts_with('.') && raw.matches('.').count() == 1 {
        return raw.to_ascii_lowercase();
    }
    Path::new(raw)
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| format!(".{}", extension.to_ascii_lowercase()))
        .unwrap_or_default()
}

/// Human name for a platform key (Threads ships Instagram's ingest spec).
#[must_use]
pub fn destination_display_name(platform: &str) -> String {
    if platform == "threads" {
        return "Threads".to_owned();
    }
    ingest_spec(platform).map_or_else(|| platform.to_owned(), |spec| spec.display_name.to_owned())
}

/// Whether a destination can publish `modality` today.
///
/// Reads the **Python** implemented set: the preflight must agree with the
/// shipped engine about what a destination accepts.
fn supports_modality(spec: &IngestSpec, modality: MediaKind) -> bool {
    let wanted = match modality {
        MediaKind::Video => ContentType::Video,
        MediaKind::Image => ContentType::Image,
        MediaKind::Unknown => return false,
    };
    xpst_core::content::content_profile(spec.platform)
        .is_some_and(|profile| profile.python_implemented.contains(&wanted))
}

/// Check a media file against `platform`'s ingest spec before upload.
#[must_use]
pub fn verify_media(path: &Path, platform: &str, check_loudness: bool) -> MediaVerdict {
    let spec = ingest_spec(platform).unwrap_or_else(|| {
        ingest_spec("instagram").expect("the ingest table always carries instagram")
    });
    let mut checks: Vec<Check> = Vec::new();
    let detected = detect_modality(&path.to_string_lossy());
    let suffix = normalize_suffix(&path.to_string_lossy());

    // Unsupported modality — one clear error naming the destination.
    if let Some(modality) = detected {
        if !supports_modality(spec, modality) {
            checks.push(Check {
                name: MODALITY_CHECK.to_owned(),
                status: CheckStatus::Error,
                detail: modality_unsupported_message(
                    modality_label(modality),
                    &suffix,
                    &destination_display_name(platform),
                ),
            });
            return MediaVerdict {
                checks,
                duration_seconds: None,
                aspect_ratio: None,
                probe: None,
            };
        }
        if modality == MediaKind::Image {
            return verify_image(path, platform, spec, checks);
        }
    }

    // Container — platform rejection risk.
    add(
        &mut checks,
        "container",
        spec.video_containers.contains(&suffix.as_str()),
        &format!(
            "{} vs accepted {}",
            if suffix.is_empty() { "(none)" } else { &suffix },
            spec.video_containers.join(", ")
        ),
        CheckStatus::Error,
    );

    let info = match probe(path) {
        Ok(info) => info,
        Err(error) => {
            checks.push(Check {
                name: "probe".to_owned(),
                status: CheckStatus::Warn,
                detail: format!("ffprobe failed ({error}); spec not verified"),
            });
            return MediaVerdict {
                checks,
                duration_seconds: None,
                aspect_ratio: None,
                probe: None,
            };
        }
    };

    let video = pick_video_stream(&info.streams);
    let audio = info
        .streams
        .iter()
        .find(|stream| stream.codec_type.as_deref() == Some("audio"));

    // Real video stream — a still/cover-art upload.
    add(
        &mut checks,
        "video_stream",
        video.is_some(),
        if video.is_some() {
            "real video track present"
        } else {
            "NO real video stream (cover art / audio only)"
        },
        CheckStatus::Error,
    );
    let Some(video) = video else {
        return MediaVerdict {
            checks,
            duration_seconds: None,
            aspect_ratio: None,
            probe: Some(info.document.clone()),
        };
    };

    // Stream properties — platform transcode risk.
    add(
        &mut checks,
        "video_codec",
        video.codec_name.as_deref() == Some(spec.video_codec),
        &format!(
            "{} vs {}",
            display(video.codec_name.as_deref()),
            spec.video_codec
        ),
        CheckStatus::Warn,
    );
    add(
        &mut checks,
        "pix_fmt",
        video.pix_fmt.as_deref() == Some(spec.pix_fmt),
        &format!(
            "{} vs {} ({} rejects others)",
            display(video.pix_fmt.as_deref()),
            spec.pix_fmt,
            spec.display_name
        ),
        CheckStatus::Warn,
    );
    let width = as_int(video.width.as_ref());
    let height = as_int(video.height.as_ref());
    let long_edge = width.max(height);
    // Soft ceiling: the destination re-encodes anything above its long edge
    // rather than refusing it, so the verdict is a warning — and it says what
    // will actually happen to the file instead of leaving "ready" looking
    // lossless.
    add(
        &mut checks,
        "dimensions",
        long_edge > 0 && long_edge <= i64::from(spec.video_long_edge),
        &format!(
            "{width}x{height} (long edge {long_edge}) vs max {} — advisory: {} re-encodes above its \
             ceiling instead of rejecting the file",
            spec.video_long_edge, spec.display_name
        ),
        CheckStatus::Warn,
    );
    let fps = parse_frame_rate(
        video
            .avg_frame_rate
            .as_deref()
            .or(video.r_frame_rate.as_deref())
            .unwrap_or(""),
    );
    add(
        &mut checks,
        "fps",
        fps.is_none_or(|fps| fps <= f64::from(spec.fps_cap) + 0.1),
        &match fps {
            Some(fps) => format!("{fps:.1} vs cap {}", spec.fps_cap),
            None => "unknown".to_owned(),
        },
        CheckStatus::Warn,
    );

    let bit_rate = {
        let from_stream = as_int(video.bit_rate.as_ref());
        if from_stream != 0 {
            from_stream
        } else {
            as_int(info.format.bit_rate.as_ref())
        }
    };
    add(
        &mut checks,
        "video_bitrate",
        bit_rate == 0 || bit_rate as f64 <= spec.max_video_bitrate_bps as f64 * 1.25,
        &if bit_rate != 0 {
            format!(
                "{:.2} Mbps vs ceiling {:.0} Mbps (+25%) — advisory: {} re-encodes above its \
                 ceiling instead of rejecting the file",
                bit_rate as f64 / 1_000_000.0,
                spec.max_video_bitrate_bps as f64 / 1_000_000.0,
                spec.display_name
            )
        } else {
            "unknown".to_owned()
        },
        CheckStatus::Warn,
    );

    if let Some(audio) = audio {
        add(
            &mut checks,
            "audio_codec",
            audio.codec_name.as_deref() == Some(spec.audio_codec),
            &format!(
                "{} vs {}",
                display(audio.codec_name.as_deref()),
                spec.audio_codec
            ),
            CheckStatus::Warn,
        );
        let sample_rate = as_int(audio.sample_rate.as_ref());
        // Advisory, never a defect: every destination resamples audio to its own
        // rate, and YouTube (48 kHz) / X / TikTok (44.1 kHz) disagree, so one file
        // cannot satisfy all of them at once.
        add(
            &mut checks,
            "audio_rate",
            sample_rate == 0 || sample_rate == i64::from(spec.audio_rate),
            &format!(
                "{sample_rate} Hz vs {} Hz target — advisory only: {} resamples audio, so the file \
                 is accepted",
                spec.audio_rate, spec.display_name
            ),
            CheckStatus::Warn,
        );
    }

    // Duration — premium tiers allow more; the standard tier is a warning.
    let duration = as_float(info.format.duration.as_ref());
    add(
        &mut checks,
        "duration",
        duration.is_none()
            || spec.duration_cap_s.is_none()
            || duration.is_some_and(|value| value <= f64::from(spec.duration_cap_s.unwrap_or(0))),
        &match (duration, spec.duration_cap_s) {
            (Some(value), Some(cap)) => format!("{value:.0}s vs cap {cap}s"),
            (Some(_), None) => "ok".to_owned(),
            (None, _) => "unknown".to_owned(),
        },
        CheckStatus::Warn,
    );

    check_file_size(path, spec, &mut checks);

    // faststart — playback/ingest optimization, a warning only.
    if suffix == ".mp4" && has_faststart(path) == Some(false) {
        checks.push(Check {
            name: "faststart".to_owned(),
            status: CheckStatus::Warn,
            detail: "moov atom is not at the front (progressive playback / YouTube processing)"
                .to_owned(),
        });
    }

    // Loudness — off-target means the platform gain-stage moves it.
    if check_loudness && audio.is_some() {
        if let Some(measured) = crate::loudness::measure(None, path, -14.0) {
            let lufs = measured.input_i;
            let deviation = lufs - spec.lufs;
            add(
                &mut checks,
                "loudness",
                deviation.abs() <= 2.0,
                &format!(
                    "{} LUFS vs target {:.1} (\u{0394} {}{} LU)",
                    crate::loudness::format_one_decimal(lufs),
                    spec.lufs,
                    if deviation.is_sign_negative() {
                        "-"
                    } else {
                        "+"
                    },
                    crate::loudness::format_one_decimal(deviation.abs()),
                ),
                CheckStatus::Warn,
            );
        }
    }

    let (duration_seconds, aspect_ratio) = probe_dimensions(&info);
    MediaVerdict {
        checks,
        duration_seconds,
        aspect_ratio,
        probe: Some(info.document),
    }
}

fn modality_label(modality: MediaKind) -> &'static str {
    match modality {
        MediaKind::Video => "video",
        MediaKind::Image => "image",
        MediaKind::Unknown => "unknown",
    }
}

fn display(value: Option<&str>) -> &str {
    value.unwrap_or("None")
}

fn add(checks: &mut Vec<Check>, name: &str, ok: bool, detail: &str, error_level: CheckStatus) {
    checks.push(Check {
        name: name.to_owned(),
        status: if ok { CheckStatus::Ok } else { error_level },
        detail: detail.to_owned(),
    });
}

/// File size — hard rejection above the cap.
fn check_file_size(path: &Path, spec: &IngestSpec, checks: &mut Vec<Check>) {
    let Ok(metadata) = std::fs::metadata(path) else {
        return;
    };
    let size_mb = metadata.len() as f64 / (1024.0 * 1024.0);
    let detail = match spec.file_size_cap_mb {
        Some(cap) => format!("{size_mb:.0} MB vs cap {cap} MB"),
        None => format!("{size_mb:.0} MB"),
    };
    add(
        checks,
        "file_size",
        spec.file_size_cap_mb
            .is_none_or(|cap| size_mb <= cap as f64),
        &detail,
        CheckStatus::Error,
    );
}

/// Still-image size — a destination rejects above its own image cap.
fn check_image_file_size(path: &Path, platform: &str, spec: &IngestSpec, checks: &mut Vec<Check>) {
    let Some(cap_bytes) = spec.image_max_bytes else {
        return;
    };
    let Ok(metadata) = std::fs::metadata(path) else {
        return;
    };
    let size_mb = metadata.len() as f64 / (1024.0 * 1024.0);
    let cap_mb = cap_bytes as f64 / (1024.0 * 1024.0);
    // A cap declared in whole megabytes is quoted in megabytes. A cap derived
    // from a platform's decimal byte limit (Bluesky's lexicon says 2,000,000
    // bytes) is quoted in bytes instead, so the number in the refusal is the
    // platform's own ceiling rather than a rounded-off approximation of it.
    let cap_text = if cap_mb.fract() == 0.0 {
        format!("{cap_mb:.0} MB")
    } else {
        format!("{} bytes", thousands(cap_bytes))
    };
    let detail = if size_mb > cap_mb {
        format!(
            "{size_mb:.1} MB vs {} image cap {cap_text}",
            destination_display_name(platform)
        )
    } else {
        format!("{size_mb:.1} MB vs cap {cap_text}")
    };
    add(
        checks,
        "file_size",
        size_mb <= cap_mb,
        &detail,
        CheckStatus::Error,
    );
}

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

/// Destination-named complaint about an image's aspect ratio, or `None`.
fn image_aspect_error(
    platform: &str,
    spec: &IngestSpec,
    width: i64,
    height: i64,
) -> Option<String> {
    let (min, max) = (spec.image_aspect_min?, spec.image_aspect_max?);
    if width == 0 || height == 0 {
        return None;
    }
    let ratio = width as f64 / height as f64;
    if ratio >= min - 1e-6 && ratio <= max + 1e-6 {
        return None;
    }
    Some(format!(
        "{} needs an image between {min:.2}:1 and {max:.2}:1 (width:height); this one is \
         {width}x{height} ({ratio:.2}:1). Crop or resize it and try again.",
        destination_display_name(platform)
    ))
}

/// Checks that apply to a still image at a destination that publishes them.
///
/// No video-stream, codec, fps, faststart or loudness rule is meaningful for a
/// still image, so none of them run here — that is the whole point of the
/// modality split.
fn verify_image(
    path: &Path,
    platform: &str,
    spec: &IngestSpec,
    mut checks: Vec<Check>,
) -> MediaVerdict {
    let suffix = normalize_suffix(&path.to_string_lossy());
    let accepted = spec.image_containers;
    let destination = destination_display_name(platform);
    add(
        &mut checks,
        "container",
        accepted.contains(&suffix.as_str()),
        &if accepted.contains(&suffix.as_str()) {
            format!("{suffix} is an accepted {destination} image format")
        } else {
            format!(
                "{destination} does not publish {} images: it accepts {}. Convert the file and \
                 try again.",
                if suffix.is_empty() {
                    "that file type"
                } else {
                    &suffix
                },
                if accepted.is_empty() {
                    "(none)".to_owned()
                } else {
                    accepted.join(", ")
                }
            )
        },
        CheckStatus::Error,
    );

    let mut dimensions = read_image_dimensions(path);
    let mut read_by = "header";
    let mut probe_doc: Option<Probe> = None;
    if dimensions.is_none() {
        // Unknown or unusual container: ask ffprobe, exactly like the video path.
        match probe(path) {
            Ok(info) => {
                let stream = info
                    .streams
                    .iter()
                    .find(|stream| stream.codec_type.as_deref() == Some("video"));
                if let Some(stream) = stream {
                    let width = as_int(stream.width.as_ref());
                    let height = as_int(stream.height.as_ref());
                    if width > 0 && height > 0 {
                        dimensions = Some((width, height));
                        read_by = "ffprobe";
                    }
                }
                probe_doc = Some(info);
            }
            Err(error) => {
                checks.push(Check {
                    name: "probe".to_owned(),
                    status: CheckStatus::Warn,
                    detail: format!(
                        "could not size this image (header and ffprobe both failed: {error})"
                    ),
                });
            }
        }
    }

    let Some((width, height)) = dimensions else {
        if !checks.iter().any(|check| check.name == "probe") {
            checks.push(Check {
                name: "probe".to_owned(),
                status: CheckStatus::Warn,
                detail: "image dimensions unreadable; the dimension and aspect rules were not \
                         verified"
                    .to_owned(),
            });
        }
        check_image_file_size(path, platform, spec, &mut checks);
        return MediaVerdict {
            checks,
            duration_seconds: None,
            aspect_ratio: None,
            probe: probe_doc.map(|info| info.document),
        };
    };

    let long_edge = width.max(height);
    add(
        &mut checks,
        "dimensions",
        long_edge > 0 && long_edge <= i64::from(spec.video_long_edge),
        &format!(
            "{width}x{height} (long edge {long_edge}) vs max {}",
            spec.video_long_edge
        ),
        CheckStatus::Warn,
    );
    if let Some(error) = image_aspect_error(platform, spec, width, height) {
        checks.push(Check {
            name: "aspect_ratio".to_owned(),
            status: CheckStatus::Error,
            detail: error,
        });
    }
    check_image_file_size(path, platform, spec, &mut checks);

    let _ = read_by;
    MediaVerdict {
        checks,
        duration_seconds: None,
        aspect_ratio: if height > 0 {
            Some(width as f64 / height as f64)
        } else {
            None
        },
        probe: probe_doc.map(|info| info.document),
    }
}

// ── The probe ───────────────────────────────────────────────────────────────

/// One ffprobe stream, as much of it as the checks read.
#[derive(Debug, Clone, Default)]
pub struct Stream {
    pub codec_type: Option<String>,
    pub codec_name: Option<String>,
    pub pix_fmt: Option<String>,
    pub width: Option<Value>,
    pub height: Option<Value>,
    pub avg_frame_rate: Option<String>,
    pub r_frame_rate: Option<String>,
    pub bit_rate: Option<Value>,
    pub sample_rate: Option<Value>,
    pub attached_pic: bool,
}

/// One ffprobe format block.
#[derive(Debug, Clone, Default)]
pub struct Format {
    pub duration: Option<Value>,
    pub bit_rate: Option<Value>,
}

/// A parsed `ffprobe -print_format json -show_format -show_streams` document.
#[derive(Debug, Clone, Default)]
pub struct Probe {
    pub streams: Vec<Stream>,
    pub format: Format,
    /// The raw stdout text, verbatim.
    ///
    /// `MediaReport.to_dict(include_probe=True)` publishes the probe document
    /// itself, so its key order is ffprobe's. Keeping the text (rather than a
    /// `serde_json::Value`) is what makes that byte-identical: `serde_json`'s
    /// `Map` is a `BTreeMap` unless `preserve_order` is on, which would sort
    /// every key.
    pub document: String,
}

/// Run ffprobe on a file.
///
/// The command line is `get_video_info_standalone`'s, verbatim: a different
/// argument set would produce a different document and the checks would be
/// measuring something else.
pub fn probe(path: &Path) -> Result<Probe, String> {
    let binary = crate::resolve_ffprobe().unwrap_or_else(|| "ffprobe".to_owned());
    let output = Command::new(&binary)
        .arg("-v")
        .arg("quiet")
        .arg("-print_format")
        .arg("json")
        .arg("-show_format")
        .arg("-show_streams")
        .arg(path)
        .output()
        .map_err(|error| format!("ffprobe failed: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "ffprobe failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let document: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("ffprobe failed: {error}"))?;
    let mut probe = parse_probe(&document);
    probe.document = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    Ok(probe)
}

fn parse_probe(document: &Value) -> Probe {
    let streams = document
        .get("streams")
        .and_then(Value::as_array)
        .map(|items| items.iter().map(parse_stream).collect())
        .unwrap_or_default();
    let format = document.get("format").cloned().unwrap_or(Value::Null);
    Probe {
        streams,
        format: Format {
            duration: format.get("duration").cloned(),
            bit_rate: format.get("bit_rate").cloned(),
        },
        document: String::new(),
    }
}

fn parse_stream(value: &Value) -> Stream {
    Stream {
        codec_type: value
            .get("codec_type")
            .and_then(Value::as_str)
            .map(str::to_owned),
        codec_name: value
            .get("codec_name")
            .and_then(Value::as_str)
            .map(str::to_owned),
        pix_fmt: value
            .get("pix_fmt")
            .and_then(Value::as_str)
            .map(str::to_owned),
        width: value.get("width").cloned(),
        height: value.get("height").cloned(),
        avg_frame_rate: value
            .get("avg_frame_rate")
            .and_then(Value::as_str)
            .map(str::to_owned),
        r_frame_rate: value
            .get("r_frame_rate")
            .and_then(Value::as_str)
            .map(str::to_owned),
        bit_rate: value.get("bit_rate").cloned(),
        sample_rate: value.get("sample_rate").cloned(),
        attached_pic: value
            .get("disposition")
            .and_then(|disposition| disposition.get("attached_pic"))
            .and_then(Value::as_i64)
            .is_some_and(|flag| flag != 0),
    }
}

/// The REAL video stream, ignoring cover-art / thumbnail streams.
///
/// Audio files routinely carry an mjpeg/png `attached_pic` stream (album art,
/// podcast cover). Treating that as "the video" makes the encoder fabricate a
/// 1-frame video and silently upload a still image.
#[must_use]
pub fn pick_video_stream(streams: &[Stream]) -> Option<&Stream> {
    streams.iter().find(|stream| {
        if stream.codec_type.as_deref() != Some("video") {
            return false;
        }
        if stream.attached_pic {
            return false;
        }
        // Untagged still-image codecs in a media container are cover art in
        // practice; real video tracks use h264/hevc/av1/mpeg4/vp9…
        if matches!(
            stream.codec_name.as_deref(),
            Some("mjpeg" | "png" | "bmp" | "gif")
        ) && !stream.attached_pic
        {
            return false;
        }
        true
    })
}

/// `int(value or 0)` for a JSON scalar: a number, or a numeric string.
fn as_int(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64))
            .unwrap_or(0),
        Some(Value::String(text)) => text.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

/// `float(value)` for a JSON scalar, or `None` when it is absent/unparseable.
fn as_float(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::String(text)) => text.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// Parse an ffprobe frame-rate fraction (`30000/1001`, `60/1`) to a float.
#[must_use]
pub fn parse_frame_rate(raw: &str) -> Option<f64> {
    if let Some((numerator, denominator)) = raw.split_once('/') {
        let denominator = denominator.trim().parse::<f64>().ok()?;
        if denominator == 0.0 {
            return None;
        }
        return numerator
            .trim()
            .parse::<f64>()
            .ok()
            .map(|value| value / denominator);
    }
    raw.trim().parse::<f64>().ok()
}

/// `(duration_seconds, aspect_ratio)` from a probe document.
fn probe_dimensions(info: &Probe) -> (Option<f64>, Option<f64>) {
    let duration = as_float(info.format.duration.as_ref());
    let Some(video) = info
        .streams
        .iter()
        .find(|stream| stream.codec_type.as_deref() == Some("video"))
    else {
        return (duration, None);
    };
    let width = as_int(video.width.as_ref()) as f64;
    let height = as_int(video.height.as_ref()) as f64;
    let aspect = if width > 0.0 && height > 0.0 {
        Some(width / height)
    } else {
        None
    };
    (duration, aspect)
}

/// True when the MP4 `moov` atom precedes `mdat` (progressive playback).
///
/// A pure top-level atom scan (cheap, no ffmpeg). `None` when the structure
/// cannot be determined (not MP4, fragmented files, read errors).
#[must_use]
pub fn has_faststart(path: &Path) -> Option<bool> {
    use std::io::{Read, Seek, SeekFrom};

    let mut file = std::fs::File::open(path).ok()?;
    let size = file.metadata().ok()?.len();
    let mut seen: Vec<[u8; 4]> = Vec::new();
    let mut position: u64 = 0;
    while position + 8 <= size && seen.len() < 64 {
        file.seek(SeekFrom::Start(position)).ok()?;
        let mut header = [0_u8; 8];
        if file.read(&mut header).ok()? < 8 {
            return None;
        }
        let box_size = u32::from_be_bytes([header[0], header[1], header[2], header[3]]);
        let box_type = [header[4], header[5], header[6], header[7]];
        if box_size < 8 {
            // 64-bit or zero-sized (fragmented) — bail out.
            return None;
        }
        seen.push(box_type);
        if box_type == *b"moov" && seen.contains(b"mdat") {
            return Some(false);
        }
        if box_type == *b"mdat" && seen.contains(b"moov") {
            return Some(true);
        }
        position += u64::from(box_size);
    }
    None
}

// ── Still-image headers ─────────────────────────────────────────────────────

/// Pixel size `(width, height)` of a still image, or `None` if unreadable.
///
/// Supports JPEG, PNG, GIF and WebP from the file header alone — a few dozen
/// bytes of I/O, no subprocess. Never panics: an unknown, truncated or malformed
/// file returns `None`.
#[must_use]
pub fn read_image_dimensions(path: &Path) -> Option<(i64, i64)> {
    use std::io::Read;

    let mut file = std::fs::File::open(path).ok()?;
    let mut header = [0_u8; 64];
    let read = file.read(&mut header).ok()?;
    let header = &header[..read];
    let suffix = normalize_suffix(&path.to_string_lossy());
    match suffix.as_str() {
        ".jpg" | ".jpeg" => jpeg_dimensions(path),
        ".png" => png_dimensions(header),
        ".gif" => gif_dimensions(header),
        ".webp" => webp_dimensions(header),
        _ => None,
    }
}

fn jpeg_dimensions(path: &Path) -> Option<(i64, i64)> {
    use std::io::{Read, Seek, SeekFrom};

    let mut file = std::fs::File::open(path).ok()?;
    let mut start = [0_u8; 2];
    file.read_exact(&mut start).ok()?;
    if start != [0xFF, 0xD8] {
        return None;
    }
    loop {
        let mut byte = [0_u8; 1];
        if file.read(&mut byte).ok()? == 0 {
            return None;
        }
        if byte[0] != 0xFF {
            continue; // fill byte between segments
        }
        let mut marker = [0_u8; 1];
        file.read_exact(&mut marker).ok()?;
        while marker[0] == 0xFF {
            // legal padding before a marker
            file.read_exact(&mut marker).ok()?;
        }
        let code = marker[0];
        if code == 0x01 || (0xD0..=0xD9).contains(&code) {
            continue; // standalone markers, no payload
        }
        let mut length_bytes = [0_u8; 2];
        file.read_exact(&mut length_bytes).ok()?;
        let segment_length = u16::from_be_bytes(length_bytes) as i64;
        if segment_length < 2 {
            return None;
        }
        let is_sof = (0xC0..0xD0).contains(&code) && !matches!(code, 0xC4 | 0xC8 | 0xCC);
        if is_sof {
            let mut body = [0_u8; 5];
            file.read_exact(&mut body).ok()?;
            let height = i64::from(u16::from_be_bytes([body[1], body[2]]));
            let width = i64::from(u16::from_be_bytes([body[3], body[4]]));
            if width <= 0 || height <= 0 {
                return None;
            }
            return Some((width, height));
        }
        if code == 0xDA {
            // start of scan — no frame header left to find
            return None;
        }
        file.seek(SeekFrom::Current(segment_length - 2)).ok()?;
    }
}

fn png_dimensions(header: &[u8]) -> Option<(i64, i64)> {
    if header.len() < 24 || header[..8] != [0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A] {
        return None;
    }
    if header[12..16] != *b"IHDR" {
        return None;
    }
    let width = u32::from_be_bytes([header[16], header[17], header[18], header[19]]);
    let height = u32::from_be_bytes([header[20], header[21], header[22], header[23]]);
    if width == 0 || height == 0 {
        return None;
    }
    Some((i64::from(width), i64::from(height)))
}

fn gif_dimensions(header: &[u8]) -> Option<(i64, i64)> {
    if header.len() < 10 {
        return None;
    }
    if header[..6] != *b"GIF87a" && header[..6] != *b"GIF89a" {
        return None;
    }
    let width = u16::from_le_bytes([header[6], header[7]]);
    let height = u16::from_le_bytes([header[8], header[9]]);
    if width == 0 || height == 0 {
        return None;
    }
    Some((i64::from(width), i64::from(height)))
}

fn webp_dimensions(header: &[u8]) -> Option<(i64, i64)> {
    if header.len() < 16 || header[..4] != *b"RIFF" || header[8..12] != *b"WEBP" {
        return None;
    }
    let chunk = &header[12..16];
    if chunk == b"VP8X" {
        if header.len() < 30 {
            return None;
        }
        let width = i64::from(u32::from_le_bytes([0, header[24], header[25], header[26]])) + 1;
        let height = i64::from(u32::from_le_bytes([0, header[27], header[28], header[29]])) + 1;
        return Some((width, height));
    }
    if chunk == b"VP8 " {
        if header.len() < 30 || header[23..26] != [0x9D, 0x01, 0x2A] {
            return None;
        }
        let width = i64::from(u16::from_le_bytes([header[26], header[27]]) & 0x3FFF);
        let height = i64::from(u16::from_le_bytes([header[28], header[29]]) & 0x3FFF);
        if width == 0 || height == 0 {
            return None;
        }
        return Some((width, height));
    }
    if chunk == b"VP8L" {
        if header.len() < 25 || header[20] != 0x2F {
            return None;
        }
        let bits = u32::from_le_bytes([header[21], header[22], header[23], header[24]]);
        let width = i64::from(bits & 0x3FFF) + 1;
        let height = i64::from((bits >> 14) & 0x3FFF) + 1;
        return Some((width, height));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suffix_normalization_matches_python() {
        assert_eq!(normalize_suffix("clip.MP4"), ".mp4");
        assert_eq!(normalize_suffix(".jpg"), ".jpg");
        assert_eq!(normalize_suffix("/tmp/a/b/photo.jpeg"), ".jpeg");
        assert_eq!(normalize_suffix("noext"), "");
    }

    #[test]
    fn frame_rates_parse_like_python() {
        assert_eq!(parse_frame_rate("60/1"), Some(60.0));
        assert_eq!(parse_frame_rate("30000/1001"), Some(29.97002997002997));
        assert_eq!(parse_frame_rate("0/0"), None);
        assert_eq!(parse_frame_rate(""), None);
    }

    #[test]
    fn a_cover_art_stream_is_not_the_video() {
        let streams = vec![
            Stream {
                codec_type: Some("video".to_owned()),
                codec_name: Some("mjpeg".to_owned()),
                ..Stream::default()
            },
            Stream {
                codec_type: Some("video".to_owned()),
                codec_name: Some("h264".to_owned()),
                ..Stream::default()
            },
        ];
        assert_eq!(
            pick_video_stream(&streams).and_then(|stream| stream.codec_name.as_deref()),
            Some("h264")
        );
    }

    #[test]
    fn png_dimensions_come_from_the_header() {
        let mut header = vec![0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
        header.extend_from_slice(&[0, 0, 0, 13]);
        header.extend_from_slice(b"IHDR");
        header.extend_from_slice(&1080_u32.to_be_bytes());
        header.extend_from_slice(&1920_u32.to_be_bytes());
        assert_eq!(png_dimensions(&header), Some((1080, 1920)));
    }
}
