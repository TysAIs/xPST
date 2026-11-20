//! The publish sequence: container → (upload) → status → publish.
//!
//! Meta's contract is the same for every content type: create a media container,
//! wait until the container is ready, then publish it. What differs is how the
//! bytes get there:
//!
//! * video from a local file — a resumable upload session
//!   (`upload_type=resumable`, bytes to `rupload.facebook.com`);
//! * video from a public URL — the platform fetches it (`video_url`);
//! * a feed image — always a public URL (`image_url`), never an upload;
//! * a carousel — one child container per item, then a `CAROUSEL` container that
//!   lists them.
//!
//! Every wait in this file is bounded. Instagram's guide recommends checking a
//! container's status "once per minute, for no more than 5 minutes", so the
//! default policy is five checks across a five-minute wall-clock cap; the loop
//! stops on whichever bound is hit first and reports the timeout as its own
//! outcome rather than spinning.

use std::time::{Duration, Instant};

use serde_json::Value;
use xpst_provider::{Candidate, Operation, Platform, Reconciliation, Unconfirmed};

use crate::capability;
use crate::classify::{self, head};
use crate::error::{PublishError, Refusal, Stage};
use crate::limits;
use crate::media::{MediaMeasurements, MediaSource};
use crate::transport::{HttpRequest, Transport};

/// The Graph API version every URL in this crate is built from.
///
/// Mirrors `src/xpst/utils/graph_api.py::GRAPH_API_VERSION` so the two engines
/// cannot disagree about which version they call. Meta removes a version about
/// two years after release, so this is one line and one test to bump, never a
/// literal scattered through the code.
pub const GRAPH_API_VERSION: &str = "v26.0";

/// Where the two hosts live. Injectable so a test — or a later redirect through
/// a proxy — never has to rewrite URLs by hand.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoints {
    /// `graph.facebook.com/<version>`.
    pub graph: String,
    /// `rupload.facebook.com/ig-api-upload/<version>`.
    pub rupload: String,
}

impl Default for Endpoints {
    fn default() -> Self {
        Self {
            graph: format!("https://graph.facebook.com/{GRAPH_API_VERSION}"),
            rupload: format!("https://rupload.facebook.com/ig-api-upload/{GRAPH_API_VERSION}"),
        }
    }
}

/// What the caller must already have: a token with `instagram_content_publish`
/// and the Instagram professional account's id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Credentials {
    pub access_token: String,
    pub user_id: String,
}

impl Credentials {
    pub fn new(access_token: impl Into<String>, user_id: impl Into<String>) -> Self {
        Self {
            access_token: access_token.into(),
            user_id: user_id.into(),
        }
    }
}

/// The clock the sequence reads and sleeps on.
///
/// A seam, not a convenience: it is what lets the status-timeout test prove the
/// bound in milliseconds instead of five real minutes, and it keeps the poll
/// policy honest about *elapsed* time rather than about how many times a loop
/// happened to run.
pub trait Clock {
    /// Monotonic milliseconds since this clock was created.
    fn now_ms(&self) -> u64;
    /// Sleep, advancing [`Clock::now_ms`] by at least `millis`.
    fn sleep_ms(&mut self, millis: u64);
}

/// The real clock, used by the engine.
#[derive(Debug)]
pub struct SystemClock {
    started: Instant,
}

impl SystemClock {
    #[must_use]
    pub fn new() -> Self {
        Self {
            started: Instant::now(),
        }
    }
}

impl Default for SystemClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for SystemClock {
    fn now_ms(&self) -> u64 {
        u64::try_from(self.started.elapsed().as_millis()).unwrap_or(u64::MAX)
    }

    fn sleep_ms(&mut self, millis: u64) {
        std::thread::sleep(Duration::from_millis(millis));
    }
}

/// How long to wait for processing, and how many times to ask.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PollPolicy {
    /// Delay between status checks.
    pub interval_ms: u64,
    /// Hard ceiling on status requests.
    pub max_attempts: u32,
    /// Hard ceiling on elapsed time, sleeps included.
    pub max_total_ms: u64,
}

