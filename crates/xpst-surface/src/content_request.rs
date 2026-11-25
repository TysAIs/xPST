//! The typed publish request and its content verdict.
//!
//! Parity target: `src/xpst/content.py` — `DestinationOverride`,
//! `parse_destination_texts`, `ContentRequest`, `ContentIssue`,
//! `validate_content_request`, `content_verdict` and `publish_route`.
//!
//! This is the ONE content answer every surface reports for a request. The CLI,
//! the MCP tool, the HTTP API and the dashboard all embed this document (or the
//! fields it carries) instead of re-deriving a content type, re-checking a
//! destination or re-writing a refusal message, so identical inputs produce
//! byte-identical blockers everywhere.
//!
//! Pure: no I/O, no network, no clock. A media path is classified by its suffix
//! and never opened — the preflight plan is the thing that probes a file.
//!
//! **Divergence, recorded in the ledger note:** `ContentRequest::from_payload`
//! reads a JSON document rather than a Python `Mapping`, so a non-object
//! `overrides` value is ignored here where Python's `isinstance(Mapping)` guard
//! also ignores it — the two agree. A non-object *entry* inside `overrides`
//! raises `ContentContractError` in both.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use xpst_core::content::{
    coerce_content_type, content_profile, media_kind, text_limit, ContentContractError,
    ContentType, MediaKind, MAX_MEDIA_ITEMS,
};
use xpst_core::preflight::media_transport_blocker;

/// `SUPPORT_SUPPORTED` / `SUPPORT_UNSUPPORTED` / `SUPPORT_UNKNOWN`.
pub const SUPPORT_SUPPORTED: &str = "supported";
pub const SUPPORT_UNSUPPORTED: &str = "unsupported";
pub const SUPPORT_UNKNOWN: &str = "unknown";

/// `PUBLISH_ROUTE_*`: the publishing path a validated request takes.
pub const PUBLISH_ROUTE_VIDEO: &str = "video";
pub const PUBLISH_ROUTE_IMAGE: &str = "image";
pub const PUBLISH_ROUTE_CAROUSEL: &str = "carousel";
pub const PUBLISH_ROUTE_TEXT: &str = "text";
pub const PUBLISH_ROUTE_UNIMPLEMENTED: &str = "unimplemented";

/// `ContentIssue.severity`.
pub const SEVERITY_ERROR: &str = "error";
pub const SEVERITY_WARNING: &str = "warning";

/// `DestinationOverride`: what one destination gets instead of the shared values.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DestinationOverride {
    pub text: Option<String>,
    pub content_type: Option<ContentType>,
}

impl DestinationOverride {
    /// `DestinationOverride.from_payload`: `{"text": ..., "content_type": ...}`,
    /// or a bare string meaning text.
    ///
    /// An unrecognized `content_type` is kept as `None` rather than raised, so
    /// validation can report it truthfully instead of a parser throwing.
    pub fn from_payload(payload: Option<&Value>) -> Result<Self, ContentContractError> {
        let Some(payload) = payload else {
            return Ok(Self::default());
        };
        if let Some(text) = payload.as_str() {
            return Ok(Self {
                text: Some(text.to_string()),
                content_type: None,
            });
        }
        let Some(map) = payload.as_object() else {
            return Err(ContentContractError::OverrideNotAnObject(
                python_type_name(payload).to_string(),
            ));
        };
        let content_type = map
            .get("content_type")
            .filter(|value| truthy(value))
            .and_then(|value| coerce_content_type(&python_str(value)).ok());
        let raw_text = map.get("text").or_else(|| map.get("caption"));
        Ok(Self {
            text: raw_text.map(python_str),
            content_type,
        })
    }

    /// `DestinationOverride.to_dict()`.
    #[must_use]
    pub fn to_dict(&self) -> Value {
        let mut map = Map::new();
        map.insert(
            "text".to_string(),
            self.text.clone().map_or(Value::Null, Value::String),
        );
        map.insert(
            "content_type".to_string(),
            self.content_type
                .map_or(Value::Null, |item| Value::String(item.as_str().to_string())),
        );
        Value::Object(map)
    }
}

