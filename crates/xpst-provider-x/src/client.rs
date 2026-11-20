//! The publish boundary.
//!
//! One method — [`XPublisher::publish`] — owns the order of every check and
//! every byte that leaves the process:
//!
//! 1. credentials present (else `needs_reauth`, no request sent);
//! 2. text empty / over length, refused with the measured count;
//! 3. a media post must carry an asset key, so the cross-account rule can run;
//! 4. cross-account duplicate refused against the ledger, under the documented
//!    policy;
//! 5. cost cap, if the caller set one — refused, never truncated;
//! 6. media upload (`INIT` → `APPEND`×n → `FINALIZE` → `STATUS`×n) for media
//!    posts, stopping only on an explicit `succeeded` from X;
//! 7. `POST /2/tweets`, OAuth 1.0a signed;
//! 8. only then is the asset recorded, so a failed post never blocks the retry
//!    and a successful one always blocks the second account.
//!
//! Steps 1–5 make no network call, which is why a refusal here costs nothing.
//! Steps 6–7 are the only places money is spent.
//!
//! ## Success is not one shape
//!
//! X's v1.1 upload commands do not agree on what a successful response looks
//! like, and this module refuses to pretend otherwise. Each call site states
//! the shape it expects ([`SuccessShape`]):
//!
//! | Endpoint | Status | Body |
//! |---|---|---|
//! | `POST media/upload.json` `INIT` | 202 | JSON (`media_id_string`) |
//! | `POST media/upload.json` `APPEND` | **204** | **none — zero bytes** |
//! | `POST media/upload.json` `FINALIZE` | 200 | JSON (`media_id`, maybe `processing_info`) |
//! | `GET media/upload.json` `STATUS` | 200 | JSON (`processing_info`) |
//! | `POST /2/tweets` | 201 | JSON (`data.id`) |
//!
//! Requiring a JSON document from `APPEND` is not a harmless strictness: the
//! live endpoint answers 204, so every append fails and no media post can ever
//! be published. The inverse mistake — swallowing parse errors everywhere —
//! would turn a truncated or proxied response into a silent success on the
//! endpoints whose bodies carry the media id. Both directions are tested.
//!
//! ## Only `succeeded` is ready
//!
//! `FINALIZE` may answer while the media is still being processed
//! (`processing_info.state` of `pending` or `in_progress`, with
//! `check_after_secs`). Posting then references media that may not exist yet.
//! [`XPublisher::publish`] therefore polls `STATUS` until the state is exactly
//! `succeeded`, with a bounded number of polls, and reports anything it could
//! not confirm as [`XError::MediaProcessingUnconfirmed`] instead of posting.

use std::fmt;
use std::time::Duration;

use serde_json::{json, Value};

use crate::cost::{Cost, CostCap};
use crate::duplicate::{DuplicatePolicy, PublicationLedger, PublishedEntry};
use crate::error::{from_response, XError};
use crate::media::{Media, MEDIA_CHUNK_BYTES};
use crate::oauth1::{fresh_nonce, percent_encode, unix_timestamp, Credentials, OAuth1Signer};
use crate::preflight::{self, PreflightReport};
use crate::request::PublishRequest;
use crate::transport::{Body, Method, Multipart, Request, Response, Transport, TransportError};

/// How many `STATUS` polls one upload may spend waiting for media to process.
///
/// A bound exists so a media job that never finishes cannot pin a publishing
/// queue indefinitely: after this many polls the upload is reported as
/// unconfirmed and nothing is posted. Twenty polls at X's own `check_after_secs`
/// covers the documented ceiling for video processing.
pub const MAX_PROCESSING_POLLS: usize = 20;

/// Poll interval used when `FINALIZE`/`STATUS` does not say how long to wait.
const DEFAULT_PROCESSING_POLL_SECONDS: u64 = 1;

/// Base URLs, kept as data so tests point at a fake host.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoints {
    /// v2 API root, e.g. `https://api.twitter.com/2`.
    pub api: String,
    /// v1.1 upload root, e.g. `https://upload.twitter.com/1.1`.
    pub upload: String,
}

impl Default for Endpoints {
    fn default() -> Self {
        Self {
            api: "https://api.twitter.com/2".to_string(),
            upload: "https://upload.twitter.com/1.1".to_string(),
        }
    }
}

impl Endpoints {
    #[must_use]
    pub fn new(api: impl Into<String>, upload: impl Into<String>) -> Self {
        Self {
            api: api.into(),
            upload: upload.into(),
        }
    }