impl Default for PollPolicy {
    fn default() -> Self {
        // Meta's own recommendation: "query a container's status once per
        // minute, for no more than 5 minutes". Five checks at 60s is inside
        // that, and it is also inside the 200-calls-per-hour budget that makes
        // the Python engine's 5s × 60 inline loop look expensive.
        Self {
            interval_ms: 60_000,
            max_attempts: 5,
            max_total_ms: 300_000,
        }
    }
}

/// Behaviour knobs that are not provider limits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Policy {
    pub poll: PollPolicy,
    /// Bytes per resumable-upload request. 8 MiB is Meta's own example size and
    /// keeps a retryable chunk small enough to resend.
    pub chunk_bytes: usize,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            poll: PollPolicy::default(),
            chunk_bytes: 8 * 1024 * 1024,
        }
    }
}

/// One item of a carousel.
#[derive(Debug, Clone, PartialEq)]
pub struct CarouselItem {
    pub source: MediaSource,
    pub measurements: MediaMeasurements,
}

impl CarouselItem {
    #[must_use]
    pub fn new(source: MediaSource, measurements: MediaMeasurements) -> Self {
        Self {
            source,
            measurements,
        }
    }
}

/// What the caller wants published.
#[derive(Debug, Clone, PartialEq)]
pub enum InstagramPost {
    /// A Reel: local file (resumable upload) or public video URL.
    Reel {
        source: MediaSource,
        measurements: MediaMeasurements,
    },
    /// A feed image. Always a public URL.
    Image {
        source: MediaSource,
        measurements: MediaMeasurements,
    },
    /// A carousel of 2–10 items, each by its own kind's rule.
    Carousel { items: Vec<CarouselItem> },
}

/// One publish request.
#[derive(Debug, Clone, PartialEq)]
pub struct PublishRequest {
    pub caption: String,
    pub post: InstagramPost,
    /// Posts this account published in the last 24 hours, as far as the caller
    /// knows. `Publisher::publishing_usage` reads Meta's own counter when the
    /// caller would rather ask than remember.
    pub posts_in_last_24h: u32,
}

/// What a successful publish produced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishReceipt {
    /// The Instagram media id returned by `media_publish`.
    ///
    /// `None` means the publication was proven another way — the container's own
    /// `PUBLISHED` status, seen by [`Publisher::resume_publish`] — and the media
    /// id has to be looked up. It is never `None` for a publish that received
    /// Meta's answer, and [`Self::container_id`] identifies the post either way.
    pub media_id: Option<String>,
    /// The container that was published.
    pub container_id: String,
    /// How many status checks this call spent waiting for processing.
    pub status_polls: u32,
}

/// Meta's own view of the 24-hour publishing allowance.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PublishingUsage {
    pub quota_usage: u32,
    pub quota_total: u32,
    pub quota_duration_seconds: u64,
}

/// A container that was created but not yet published.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Container {
    id: String,
    /// The upload URL when Meta returned one. The reference documents the
    /// resumable response as `{"id": …, "uri": …}`; when a server omits `uri`
    /// the documented path is reconstructed from the id.
    uri: Option<String>,
}

/// The state a container reported.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ContainerState {
    Ready,
    AlreadyPublished,
}

/// The sequence itself.
pub struct Publisher<'a, T: Transport, C: Clock> {
    transport: &'a mut T,
    clock: &'a mut C,
    credentials: &'a Credentials,
    endpoints: Endpoints,
    policy: Policy,
}

impl<'a, T: Transport, C: Clock> Publisher<'a, T, C> {
    pub fn new(transport: &'a mut T, clock: &'a mut C, credentials: &'a Credentials) -> Self {
        Self {
            transport,
            clock,
            credentials,
            endpoints: Endpoints::default(),
            policy: Policy::default(),
        }
    }

    #[must_use]
    pub fn with_endpoints(mut self, endpoints: Endpoints) -> Self {
        self.endpoints = endpoints;
        self
    }