/// `parse_destination_texts()`: `{platform: caption}` from an override payload.
///
/// Accepts the same two shapes [`DestinationOverride::from_payload`] does and
/// drops entries that carry no text, so a caller that falls back to the shared
/// caption keeps the default. A value that is neither shape is an error, so the
/// surface can report the bad payload instead of posting something else.
pub fn parse_destination_texts(
    raw: Option<&Value>,
) -> Result<BTreeMap<String, String>, ContentContractError> {
    Ok(parse_destination_texts_ordered(raw)?.into_iter().collect())
}

/// `parse_destination_texts` with the caller's key order preserved.
///
/// Python's `dict` keeps insertion order, and `xpst_schedule_add` persists the
/// overrides into the schedule entry — so a two-key payload must reach the store
/// in the order the caller wrote it. The `BTreeMap` form above is right for a
/// lookup table and wrong for a document.
pub fn parse_destination_texts_ordered(
    raw: Option<&Value>,
) -> Result<Vec<(String, String)>, ContentContractError> {
    let mut texts: Vec<(String, String)> = Vec::new();
    let Some(Value::Object(map)) = raw else {
        return Ok(texts);
    };
    for (name, value) in map {
        let over = DestinationOverride::from_payload(Some(value))?;
        let key = name.trim().to_lowercase();
        if !key.is_empty() {
            if let Some(text) = over.text {
                texts.push((key, text));
            }
        }
    }
    Ok(texts)
}

/// `ContentRequest`: `{content_type, media[], text, platforms, overrides}`.
///
/// `content_type` is `None` when the caller did not state one; the effective
/// type is then inferred from `media` exactly as the pre-contract pipeline did
/// (one file → video, many → carousel).
#[derive(Debug, Clone, Default)]
pub struct ContentRequest {
    pub content_type: Option<ContentType>,
    pub media: Vec<String>,
    pub text: String,
    pub platforms: Vec<String>,
    pub overrides: BTreeMap<String, DestinationOverride>,
    /// The caller's raw content-type string, kept only so an unrecognized value
    /// can be reported as a truthful blocker instead of raised at parse.
    pub raw_content_type: Option<String>,
}

impl ContentRequest {
    /// `effective_content_type`: explicit when valid, else inferred.
    #[must_use]
    pub fn effective_content_type(&self) -> ContentType {
        match self.content_type {
            Some(content_type) => content_type,
            None => infer_content_type(&self.media),
        }
    }

    /// `is_explicit_content_type`.
    #[must_use]
    pub fn is_explicit_content_type(&self) -> bool {
        self.content_type.is_some()
    }

    /// `media_paths` / `caption`: the legacy accessors.
    #[must_use]
    pub fn media_paths(&self) -> Vec<String> {
        self.media.clone()
    }

    #[must_use]
    pub fn caption(&self) -> &str {
        &self.text
    }

    /// `override_for()`: the override for a destination, matched the way Python
    /// matches it (trimmed, lowercased).
    #[must_use]
    pub fn override_for(&self, platform: &str) -> Option<&DestinationOverride> {
        let key = platform.trim().to_lowercase();
        self.overrides
            .iter()
            .find(|(name, _)| name.trim().to_lowercase() == key)
            .map(|(_, over)| over)
    }

    /// `text_for()`: the destination's override when set, else the shared text.
    #[must_use]
    pub fn text_for(&self, platform: &str) -> String {
        match self
            .override_for(platform)
            .and_then(|over| over.text.clone())
        {
            Some(text) => text,
            None => self.text.clone(),
        }
    }

    /// `content_type_for()`: the destination's override when set, else effective.
    #[must_use]
    pub fn content_type_for(&self, platform: &str) -> ContentType {
        self.override_for(platform)
            .and_then(|over| over.content_type)
            .unwrap_or_else(|| self.effective_content_type())
    }