    fn tweets(&self) -> String {
        format!("{}/tweets", self.api.trim_end_matches('/'))
    }

    fn media_upload(&self) -> String {
        format!("{}/media/upload.json", self.upload.trim_end_matches('/'))
    }
}

/// Time, as the boundary needs it (rate-limit text, duplicate window).
///
/// Injected so the duplicate-window tests are deterministic instead of racing
/// midnight.
pub trait Clock {
    fn now_unix(&self) -> i64;
}

/// The real clock.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_unix(&self) -> i64 {
        unix_timestamp()
    }
}

/// Pauses between media-processing polls.
///
/// Injected for the same reason [`Clock`] is: a test that really waits the
/// documented processing interval is a test nobody runs, and a fake that cannot
/// pause cannot show that the client waited instead of posting immediately.
pub trait Wait {
    fn pause(&mut self, seconds: u64);
}

/// The real pause.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemWait;

impl Wait for SystemWait {
    fn pause(&mut self, seconds: u64) {
        if seconds > 0 {
            std::thread::sleep(Duration::from_secs(seconds));
        }
    }
}

/// What a successful response from a given endpoint is expected to carry.
///
/// This is per call site, not a general parser setting: it is the only way to
/// accept X's 204 from `APPEND` without also accepting a truncated body from
/// `INIT`, `FINALIZE`, `STATUS` or `POST /2/tweets`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SuccessShape {
    /// Success carries a JSON document this crate reads. An empty or
    /// unparseable body is a failure, because the field the caller needs (the
    /// media id, the post id) is only in that document.
    Json,
    /// Success is an acknowledgement with no body. `APPEND` answers `204 No
    /// Content`; nothing downstream reads its body, so no body is required and
    /// none is parsed.
    NoBody,
}

/// What was published, and what it cost.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Published {
    pub post_id: String,
    pub url: String,
    pub cost: Cost,
    /// The identity recorded in the ledger, if the post had media.
    pub asset_key: Option<String>,
}

/// X publishing over an injected [`Transport`].
pub struct XPublisher<T: Transport> {
    transport: T,
    signer: OAuth1Signer,
    endpoints: Endpoints,
    cost_cap: Option<CostCap>,
    duplicate_policy: DuplicatePolicy,
    ledger: PublicationLedger,
    clock: Box<dyn Clock>,
    wait: Box<dyn Wait>,
}

impl<T: Transport> fmt::Debug for XPublisher<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("XPublisher")
            .field("endpoints", &self.endpoints)
            .field("cost_cap", &self.cost_cap)
            .field("duplicate_policy", &self.duplicate_policy)
            .field("ledger_entries", &self.ledger.entries().len())
            .finish_non_exhaustive()
    }
}

impl<T: Transport> XPublisher<T> {
    #[must_use]
    pub fn new(transport: T, credentials: Credentials) -> Self {
        Self {
            transport,
            signer: OAuth1Signer::new(credentials),
            endpoints: Endpoints::default(),
            cost_cap: None,
            duplicate_policy: DuplicatePolicy::documented(),
            ledger: PublicationLedger::new(),
            clock: Box::new(SystemClock),
            wait: Box::new(SystemWait),
        }
    }

    #[must_use]
    pub fn with_endpoints(mut self, endpoints: Endpoints) -> Self {
        self.endpoints = endpoints;
        self
    }

    /// Set a spend ceiling. Without one, expensive posts are reported but never
    /// refused on cost.
    #[must_use]
    pub fn with_cost_cap(mut self, cap: CostCap) -> Self {
        self.cost_cap = Some(cap);
        self
    }

    /// Weaken the documented duplicate rule deliberately (see
    /// [`DuplicatePolicy`]).
    #[must_use]
    pub fn with_duplicate_policy(mut self, policy: DuplicatePolicy) -> Self {
        self.duplicate_policy = policy;
        self
    }

    #[must_use]
    pub fn with_clock(mut self, clock: Box<dyn Clock>) -> Self {
        self.clock = clock;
        self
    }

    /// Replace the pause between media-processing polls, so tests can prove the
    /// client waits without actually waiting.
    #[must_use]
    pub fn with_wait(mut self, wait: Box<dyn Wait>) -> Self {
        self.wait = wait;
        self
    }