    #[must_use]
    pub fn with_policy(mut self, policy: Policy) -> Self {
        self.policy = policy;
        self
    }

    /// Everything that can be decided without a request.
    ///
    /// Public so the preflight surfaces can answer "would this publish?" with the
    /// same code path the publisher uses. A `Refusal` here guarantees no request
    /// was made.
    pub fn preflight(&self, request: &PublishRequest) -> Result<(), Refusal> {
        if let Some(refusal) = limits::daily_cap_violation(request.posts_in_last_24h) {
            return Err(refusal);
        }
        if let Some(refusal) = limits::caption_check(&request.caption) {
            return Err(refusal);
        }

        match &request.post {
            InstagramPost::Reel {
                source,
                measurements,
            } => {
                if let Some(refusal) = limits::media_check(source, measurements) {
                    return Err(refusal);
                }
                Ok(())
            }
            InstagramPost::Image {
                source,
                measurements,
            } => {
                // The declaration answers before any upload: a local image is
                // not a request Meta would reject, it is a request that cannot
                // be expressed.
                if let Some(refusal) = capability::local_image_refusal(source) {
                    return Err(refusal);
                }
                if let Some(refusal) = limits::image_violation(source.local_path(), measurements) {
                    return Err(refusal);
                }
                Ok(())
            }
            InstagramPost::Carousel { items } => {
                if let Some(refusal) = limits::carousel_violation(items.len()) {
                    return Err(refusal);
                }
                for item in items {
                    if is_local_image(&item.source) {
                        if let Some(refusal) = capability::local_image_refusal(&item.source) {
                            return Err(refusal);
                        }
                    }
                    if let Some(refusal) = limits::media_check(&item.source, &item.measurements) {
                        return Err(refusal);
                    }
                }
                Ok(())
            }
        }
    }

    /// Publish, refusing locally when the request cannot succeed.
    pub fn publish(&mut self, request: PublishRequest) -> Result<PublishReceipt, PublishError> {
        self.preflight(&request)?;

        match request.post {
            InstagramPost::Reel {
                source,
                measurements,
            } => self.publish_video(&request.caption, &source, &measurements),
            InstagramPost::Image { source, .. } => self.publish_image(&request.caption, &source),
            InstagramPost::Carousel { items } => self.publish_carousel(&request.caption, &items),
        }
    }

    /// Read Meta's own count of posts published in the last 24 hours.
    pub fn publishing_usage(&mut self) -> Result<PublishingUsage, PublishError> {
        let url = format!(
            "{}/{}/content_publishing_limit",
            self.endpoints.graph, self.credentials.user_id
        );
        let token = self.credentials.access_token.clone();
        let request = HttpRequest::get(
            url,
            vec![
                ("fields".to_string(), "quota_usage,config".to_string()),
                ("access_token".to_string(), token),
            ],
        );
        let value = self.send_json(Stage::ReadQuota, request)?;

        let entry = value
            .get("data")
            .and_then(Value::as_array)
            .and_then(|entries| entries.first())
            .ok_or_else(|| PublishError::MissingField {
                stage: Stage::ReadQuota,
                field: "data",
                body: head(&value.to_string()),
            })?;

        // Required, not defaulted: treating a missing counter as "no posts
        // today" would let the daily cap be bypassed by a response shape
        // change, which is the one direction this check must never fail in.
        let quota_usage = entry
            .get("quota_usage")
            .and_then(Value::as_u64)
            .and_then(|value| u32::try_from(value).ok())
            .ok_or_else(|| PublishError::MissingField {
                stage: Stage::ReadQuota,
                field: "quota_usage",
                body: head(&entry.to_string()),
            })?;
        let config = entry.get("config");
        let quota_total = config
            .and_then(|config| config.get("quota_total"))
            .and_then(Value::as_u64)
            .and_then(|value| u32::try_from(value).ok())
            .unwrap_or(limits::MAX_POSTS_PER_DAY);
        let quota_duration_seconds = config
            .and_then(|config| config.get("quota_duration"))
            .and_then(Value::as_u64)
            .unwrap_or(86_400);

        Ok(PublishingUsage {
            quota_usage,
            quota_total,
            quota_duration_seconds,
        })
    }