    /// `per_platform_texts()`: captions that differ from the shared text.
    ///
    /// Destinations without a text override are *absent*, so a caller that falls
    /// back to [`Self::text`] keeps the shared caption for every destination the
    /// user did not write a specific one for.
    #[must_use]
    pub fn per_platform_texts(&self, platforms: Option<&[String]>) -> BTreeMap<String, String> {
        let names: Vec<String> = match platforms {
            Some(items) => items.to_vec(),
            None => self.platforms.clone(),
        };
        let mut captions = BTreeMap::new();
        for name in names {
            let key = name.trim().to_lowercase();
            if key.is_empty() {
                continue;
            }
            if let Some(text) = self.override_for(&key).and_then(|over| over.text.clone()) {
                captions.insert(key, text);
            }
        }
        captions
    }

    /// `to_dict()`: the stable serialization every surface serves.
    #[must_use]
    pub fn to_dict(&self) -> Value {
        let mut overrides = Map::new();
        for (name, over) in &self.overrides {
            overrides.insert(name.clone(), over.to_dict());
        }
        let mut map = Map::new();
        map.insert(
            "content_type".to_string(),
            self.content_type
                .map_or(Value::Null, |item| Value::String(item.as_str().to_string())),
        );
        map.insert(
            "effective_content_type".to_string(),
            Value::String(self.effective_content_type().as_str().to_string()),
        );
        map.insert(
            "media".to_string(),
            Value::Array(self.media.iter().cloned().map(Value::String).collect()),
        );
        map.insert(
            "media_paths".to_string(),
            Value::Array(self.media.iter().cloned().map(Value::String).collect()),
        );
        map.insert("text".to_string(), Value::String(self.text.clone()));
        map.insert("caption".to_string(), Value::String(self.text.clone()));
        map.insert(
            "platforms".to_string(),
            Value::Array(self.platforms.iter().cloned().map(Value::String).collect()),
        );
        map.insert("overrides".to_string(), Value::Object(overrides));
        Value::Object(map)
    }

    /// `ContentRequest.from_payload()`: the one request parser.
    ///
    /// Accepts both the typed shape (`content_type`/`media`/`text`/`overrides`)
    /// and the legacy shape (`media_paths`/`media_path`/`caption`). Unknown keys
    /// are ignored; platform names are lower-cased and deduped in request order.
    pub fn from_payload(payload: Option<&Value>) -> Result<Self, ContentContractError> {
        let empty = Map::new();
        let data = payload.and_then(Value::as_object).unwrap_or(&empty);

        let mut raw_media: Option<Vec<String>> = None;
        if let Some(value) = data.get("media") {
            raw_media = Some(string_list(value));
        }
        if raw_media.is_none() {
            if let Some(value) = data.get("media_paths") {
                raw_media = Some(string_list(value));
            }
        }
        if raw_media.is_none() {
            let single = data
                .get("media_path")
                .filter(|value| truthy(value))
                .or_else(|| data.get("video_path").filter(|value| truthy(value)));
            raw_media = Some(single.map(python_str).into_iter().collect());
        }
        let media: Vec<String> = raw_media
            .unwrap_or_default()
            .into_iter()
            .map(|item| item.trim().to_string())
            .filter(|item| !item.is_empty())
            .take(MAX_MEDIA_ITEMS)
            .collect();

        let raw_text = data.get("text");
        // Whether the caller used the *typed* spelling of a body. `caption` is
        // the legacy spelling (a caption for media) and keeps its old meaning.
        let typed_text = raw_text.is_some_and(|value| !python_str(value).trim().is_empty());
        let text_value = raw_text.or_else(|| data.get("caption"));

        let mut platforms: Vec<String> = Vec::new();
        if let Some(value) = data.get("platforms") {
            for item in string_list(value) {
                let name = item.trim().to_lowercase();
                if !name.is_empty() && !platforms.contains(&name) {
                    platforms.push(name);
                }
            }
        }

        let mut overrides: BTreeMap<String, DestinationOverride> = BTreeMap::new();
        let raw_overrides = data
            .get("overrides")
            .filter(|value| truthy(value))
            .or_else(|| {
                data.get("per_destination_overrides")
                    .filter(|value| truthy(value))
            })
            .or_else(|| {
                data.get("per_platform_captions")
                    .filter(|value| truthy(value))
            });
        if let Some(Value::Object(map)) = raw_overrides {
            for (name, value) in map {
                overrides.insert(
                    name.clone(),
                    DestinationOverride::from_payload(Some(value))?,
                );
            }
        }

        let mut content_type: Option<ContentType> = None;
        let mut raw_content_type: Option<String> = None;
        match data.get("content_type") {
            Some(value) if !python_str(value).trim().is_empty() => {
                let raw = python_str(value).trim().to_string();
                content_type = coerce_content_type(&raw).ok();
                raw_content_type = Some(raw);
            }
            _ if typed_text && media.is_empty() => {
                // The typed spelling of a text post: `text` with no file and no
                // stated type *is* a text post, not a file-less video request.
                content_type = Some(ContentType::Text);
            }
            _ => {}
        }

        Ok(Self {
            content_type,
            media,
            text: text_value.map(python_str).unwrap_or_default(),
            platforms,
            overrides,
            raw_content_type,
        })
    }
}