    /// Load prior publications so the cross-account rule survives a restart
    /// once `xpst-store` supplies them.
    #[must_use]
    pub fn with_ledger(mut self, ledger: PublicationLedger) -> Self {
        self.ledger = ledger;
        self
    }

    /// Consume the publisher and return its transport, so a caller (or a test)
    /// can inspect what was sent.
    #[must_use]
    pub fn into_transport(self) -> T {
        self.transport
    }

    #[must_use]
    pub fn ledger(&self) -> &PublicationLedger {
        &self.ledger
    }

    /// Price a batch without sending anything.
    #[must_use]
    pub fn preflight(&self, requests: &[PublishRequest]) -> PreflightReport {
        let now = self.clock.now_unix();
        let policy = self.duplicate_policy;
        preflight::report(requests, self.cost_cap, |request| {
            request
                .asset_key
                .as_deref()
                .and_then(|key| self.ledger.conflict(key, &request.account, now, policy))
        })
    }

    /// Publish one post, or explain exactly why not.
    pub fn publish(&mut self, request: &PublishRequest) -> Result<Published, XError> {
        self.check_credentials()?;

        let cost = request.cost();
        self.check_text(request)?;
        let asset_key = self.checked_asset_key(request)?;
        self.check_duplicate(request, asset_key.as_deref())?;
        self.check_cost(cost)?;

        let media_id = match &request.media {
            Some(media) => Some(self.upload_media(media)?),
            None => None,
        };
        let post_id = self.create_post(&request.text, media_id.as_deref())?;

        if let Some(key) = &asset_key {
            self.ledger.record(PublishedEntry::new(
                key.clone(),
                request.account.clone(),
                self.clock.now_unix(),
                Some(post_id.clone()),
            ));
        }

        Ok(Published {
            url: format!("https://x.com/i/status/{post_id}"),
            post_id,
            cost,
            asset_key,
        })
    }

    fn check_credentials(&self) -> Result<(), XError> {
        if self.signer.credentials().is_complete() {
            Ok(())
        } else {
            Err(XError::NeedsReauth {
                detail: "consumer key/secret and access token/secret are all required; \
                         complete the OAuth flow again"
                    .to_string(),
            })
        }
    }

    fn check_text(&self, request: &PublishRequest) -> Result<(), XError> {
        if request.media.is_none() && request.text.trim().is_empty() {
            return Err(XError::TextEmpty);
        }
        if let Some(limit) = preflight::x_text_limit() {
            let measured = request.text_length();
            if measured > limit {
                return Err(XError::TextTooLong { measured, limit });
            }
        }
        Ok(())
    }

    fn checked_asset_key(&self, request: &PublishRequest) -> Result<Option<String>, XError> {
        match request.asset_key.as_deref().map(str::trim) {
            Some(key) if !key.is_empty() => Ok(Some(key.to_string())),
            _ if request.media.is_some() => Err(XError::AssetKeyRequired),
            _ => Ok(None),
        }
    }

    fn check_duplicate(
        &self,
        request: &PublishRequest,
        asset_key: Option<&str>,
    ) -> Result<(), XError> {
        let Some(key) = asset_key else {
            return Ok(());
        };
        let now = self.clock.now_unix();
        match self
            .ledger
            .conflict(key, &request.account, now, self.duplicate_policy)
        {
            Some(conflict) => Err(XError::DuplicateAcrossAccounts {
                asset_key: conflict.asset_key,
                other_account: conflict.other_account,
                posted_at: conflict.posted_at,
                window_seconds: conflict.window_seconds,
            }),
            None => Ok(()),
        }
    }

    fn check_cost(&self, cost: Cost) -> Result<(), XError> {
        match self.cost_cap {
            Some(cap) if cost.micros > cap.max_micros => Err(XError::CostCapExceeded {
                planned_micros: cost.micros,
                cap_micros: cap.max_micros,
            }),
            _ => Ok(()),
        }
    }