    fn publish_video(
        &mut self,
        caption: &str,
        source: &MediaSource,
        _measurements: &MediaMeasurements,
    ) -> Result<PublishReceipt, PublishError> {
        let (container_id, status_polls) = match source.public_url() {
            Some(url) => {
                let container = self.create_container(vec![
                    ("media_type".to_string(), "REELS".to_string()),
                    ("video_url".to_string(), url.to_string()),
                    ("caption".to_string(), caption.to_string()),
                ])?;
                let polls = self.poll_container(&container.id)?;
                (container.id, polls)
            }
            None => {
                let data = source.read_bytes()?;
                if data.is_empty() {
                    return Err(limits::empty_media_refusal().into());
                }
                let container = self.create_container(vec![
                    ("media_type".to_string(), "REELS".to_string()),
                    ("upload_type".to_string(), "resumable".to_string()),
                    ("caption".to_string(), caption.to_string()),
                ])?;
                self.upload_bytes(&container, &data)?;
                let polls = self.poll_container(&container.id)?;
                (container.id, polls)
            }
        };

        let media_id = self.publish_container(&container_id)?;
        Ok(PublishReceipt {
            media_id: Some(media_id),
            container_id,
            status_polls,
        })
    }

    fn publish_image(
        &mut self,
        caption: &str,
        source: &MediaSource,
    ) -> Result<PublishReceipt, PublishError> {
        // `preflight` refused this already; the unwrap-shaped branch keeps the
        // type system honest about it without inventing a URL.
        let Some(url) = source.public_url() else {
            return Err(capability::local_image_refusal(source)
                .unwrap_or_else(|| {
                    Refusal::new(
                        "instagram.image.needs_public_url",
                        "Instagram feed images need a public URL.",
                    )
                })
                .into());
        };

        let container = self.create_container(vec![
            ("image_url".to_string(), url.to_string()),
            ("caption".to_string(), caption.to_string()),
        ])?;
        let status_polls = self.poll_container(&container.id)?;
        let media_id = self.publish_container(&container.id)?;
        Ok(PublishReceipt {
            media_id: Some(media_id),
            container_id: container.id,
            status_polls,
        })
    }

    fn publish_carousel(
        &mut self,
        caption: &str,
        items: &[CarouselItem],
    ) -> Result<PublishReceipt, PublishError> {
        let mut children: Vec<String> = Vec::with_capacity(items.len());
        let mut status_polls = 0;

        for item in items {
            let (child_id, polls) = self.create_carousel_child(item)?;
            status_polls += polls;
            children.push(child_id);
        }

        let container = self.create_container(vec![
            ("media_type".to_string(), "CAROUSEL".to_string()),
            ("children".to_string(), children.join(",")),
            ("caption".to_string(), caption.to_string()),
        ])?;
        status_polls += self.poll_container(&container.id)?;
        let media_id = self.publish_container(&container.id)?;

        Ok(PublishReceipt {
            media_id: Some(media_id),
            container_id: container.id,
            status_polls,
        })
    }

    fn create_carousel_child(
        &mut self,
        item: &CarouselItem,
    ) -> Result<(String, u32), PublishError> {
        match &item.source {
            MediaSource::PublicUrl(url) => {
                let mut form = vec![("is_carousel_item".to_string(), "true".to_string())];
                let mut polls = 0;
                if is_video_url(url) {
                    form.push(("media_type".to_string(), "VIDEO".to_string()));
                    form.push(("video_url".to_string(), url.clone()));
                    // Only video has a processing stage to wait for; an image
                    // container is ready as soon as it exists, so asking costs a
                    // request and tells us nothing.
                    let container = self.create_container(form)?;
                    polls = self.poll_container(&container.id)?;
                    return Ok((container.id, polls));
                }
                // Feed images need no `media_type`; the container is an image
                // container by virtue of carrying `image_url`.
                form.push(("image_url".to_string(), url.clone()));
                let container = self.create_container(form)?;
                Ok((container.id, polls))
            }
            MediaSource::LocalFile(_) => {
                let data = item.source.read_bytes()?;
                if data.is_empty() {
                    return Err(limits::empty_media_refusal().into());
                }
                let container = self.create_container(vec![
                    ("media_type".to_string(), "VIDEO".to_string()),
                    ("is_carousel_item".to_string(), "true".to_string()),
                    ("upload_type".to_string(), "resumable".to_string()),
                ])?;
                self.upload_bytes(&container, &data)?;
                let polls = self.poll_container(&container.id)?;
                Ok((container.id, polls))
            }
        }
    }