/// `infer_content_type()`: the pre-contract behaviour, kept deliberately.
///
/// * no media → `TEXT`
/// * one image → `IMAGE`
/// * one other file → `VIDEO` (any single non-image file was treated as a video
///   before this contract existed; keeping that avoids breaking callers)
/// * two or more → `CAROUSEL`
#[must_use]
pub fn infer_content_type(media: &[String]) -> ContentType {
    let cleaned: Vec<&str> = media
        .iter()
        .map(|item| item.trim())
        .filter(|item| !item.is_empty())
        .collect();
    if cleaned.is_empty() {
        return ContentType::Text;
    }
    if cleaned.len() > 1 {
        return ContentType::Carousel;
    }
    if media_kind(cleaned[0]) == MediaKind::Image {
        ContentType::Image
    } else {
        ContentType::Video
    }
}

/// `content_support_status()`: `supported` / `unsupported` / `unknown`.
#[must_use]
pub fn content_support_status(platform: &str, content_type: ContentType) -> &'static str {
    let Some(profile) = content_profile(platform) else {
        return SUPPORT_UNKNOWN;
    };
    if !profile.publishing {
        return SUPPORT_UNSUPPORTED;
    }
    if profile.supports(content_type) {
        SUPPORT_SUPPORTED
    } else {
        SUPPORT_UNSUPPORTED
    }
}

/// `validate_destination_content()`: the refusal message, or `None`.
///
/// Unknown (third-party/plugin) destinations pass: xPST does not fabricate a
/// capability it has not declared, and the uploader reports the real outcome.
#[must_use]
pub fn validate_destination_content(platform: &str, content_type: ContentType) -> Option<String> {
    let profile = content_profile(platform)?;
    if !profile.publishing {
        return Some(format!(
            "{platform} is a messaging destination: it sends DMs and cannot publish {}. Use a \
             publishing destination instead.",
            content_type.label()
        ));
    }
    if profile.supports(content_type) {
        return None;
    }
    let mut supported: Vec<&str> = profile
        .implemented
        .iter()
        .map(|item| item.as_str())
        .collect();
    supported.sort_unstable();
    let supported_list = if supported.is_empty() {
        "none".to_string()
    } else {
        supported.join(", ")
    };
    if profile.declares(content_type) {
        return Some(format!(
            "{platform} does not support {}: it is declared in the provider capabilities but has \
             no publishing implementation yet. Supported content types for {platform}: \
             {supported_list}.",
            content_type.label()
        ));
    }
    Some(format!(
        "{platform} does not support {}. Supported content types for {platform}: {supported_list}.",
        content_type.label()
    ))
}