    fn upload_media(&mut self, media: &Media) -> Result<String, XError> {
        let total_bytes = media.total_bytes()?;
        if total_bytes == 0 {
            return Err(XError::EmptyMedia);
        }
        // INIT: 202 with a JSON body carrying the media id.
        let init = self.send_form(
            &self.endpoints.media_upload(),
            vec![
                ("command".into(), "INIT".into()),
                ("media_type".into(), media.media_type.clone()),
                ("total_bytes".into(), total_bytes.to_string()),
            ],
        )?;
        let media_id = media_id_from(&init)?;

        let segments = total_bytes.div_ceil(MEDIA_CHUNK_BYTES as u64) as usize;
        for index in 0..segments {
            let bytes = media.read_segment(index)?;
            let multipart = Multipart {
                fields: vec![
                    ("command".into(), "APPEND".into()),
                    ("media_id".into(), media_id.clone()),
                    ("segment_index".into(), index.to_string()),
                ],
                file_field: "media".into(),
                file_name: media.upload_name().into(),
                content_type: media.media_type.clone(),
                bytes,
            };
            let response = self.send(
                Request::new(Method::Post, self.endpoints.media_upload())
                    .header("Content-Type", multipart_content_type())
                    .body(Body::Multipart(multipart)),
                &[],
            )?;
            // APPEND is the one command that answers 204 No Content. Its body is
            // not part of the contract, so a zero-length body is success.
            self.expect_no_body(response)?;
        }

        // FINALIZE: 200 with a JSON body. It may describe media that is still
        // processing, so the body is read rather than assumed to be ready.
        let finalize = self.send_form(
            &self.endpoints.media_upload(),
            vec![
                ("command".into(), "FINALIZE".into()),
                ("media_id".into(), media_id.clone()),
            ],
        )?;
        self.await_media_ready(&media_id, &finalize)?;
        Ok(media_id)
    }

    fn create_post(&mut self, text: &str, media_id: Option<&str>) -> Result<String, XError> {
        let mut payload = json!({ "text": text });
        if let Some(media_id) = media_id {
            payload["media"] = json!({ "media_ids": [media_id] });
        }
        let bytes = serde_json::to_vec(&payload).map_err(|error| XError::Transport {
            detail: format!("could not serialise the post body: {error}"),
        })?;
        let response = self.send(
            Request::new(Method::Post, self.endpoints.tweets())
                .header("Content-Type", "application/json")
                .body(Body::Json(bytes)),
            &[],
        )?;
        let status = response.status;
        let response = self.expect_json(response)?;
        response
            .get("data")
            .and_then(|data| data.get("id"))
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| XError::Api {
                status,
                code: None,
                detail: format!("no post id in the response: {response}"),
            })
    }

    fn send_form(&mut self, url: &str, fields: Vec<(String, String)>) -> Result<Value, XError> {
        let response = self.send(
            Request::new(Method::Post, url)
                .header("Content-Type", "application/x-www-form-urlencoded")
                .body(Body::Form(fields.clone())),
            &fields,
        )?;
        self.expect_json(response)
    }

    /// `GET media/upload.json?command=STATUS&media_id=…`, signed like any other
    /// request (the query string is part of the OAuth signature base).
    fn media_status(&mut self, media_id: &str) -> Result<Value, XError> {
        let url = format!(
            "{}?command=STATUS&media_id={}",
            self.endpoints.media_upload(),
            percent_encode(media_id)
        );
        let response = self.send(Request::new(Method::Get, url), &[])?;
        self.expect_json(response)
    }

    /// Wait until X states the media is processed, or say it was not confirmed.
    ///
    /// Only an explicit `processing_info.state == "succeeded"` counts as ready.
    /// `pending`, `in_progress`, an unknown state and a *missing*
    /// `processing_info` all mean "the media is not known to be usable yet", so
    /// they are polled (up to [`MAX_PROCESSING_POLLS`]) and then reported as
    /// [`XError::MediaProcessingUnconfirmed`]. Posting a media id in any of those
    /// states would risk a post whose video never renders.
    fn await_media_ready(&mut self, media_id: &str, finalize: &Value) -> Result<(), XError> {
        let mut state = ProcessingState::read(finalize);
        let mut polls = 0usize;
        loop {
            match state {
                ProcessingState::Ready => return Ok(()),
                ProcessingState::Failed { detail } => {
                    return Err(XError::Api {
                        status: 200,
                        code: None,
                        detail: format!("X rejected the media during processing: {detail}"),
                    })
                }
                ProcessingState::Waiting {
                    state: last_state,
                    check_after_seconds,
                } => {
                    if polls >= MAX_PROCESSING_POLLS {
                        return Err(XError::MediaProcessingUnconfirmed {
                            media_id: media_id.to_string(),
                            state: last_state,
                            polls,
                        });
                    }
                    self.wait
                        .pause(check_after_seconds.unwrap_or(DEFAULT_PROCESSING_POLL_SECONDS));
                    polls += 1;
                    let status = self.media_status(media_id)?;
                    state = ProcessingState::read(&status);
                }
            }
        }
    }

    /// Sign and send. Form fields are part of the OAuth signature base; JSON and
    /// multipart bodies are not (RFC 5849 §3.4.1.3).
    fn send(
        &mut self,
        request: Request,
        signed_form: &[(String, String)],
    ) -> Result<Response, XError> {
        let authorization = self.signer.authorization_header(
            request.method,
            &request.url,
            signed_form,
            &fresh_nonce(),
            unix_timestamp(),
        );
        let request = request.header("Authorization", authorization);
        self.transport
            .send(&request)
            .map_err(|error| XError::Transport {
                detail: transport_detail(&error),
            })
    }

    /// Success for an endpoint whose body carries data this crate reads.
    ///
    /// An empty body here is a failure, not a shrug: `INIT` is where the media
    /// id comes from, and a truncated or intercepted response must not be
    /// mistaken for one.
    fn expect_json(&self, response: Response) -> Result<Value, XError> {
        let status = response.status;
        self.expect_success(response, SuccessShape::Json)?
            .ok_or_else(|| XError::Api {
                status,
                code: None,
                detail: "this endpoint's contract is JSON, but no document was returned"
                    .to_string(),
            })
    }

    /// Success for a command endpoint that answers with no body (`APPEND`, 204).
    fn expect_no_body(&self, response: Response) -> Result<(), XError> {
        self.expect_success(response, SuccessShape::NoBody)
            .map(|_| ())
    }

    fn expect_success(
        &self,
        response: Response,
        shape: SuccessShape,
    ) -> Result<Option<Value>, XError> {
        if !response.is_success() {
            return Err(from_response(&response));
        }
        match shape {
            SuccessShape::NoBody => Ok(None),
            SuccessShape::Json if response.body_is_empty() => Err(XError::Api {
                status: response.status,
                code: None,
                detail: format!(
                    "HTTP {} with an empty body, but this endpoint's contract is JSON \
                     (nothing in it was read, so this is not treated as success)",
                    response.status
                ),
            }),
            SuccessShape::Json => {
                serde_json::from_slice(&response.body)
                    .map(Some)
                    .map_err(|error| XError::Api {
                        status: response.status,
                        code: None,
                        detail: format!(
                            "unreadable response body ({error}): {}",
                            response.body_text()
                        ),
                    })
            }
        }
    }
}