    fn create_container(
        &mut self,
        params: Vec<(String, String)>,
    ) -> Result<Container, PublishError> {
        let url = format!(
            "{}/{}/media",
            self.endpoints.graph, self.credentials.user_id
        );
        let request = HttpRequest::post_form(url, self.with_token(params));
        let value = self.send_json(Stage::CreateContainer, request)?;

        let id = self.required_string(&value, "id", Stage::CreateContainer)?;
        let uri = value
            .get("uri")
            .and_then(Value::as_str)
            .map(ToString::to_string);
        Ok(Container { id, uri })
    }

    fn publish_container(&mut self, container_id: &str) -> Result<String, PublishError> {
        let url = format!(
            "{}/{}/media_publish",
            self.endpoints.graph, self.credentials.user_id
        );
        let request = HttpRequest::post_form(
            url,
            self.with_token(vec![("creation_id".to_string(), container_id.to_string())]),
        );
        // This call is the one that creates the post, and it is not idempotent:
        // Meta has no idempotency key, so a second `media_publish` for the same
        // container is a second post attempt. A lost answer therefore cannot be
        // reported as a failure — it has to name the container so it can be
        // reconciled.
        let response = self.transport.send(&request).map_err(|error| {
            PublishError::Unconfirmed(Unconfirmed::new(
                Platform::Instagram,
                Operation::PublishContainer,
                vec![Candidate::container(container_id)],
                Reconciliation::ReadContainerStatus {
                    container_id: container_id.to_string(),
                    url: self.container_status_url(container_id),
                },
                format!(
                    "media_publish for container {container_id} was sent and no answer came back \
                     ({error}). Meta may have published it."
                ),
            ))
        })?;
        let outcome = classify::classify(&response);
        if !outcome.is_success() {
            return Err(PublishError::Response {
                stage: Stage::Publish,
                outcome,
            });
        }
        let value: Value =
            serde_json::from_str(&response.body).map_err(|_| PublishError::MalformedBody {
                stage: Stage::Publish,
                detail: head(&response.body),
            })?;
        self.required_string(&value, "id", Stage::Publish)
    }

    /// The `GET` that answers "did this container become a post?".
    fn container_status_url(&self, container_id: &str) -> String {
        format!("{}/{container_id}?fields=status_code", self.endpoints.graph)
    }

    /// Ask Meta whether a container has already been published.
    ///
    /// `PUBLISHED` is the only answer that proves it. Anything else — including
    /// `FINISHED`, which means "ready but not published" — is reported as not
    /// published, because treating an unknown status as published would report a
    /// post that does not exist.
    fn container_published(&mut self, container_id: &str) -> Result<bool, PublishError> {
        let request = HttpRequest::get(self.container_status_url(container_id), {
            let mut query = vec![("fields".to_string(), "status_code".to_string())];
            query.push((
                "access_token".to_string(),
                self.credentials.access_token.clone(),
            ));
            query
        });
        let value = self.send_json(Stage::PollStatus, request)?;
        Ok(matches!(
            container_state(&value),
            Some(ContainerState::AlreadyPublished)
        ))
    }