/// `ContentIssue`: a stable, machine-readable content-contract finding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContentIssue {
    pub code: String,
    pub message: String,
    pub severity: String,
    pub platform: Option<String>,
}

impl ContentIssue {
    fn error(code: &str, message: String) -> Self {
        Self {
            code: code.to_string(),
            message,
            severity: SEVERITY_ERROR.to_string(),
            platform: None,
        }
    }

    fn for_platform(code: &str, message: String, platform: &str) -> Self {
        Self {
            code: code.to_string(),
            message,
            severity: SEVERITY_ERROR.to_string(),
            platform: Some(platform.to_string()),
        }
    }

    fn warning(code: &str, message: String, platform: &str) -> Self {
        Self {
            code: code.to_string(),
            message,
            severity: SEVERITY_WARNING.to_string(),
            platform: Some(platform.to_string()),
        }
    }

    /// `is_error`.
    #[must_use]
    pub fn is_error(&self) -> bool {
        self.severity == SEVERITY_ERROR
    }

    /// `to_dict()`.
    #[must_use]
    pub fn to_dict(&self) -> Value {
        let mut map = Map::new();
        map.insert("code".to_string(), Value::String(self.code.clone()));
        map.insert("message".to_string(), Value::String(self.message.clone()));
        map.insert("severity".to_string(), Value::String(self.severity.clone()));
        map.insert(
            "platform".to_string(),
            self.platform.clone().map_or(Value::Null, Value::String),
        );
        Value::Object(map)
    }
}

/// `_MEDIA_RULES`: per content type, `(min, max, allowed kinds)`.
fn media_rule(content_type: ContentType) -> (usize, usize, &'static [MediaKind]) {
    match content_type {
        ContentType::Video => (1, 1, &[MediaKind::Video, MediaKind::Unknown]),
        ContentType::Image => (1, 1, &[MediaKind::Image]),
        ContentType::Carousel => (
            2,
            MAX_MEDIA_ITEMS,
            &[MediaKind::Video, MediaKind::Image, MediaKind::Unknown],
        ),
        ContentType::Text | ContentType::Thread => (0, 0, &[]),
    }
}

/// `_TEXT_ONLY`: content types that carry no media and therefore require text.
fn is_text_only(content_type: ContentType) -> bool {
    matches!(content_type, ContentType::Text | ContentType::Thread)
}

/// `_media_issues()`.
fn media_issues(content_type: ContentType, media: &[String]) -> Vec<ContentIssue> {
    let (minimum, maximum, allowed) = media_rule(content_type);
    let count = media.len();
    if is_text_only(content_type) {
        if count > 0 {
            let plural = if count == 1 { "" } else { "s" };
            return vec![ContentIssue::error(
                "content_type.media_not_allowed",
                format!(
                    "{} carry no media (got {count} file{plural}); use image, carousel or video \
                     instead.",
                    content_type.label()
                ),
            )];
        }
        return Vec::new();
    }
    if count < minimum || count > maximum {
        let expected = if minimum == maximum {
            format!("exactly {minimum} media file")
        } else {
            format!("between {minimum} and {maximum} media files")
        };
        return vec![ContentIssue::error(
            "content_type.media_count",
            format!("{} take {expected} (got {count}).", content_type.label()),
        )];
    }
    let mut issues = Vec::new();
    for item in media {
        let kind = media_kind(item);
        if !allowed.contains(&kind) {
            // `sorted(allowed_kinds)[0]` — the alphabetically first kind name.
            let mut names: Vec<&str> = allowed.iter().map(|kind| kind.as_str()).collect();
            names.sort_unstable();
            let expected = names.first().copied().unwrap_or_default();
            issues.push(ContentIssue::error(
                "content_type.media_kind",
                format!(
                    "{} need {expected} files; {} is {kind}.",
                    content_type.label(),
                    file_name(item)
                ),
            ));
        }
    }
    issues
}