/// What a `FINALIZE` or `STATUS` body says about media processing.
///
/// X documents four states (`pending`, `in_progress`, `succeeded`, `failed`).
/// Anything else — including no `processing_info` at all — is treated as "not
/// ready", never as ready.
#[derive(Debug, Clone, PartialEq, Eq)]
enum ProcessingState {
    /// `succeeded`: X says the media is processed and usable.
    Ready,
    /// `failed`: X rejected the media; uploading more will not help.
    Failed { detail: String },
    /// Not known to be usable yet: `pending`, `in_progress`, an unrecognised
    /// state, or no `processing_info` field.
    Waiting {
        state: String,
        check_after_seconds: Option<u64>,
    },
}

impl ProcessingState {
    fn read(body: &Value) -> Self {
        let Some(info) = body.get("processing_info") else {
            return Self::Waiting {
                state: "absent".to_string(),
                check_after_seconds: None,
            };
        };
        let state = info
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("absent");
        let check_after_seconds = info.get("check_after_secs").and_then(Value::as_u64);
        match state {
            "succeeded" => Self::Ready,
            "failed" => Self::Failed {
                detail: info.to_string(),
            },
            other => Self::Waiting {
                state: other.to_string(),
                check_after_seconds,
            },
        }
    }
}

fn media_id_from(value: &Value) -> Result<String, XError> {
    if let Some(id) = value.get("media_id_string").and_then(Value::as_str) {
        return Ok(id.to_string());
    }
    if let Some(id) = value.get("media_id").and_then(Value::as_u64) {
        return Ok(id.to_string());
    }
    Err(XError::Api {
        status: 200,
        code: None,
        detail: format!("no media id in the INIT response: {value}"),
    })
}

fn multipart_content_type() -> String {
    // The transport serialises the boundary; this crate only states the type so
    // a fake transport can assert it without inventing a boundary of its own.
    "multipart/form-data".to_string()
}

fn transport_detail(error: &TransportError) -> String {
    error.to_string()
}