    /// Finish a publish that came back [`PublishError::Unconfirmed`].
    ///
    /// Reads the container back **first**, and publishes only when that read-back
    /// proves the container never produced a post. This is the only correct order:
    /// `media_publish` has no idempotency key, so publishing before checking is
    /// exactly how a lost response becomes two posts.
    ///
    /// A container whose status read also fails stays unconfirmed — the caller
    /// gets the same [`PublishError::Unconfirmed`] back rather than a guess.
    pub fn resume_publish(
        &mut self,
        unresolved: &Unconfirmed,
    ) -> Result<PublishReceipt, PublishError> {
        if unresolved.platform != Platform::Instagram {
            return Err(PublishError::CannotReconcile {
                detail: format!(
                    "the outcome belongs to {}, not Instagram",
                    unresolved.platform.label()
                ),
            });
        }
        let Some(container_id) = unresolved.container_id().map(ToOwned::to_owned) else {
            return Err(PublishError::CannotReconcile {
                detail: "the unconfirmed outcome does not name a media container".to_string(),
            });
        };

        if self.container_published(&container_id)? {
            // Meta published it and we never saw the answer. The container id is
            // the handle we have; the media id has to be looked up, so it is
            // reported as unknown rather than invented.
            return Ok(PublishReceipt {
                media_id: None,
                container_id,
                status_polls: 1,
            });
        }

        let media_id = self.publish_container(&container_id)?;
        Ok(PublishReceipt {
            media_id: Some(media_id),
            container_id,
            status_polls: 1,
        })
    }

    fn upload_bytes(&mut self, container: &Container, data: &[u8]) -> Result<(), PublishError> {
        let uri = container
            .uri
            .clone()
            .unwrap_or_else(|| format!("{}/{}", self.endpoints.rupload, container.id));
        let total = data.len();
        let chunk = self.policy.chunk_bytes.max(1);

        let mut offset = 0;
        while offset < total {
            let end = (offset + chunk).min(total);
            let headers = vec![
                (
                    "Authorization".to_string(),
                    format!("OAuth {}", self.credentials.access_token),
                ),
                ("offset".to_string(), offset.to_string()),
                ("file_size".to_string(), total.to_string()),
                (
                    "Content-Type".to_string(),
                    "application/octet-stream".to_string(),
                ),
            ];
            let request = HttpRequest::post_bytes(uri.clone(), headers, data[offset..end].to_vec());

            let response =
                self.transport
                    .send(&request)
                    .map_err(|error| PublishError::Transport {
                        stage: Stage::UploadMedia,
                        detail: error.message,
                    })?;
            let outcome = classify::classify(&response);
            if !outcome.is_success() {
                return Err(PublishError::Response {
                    stage: Stage::UploadMedia,
                    outcome,
                });
            }

            // The upload endpoint answers `{"success": true}`, and a rejection
            // arrives as `{"debug_info": {"retriable": …, "message": "…"}}` with
            // a 2xx status. Treating a 2xx as success here is how a broken
            // upload turns into a timeout five minutes later.
            if let Ok(value) = serde_json::from_str::<Value>(&response.body) {
                if value.get("success").and_then(Value::as_bool) == Some(false) {
                    return Err(upload_rejection(&value, &response.body));
                }
            }

            offset = end;
        }
        Ok(())
    }

    fn poll_container(&mut self, container_id: &str) -> Result<u32, PublishError> {
        let started = self.clock.now_ms();
        let mut attempts: u32 = 0;

        loop {
            attempts += 1;
            let url = format!("{}/{}", self.endpoints.graph, container_id);
            let request = HttpRequest::get(url, {
                let mut query = vec![("fields".to_string(), "status_code".to_string())];
                query.push((
                    "access_token".to_string(),
                    self.credentials.access_token.clone(),
                ));
                query
            });
            let value = self.send_json(Stage::PollStatus, request)?;

            match container_state(&value) {
                Some(ContainerState::Ready) => return Ok(attempts),
                Some(ContainerState::AlreadyPublished) => {
                    return Err(PublishError::AlreadyPublished {
                        container_id: container_id.to_string(),
                    })
                }
                None => {}
            }

            let status = container_status(&value);
            match status.as_str() {
                "ERROR" => {
                    return Err(PublishError::ContainerFailed {
                        detail: container_detail(&value),
                    })
                }
                "EXPIRED" => {
                    return Err(PublishError::ContainerExpired {
                        detail: container_detail(&value),
                    })
                }
                _ => {}
            }

            let elapsed = self.clock.now_ms().saturating_sub(started);
            if attempts >= self.policy.poll.max_attempts
                || elapsed.saturating_add(self.policy.poll.interval_ms)
                    > self.policy.poll.max_total_ms
            {
                return Err(PublishError::StatusPollTimeout {
                    attempts,
                    elapsed_ms: elapsed,
                });
            }
            self.clock.sleep_ms(self.policy.poll.interval_ms);
        }
    }