/// `_text_issues()`: per-destination text findings.
///
/// The limit comes from [`text_limit`] — the same number the sender uses — so a
/// post is refused with the destination and the limit named instead of being
/// silently truncated. A messaging destination is skipped: it is refused for
/// what it is, not for its size.
fn text_issues(request: &ContentRequest) -> Vec<ContentIssue> {
    let mut issues = Vec::new();
    for platform in &request.platforms {
        let key = platform.trim().to_lowercase();
        if let Some(profile) = content_profile(&key) {
            if !profile.publishing {
                continue;
            }
        }
        let content_type = request.content_type_for(&key);
        let body = request.text_for(&key);
        if is_text_only(content_type) && body.trim().is_empty() {
            issues.push(ContentIssue::for_platform(
                "content_type.text_required",
                format!(
                    "{} for {key} need text: the request carried none.",
                    content_type.label()
                ),
                &key,
            ));
            continue;
        }
        if let Some(limit) = text_limit(&key) {
            let length = body.chars().count();
            if length > limit {
                let advice = if request.override_for(&key).is_some() {
                    "shorten the text".to_string()
                } else {
                    format!(
                        "shorten the text, or give {key} its own shorter text with a \
                         per-destination override"
                    )
                };
                issues.push(ContentIssue::for_platform(
                    "content_type.text_too_long",
                    format!(
                        "{key} accepts at most {limit} characters per post; this one has {length}. \
                         xPST will not truncate it — {advice}."
                    ),
                    &key,
                ));
            }
        }
    }
    issues
}

/// `validate_content_request()`: every finding, in a stable order.
///
/// Request shape first, then one entry per destination that cannot take the
/// content type. A destination with no declared capability (a third-party
/// plugin) yields a *warning*, never a block.
#[must_use]
pub fn validate_content_request(request: &ContentRequest) -> Vec<ContentIssue> {
    let mut issues: Vec<ContentIssue> = Vec::new();

    if let Some(raw) = &request.raw_content_type {
        if request.content_type.is_none() {
            let known = xpst_core::content::CONTENT_TYPES
                .iter()
                .map(|item| item.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            issues.push(ContentIssue::error(
                "content_type.unknown",
                format!(
                    "Unknown content type {}: choose one of {known}.",
                    python_repr(raw)
                ),
            ));
            return issues;
        }
    }

    let content_type = request.effective_content_type();
    issues.extend(media_issues(content_type, &request.media));
    issues.extend(text_issues(request));

    let requested: Vec<String> = request
        .platforms
        .iter()
        .map(|platform| platform.to_lowercase())
        .collect();
    for name in request.overrides.keys() {
        let override_key = name.trim().to_lowercase();
        if !requested.contains(&override_key) {
            issues.push(ContentIssue::for_platform(
                "content_type.override_destination",
                format!(
                    "Per-destination overrides were given for {name}, which is not a requested \
                     destination."
                ),
                &override_key,
            ));
        }
    }

    for platform in &request.platforms {
        let platform_key = platform.trim().to_lowercase();
        let destination_type = request.content_type_for(&platform_key);
        if content_profile(&platform_key).is_none() {
            issues.push(ContentIssue::warning(
                "content_type.unknown_destination",
                format!(
                    "{platform_key} does not declare content capabilities; xPST will attempt the \
                     {} post and report the real outcome.",
                    destination_type.as_str()
                ),
                &platform_key,
            ));
            continue;
        }
        if let Some(message) = validate_destination_content(&platform_key, destination_type) {
            issues.push(ContentIssue::for_platform(
                "content_type.unsupported",
                message,
                &platform_key,
            ));
            // A destination that cannot publish this content type at all is
            // already fully explained by that one error; do not stack a media
            // transport complaint on top of it.
            continue;
        }
        // Supported content type — but can this destination receive these bytes?
        for item in &request.media {
            if let Some((code, message)) = media_transport_blocker(&platform_key, item) {
                issues.push(ContentIssue::for_platform(code, message, &platform_key));
                break;
            }
        }
    }

    issues
}

/// `content_blockers()`: just the error messages, in order.
#[must_use]
pub fn content_blockers(request: &ContentRequest) -> Vec<String> {
    validate_content_request(request)
        .into_iter()
        .filter(ContentIssue::is_error)
        .map(|issue| issue.message)
        .collect()
}

/// `blocking_issues()`: just the error findings, with codes and platforms.
#[must_use]
pub fn blocking_issues(request: &ContentRequest) -> Vec<ContentIssue> {
    validate_content_request(request)
        .into_iter()
        .filter(ContentIssue::is_error)
        .collect()
}

/// `content_verdict()`: the ONE content answer every surface reports.
#[must_use]
pub fn content_verdict(request: &ContentRequest) -> Value {
    let issues = validate_content_request(request);
    let mut overrides = Map::new();
    for (name, over) in &request.overrides {
        overrides.insert(name.clone(), over.to_dict());
    }
    let mut map = Map::new();
    map.insert(
        "content_type".to_string(),
        request
            .content_type
            .map_or(Value::Null, |item| Value::String(item.as_str().to_string())),
    );
    map.insert(
        "effective_content_type".to_string(),
        Value::String(request.effective_content_type().as_str().to_string()),
    );
    map.insert(
        "ok".to_string(),
        Value::Bool(!issues.iter().any(ContentIssue::is_error)),
    );
    map.insert(
        "blockers".to_string(),
        Value::Array(
            issues
                .iter()
                .filter(|issue| issue.is_error())
                .map(|issue| Value::String(issue.message.clone()))
                .collect(),
        ),
    );
    map.insert(
        "warnings".to_string(),
        Value::Array(
            issues
                .iter()
                .filter(|issue| !issue.is_error())
                .map(|issue| Value::String(issue.message.clone()))
                .collect(),
        ),
    );
    map.insert(
        "content_issues".to_string(),
        Value::Array(issues.iter().map(ContentIssue::to_dict).collect()),
    );
    map.insert("overrides".to_string(), Value::Object(overrides));
    map.insert(
        "route".to_string(),
        Value::String(publish_route(request).to_string()),
    );
    Value::Object(map)
}

/// `publish_route()`: the ONE modality → publishing-path decision.
#[must_use]
pub fn publish_route(request: &ContentRequest) -> &'static str {
    let content_type = request.effective_content_type();
    let media = request
        .media
        .iter()
        .filter(|item| !item.trim().is_empty())
        .count();
    match content_type {
        ContentType::Video if media == 1 => PUBLISH_ROUTE_VIDEO,
        ContentType::Image if media == 1 => PUBLISH_ROUTE_IMAGE,
        ContentType::Carousel if media >= 2 => PUBLISH_ROUTE_CAROUSEL,
        ContentType::Text if media == 0 => PUBLISH_ROUTE_TEXT,
        _ => PUBLISH_ROUTE_UNIMPLEMENTED,
    }
}

// ── Python-shaped helpers ───────────────────────────────────────────────────

/// `str(value)` for a JSON scalar, the way Python renders it.
fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Null => "None".to_string(),
        Value::Number(number) => number.to_string(),
        other => other.to_string(),
    }
}

/// `type(value).__name__` for the JSON types a payload can carry.
fn python_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(number) if number.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Python truthiness for a JSON value.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// `[str(item) for item in value]`, with a bare string treated as one item.
fn string_list(value: &Value) -> Vec<String> {
    match value {
        Value::Array(items) => items.iter().map(python_str).collect(),
        Value::Null => Vec::new(),
        other => vec![python_str(other)],
    }
}

/// `Path(item).name`.
fn file_name(path: &str) -> String {
    path.rsplit(['/', '\\']).next().unwrap_or(path).to_string()
}

/// `repr(text)` for a Python `str`: single quotes unless the text holds one.
fn python_repr(text: &str) -> String {
    if text.contains('\'') && !text.contains('"') {
        format!("\"{text}\"")
    } else {
        format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}