    fn send_json(&mut self, stage: Stage, request: HttpRequest) -> Result<Value, PublishError> {
        let response = self
            .transport
            .send(&request)
            .map_err(|error| PublishError::Transport {
                stage,
                detail: error.message,
            })?;
        let outcome = classify::classify(&response);
        if !outcome.is_success() {
            return Err(PublishError::Response { stage, outcome });
        }
        serde_json::from_str(&response.body).map_err(|_| PublishError::MalformedBody {
            stage,
            detail: head(&response.body),
        })
    }

    fn with_token(&self, params: Vec<(String, String)>) -> Vec<(String, String)> {
        let mut form = params;
        form.push((
            "access_token".to_string(),
            self.credentials.access_token.clone(),
        ));
        form
    }

    fn required_string(
        &self,
        value: &Value,
        field: &'static str,
        stage: Stage,
    ) -> Result<String, PublishError> {
        value
            .get(field)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .map(ToString::to_string)
            .ok_or_else(|| PublishError::MissingField {
                stage,
                field,
                body: head(&value.to_string()),
            })
    }
}

/// The container's `status_code`, whatever shape the payload used.
fn container_status(value: &Value) -> String {
    value
        .get("status_code")
        .and_then(Value::as_str)
        .or_else(|| {
            value
                .get("status")
                .and_then(|status| status.get("video_status"))
                .and_then(Value::as_str)
        })
        .or_else(|| value.get("status").and_then(Value::as_str))
        .unwrap_or_default()
        .trim()
        .to_ascii_uppercase()
}

fn container_state(value: &Value) -> Option<ContainerState> {
    match container_status(value).as_str() {
        "FINISHED" => Some(ContainerState::Ready),
        "PUBLISHED" => Some(ContainerState::AlreadyPublished),
        _ => None,
    }
}

/// Meta's own words for a failed container, if it gave any.
fn container_detail(value: &Value) -> String {
    value
        .get("status")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .or_else(|| {
            value
                .get("status")
                .and_then(|status| status.get("error_message"))
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .unwrap_or_else(|| head(&value.to_string()))
}

fn upload_rejection(value: &Value, body: &str) -> PublishError {
    let debug = value.get("debug_info");
    let detail = debug
        .and_then(|debug| debug.get("message"))
        .and_then(Value::as_str)
        .map_or_else(|| head(body), ToString::to_string);
    let retriable = debug
        .and_then(|debug| debug.get("retriable"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    PublishError::UploadRejected { detail, retriable }
}

/// Whether a source is a local file that names a still image.
fn is_local_image(source: &MediaSource) -> bool {
    source.local_path().is_some_and(limits::is_image_path)
}

/// Whether a URL names a video by its extension.
///
/// A URL carries no container metadata we can read without fetching it, so the
/// extension is the only hint available. An unrecognised URL is treated as an
/// image — the shape that needs no `media_type` — and if it is really a video,
/// Meta answers honestly instead of this crate guessing further.
fn is_video_url(url: &str) -> bool {
    let path = url.split('?').next().unwrap_or(url);
    let path = path.split('#').next().unwrap_or(path);
    let suffix = path
        .rsplit('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    matches!(suffix.as_str(), "mp4" | "mov" | "m4v")
}
