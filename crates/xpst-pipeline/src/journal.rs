//! The durable half of a confirmed plan: one record per plan, on disk.
//!
//! A [`ConfirmedPlan`] in memory is a decision that dies with the process. That
//! is not enough for the promise the pipeline makes, because the promise is
//! about *re-running*: "a plan whose first run published two of three
//! destinations publishes only the third when it is run again". If the record
//! only exists in memory, then a restart — or a second process — re-publishes
//! everything, and the idempotency claim is true only inside one address space.
//!
//! This module closes that gap:
//!
//! * [`PlanJournal::confirm`] freezes a plan into `plans/<plan-id>.json` under
//!   the config directory, through [`xpst_state::JsonDocument`] — an atomic
//!   `fsync`+`rename` write under the same exclusive advisory lock the state
//!   file uses. Confirming the same plan twice returns the **stored** record, so
//!   a restart resumes instead of resetting.
//! * [`JournalSink`] is the real [`StateSink`] over that document: every settled
//!   destination and every attempt lease reaches the disk before the report is
//!   returned, so what is reported and what is stored cannot disagree.
//!
//! What is stored per destination is what a resume needs and nothing that could
//! be re-derived otherwise: the frozen intent (caption, content type, mode,
//! media), the standing outcome, the attempt count, and the lease with its
//! expiry. The delivery key is deliberately *not* stored — it is
//! `platform:content_hash`, so storing it would create a second source of truth
//! that could disagree with the record.
//!
//! A damaged record fails closed: it is reported and left alone, never replaced
//! with an empty one, because an empty record reads as "nothing has been
//! published yet" and that is an instruction to publish everything again.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};
use xpst_core::content::coerce_content_type;
use xpst_core::envelope::coerce_video_mode;

use xpst_state::{JsonDocument, StateError};

use crate::intent::{
    ConfirmedPlan, DeliveryKey, DestinationIntent, DestinationRecord, Lease, MediaRef, PlanId,
};
use crate::outcome::{
    DeferSource, DeferredOutcome, DestinationOutcome, DuplicateOutcome, FailedOutcome,
    FailureClass, PublishEvidence, PublishedOutcome, RefusalSource, RefusedOutcome,
    UnconfirmedOutcome,
};
use crate::pipeline::{StateSink, StateWriteError};

/// The record format this crate writes and reads.
pub const PLAN_RECORD_VERSION: u64 = 1;

/// The subdirectory plan records live in, under the config directory.
pub const PLANS_DIR_NAME: &str = "plans";

/// The code a destination is reported under when its record carried an attempt
/// lease that never produced an outcome.
pub const LEASE_OUTCOME_UNRECORDED_CODE: &str = "LEASE_OUTCOME_UNRECORDED";

/// Everything that can go wrong persisting or reloading a plan.
#[derive(Debug, thiserror::Error)]
pub enum JournalError {
    #[error("plan journal: {0}")]
    State(#[from] StateError),

    #[error("plan record is not usable: {message}")]
    Corrupt { message: String },

    #[error(
        "plan record is version {found}; this engine reads version {expected}. Refusing to \
         reinterpret it — an older or newer record cannot be replayed safely"
    )]
    UnsupportedVersion { found: u64, expected: u64 },

    #[error(
        "the record at {path} holds plan {found}, not {expected}; refusing to publish a plan the \
         caller did not ask for"
    )]
    PlanMismatch {
        path: String,
        expected: String,
        found: String,
    },

    #[error("the plan {plan_id} has no stored record, so there is nothing to update")]
    NotStored { plan_id: String },

    #[error("{value:?} is not a usable plan id")]
    InvalidPlanId { value: String },

    #[error("{field} is missing or invalid in the plan record: {detail}")]
    Field { field: String, detail: String },
}

/// The directory holding per-plan records.
#[derive(Debug, Clone)]
pub struct PlanJournal {
    dir: PathBuf,
}

impl PlanJournal {
    /// A journal rooted at the config directory: records live in
    /// `<dir>/plans/<plan-id>.json`.
    #[must_use]
    pub fn open(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// The root this journal was opened on.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// The file a plan's record is stored in.
    pub fn path_for(&self, plan_id: &str) -> Result<PathBuf, JournalError> {
        validate_plan_id(plan_id)?;
        Ok(self
            .dir
            .join(PLANS_DIR_NAME)
            .join(format!("{plan_id}.json")))
    }

    fn document(&self, plan_id: &str) -> Result<JsonDocument, JournalError> {
        Ok(JsonDocument::at(self.path_for(plan_id)?))
    }

    /// Persist a confirmed plan, or hand back the record that is already there.
    ///
    /// This is the call that makes confirmation durable. It is deliberately
    /// *not* "write and return": if the plan id already exists on disk the
    /// stored record wins, because that record is the truth about what has
    /// already been attempted. Re-confirming the same content and destinations
    /// therefore resumes; it cannot reset a half-published plan.
    pub fn confirm(
        &self,
        plan: &ConfirmedPlan,
        now_ms: i64,
    ) -> Result<ConfirmedPlan, JournalError> {
        let document = self.document(plan.plan_id())?;
        let stored = document.update(|current| match current {
            None => Ok::<_, JournalError>((encode_plan(plan, now_ms)?, None)),
            Some(text) => {
                // Fail closed: an unreadable record is reported, never replaced.
                let loaded = decode_plan(text)?;
                Ok((text.to_owned(), Some(loaded)))
            }
        })?;
        Ok(stored.unwrap_or_else(|| plan.clone()))
    }

    /// The stored record for `plan_id`, if there is one.
    pub fn load(&self, plan_id: &str) -> Result<Option<ConfirmedPlan>, JournalError> {
        let document = self.document(plan_id)?;
        let Some(text) = document.read()? else {
            return Ok(None);
        };
        Ok(Some(decode_plan(&text)?))
    }

    /// Replace one destination's standing record and bump the plan's timestamp.
    ///
    /// Read-modify-write runs inside the journal's exclusive lock, so two
    /// processes cannot apply this to the same stale snapshot: the second sees
    /// the first one's record.
    ///
    /// Only the one destination row is rewritten. Every other row keeps the
    /// bytes and the timestamp it already had, so "when did this destination
    /// last change" stays true for the rows this call did not touch.
    pub fn record_destination(
        &self,
        plan_id: &str,
        now_ms: i64,
        record: &DestinationRecord,
    ) -> Result<(), JournalError> {
        let document = self.document(plan_id)?;
        let platform = record.platform().to_owned();
        document.update(|current| {
            let Some(text) = current else {
                return Err(JournalError::NotStored {
                    plan_id: plan_id.to_owned(),
                });
            };
            // Fail closed on a document we cannot interpret, rather than
            // patching something we do not understand.
            decode_plan(text)?;
            let mut root: Value =
                serde_json::from_str(text).map_err(|error| JournalError::Corrupt {
                    message: error.to_string(),
                })?;
            let object = root.as_object_mut().ok_or_else(|| JournalError::Corrupt {
                message: "the record is not a JSON object".to_owned(),
            })?;
            let destinations = object
                .get_mut("destinations")
                .and_then(Value::as_array_mut)
                .ok_or_else(|| JournalError::Field {
                    field: "destinations".to_owned(),
                    detail: "missing or not an array".to_owned(),
                })?;
            let entry = destination_to_json(record, now_ms)?;
            let slot = destinations
                .iter_mut()
                .find(|item| {
                    item.get("platform").and_then(Value::as_str) == Some(platform.as_str())
                })
                .ok_or_else(|| JournalError::Field {
                    field: "destinations".to_owned(),
                    detail: format!("{platform} is not a destination of this plan"),
                })?;
            *slot = entry;
            object.insert("updated_at_ms".to_owned(), json!(now_ms));
            let serialised =
                serde_json::to_string(&root).map_err(|error| JournalError::Corrupt {
                    message: format!("could not serialise the plan record: {error}"),
                })?;
            Ok((serialised, ()))
        })?;
        Ok(())
    }

    /// When the record last changed, and when each destination last changed.
    ///
    /// Read from the document rather than from a typed model, so the answer is
    /// literally what is on disk.
    pub fn timestamps(&self, plan_id: &str) -> Result<Option<PlanTimestamps>, JournalError> {
        let document = self.document(plan_id)?;
        let Some(text) = document.read()? else {
            return Ok(None);
        };
        let root: Value = serde_json::from_str(&text).map_err(|error| JournalError::Corrupt {
            message: error.to_string(),
        })?;
        let object = root.as_object().ok_or_else(|| JournalError::Corrupt {
            message: "the record is not a JSON object".to_owned(),
        })?;
        let updated_at_ms = object
            .get("updated_at_ms")
            .and_then(Value::as_i64)
            .ok_or_else(|| JournalError::Field {
                field: "updated_at_ms".to_owned(),
                detail: "missing or not a number".to_owned(),
            })?;
        let mut per_destination = std::collections::BTreeMap::new();
        let rows = object.get("destinations").and_then(Value::as_array);
        for row in rows.into_iter().flatten() {
            let Some(platform) = row.get("platform").and_then(Value::as_str) else {
                continue;
            };
            let stamp = row
                .get("updated_at_ms")
                .and_then(Value::as_i64)
                .unwrap_or(updated_at_ms);
            per_destination.insert(platform.to_owned(), stamp);
        }
        Ok(Some(PlanTimestamps {
            updated_at_ms,
            per_destination,
        }))
    }
}

/// The timestamps a stored plan record carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanTimestamps {
    /// When the record was last written.
    pub updated_at_ms: i64,
    /// When each destination's row was last written.
    pub per_destination: std::collections::BTreeMap<String, i64>,
}

/// The pipeline's outcomes, written where they outlive the process.
///
/// Both halves matter: `record` is called after a destination settles, and
/// `claim` is called *before* the provider is called, so a crash mid-attempt
/// leaves the claim on disk instead of a destination that looks untouched. A
/// failed write is reported on the destination entry and never rewrites the
/// outcome — the provider already did what it did.
pub struct JournalSink {
    journal: PlanJournal,
}

impl JournalSink {
    #[must_use]
    pub fn new(journal: PlanJournal) -> Self {
        Self { journal }
    }

    /// The journal this sink writes through.
    #[must_use]
    pub fn journal(&self) -> &PlanJournal {
        &self.journal
    }
}

impl StateSink for JournalSink {
    fn record(
        &mut self,
        plan_id: &str,
        now_ms: i64,
        record: &DestinationRecord,
    ) -> Result<(), StateWriteError> {
        self.journal
            .record_destination(plan_id, now_ms, record)
            .map_err(|error| StateWriteError::new(error.to_string()))
    }

    fn claim(
        &mut self,
        plan_id: &str,
        now_ms: i64,
        record: &DestinationRecord,
    ) -> Result<(), StateWriteError> {
        self.journal
            .record_destination(plan_id, now_ms, record)
            .map_err(|error| StateWriteError::new(error.to_string()))
    }
}

/// Serialise a plan into its stored form.
pub fn encode_plan(plan: &ConfirmedPlan, now_ms: i64) -> Result<String, JournalError> {
    let destinations: Vec<Value> = plan
        .destinations()
        .iter()
        .map(|record| destination_to_json(record, now_ms))
        .collect::<Result<_, _>>()?;
    let document = json!({
        "version": PLAN_RECORD_VERSION,
        "plan_id": plan.plan_id(),
        "content_hash": plan.content_hash(),
        "updated_at_ms": now_ms,
        "destinations": destinations,
    });
    serde_json::to_string(&document).map_err(|error| JournalError::Corrupt {
        message: format!("could not serialise the plan record: {error}"),
    })
}

/// Parse a stored plan record.
pub fn decode_plan(text: &str) -> Result<ConfirmedPlan, JournalError> {
    let root: Value = serde_json::from_str(text).map_err(|error| JournalError::Corrupt {
        message: error.to_string(),
    })?;
    let object = root.as_object().ok_or_else(|| JournalError::Corrupt {
        message: "the record is not a JSON object".to_owned(),
    })?;
    let version = object
        .get("version")
        .and_then(Value::as_u64)
        .ok_or_else(|| JournalError::Field {
            field: "version".to_owned(),
            detail: "missing or not a number".to_owned(),
        })?;
    if version != PLAN_RECORD_VERSION {
        return Err(JournalError::UnsupportedVersion {
            found: version,
            expected: PLAN_RECORD_VERSION,
        });
    }
    let plan_id = text_field(object, "plan_id")?;
    let content_hash = text_field(object, "content_hash")?;
    let destinations = object
        .get("destinations")
        .and_then(Value::as_array)
        .ok_or_else(|| JournalError::Field {
            field: "destinations".to_owned(),
            detail: "missing or not an array".to_owned(),
        })?;
    let mut records = Vec::with_capacity(destinations.len());
    for value in destinations {
        records.push(destination_from_json(value, &content_hash)?);
    }
    // The id is also the file name: a record whose id is not a usable file name
    // cannot be the one this engine wrote.
    validate_plan_id(&plan_id)?;
    Ok(ConfirmedPlan::from_parts(
        PlanId::from_stored(plan_id),
        content_hash,
        records,
    ))
}

fn destination_to_json(record: &DestinationRecord, now_ms: i64) -> Result<Value, JournalError> {
    let mut object = Map::new();
    object.insert("platform".to_owned(), json!(record.platform()));
    object.insert("attempts".to_owned(), json!(record.attempts()));
    object.insert("updated_at_ms".to_owned(), json!(now_ms));
    object.insert(
        "outcome".to_owned(),
        match record.outcome() {
            None => Value::Null,
            Some(outcome) => outcome_to_json(outcome),
        },
    );
    object.insert(
        "lease".to_owned(),
        match record.lease() {
            None => Value::Null,
            Some(lease) => json!({
                "owner": lease.owner,
                "attempt_no": lease.attempt_no,
                "expires_at_ms": lease.expires_at_ms,
            }),
        },
    );
    object.insert(
        "intent".to_owned(),
        match record.intent() {
            None => Value::Null,
            Some(intent) => intent_to_json(intent),
        },
    );
    Ok(Value::Object(object))
}

fn intent_to_json(intent: &DestinationIntent) -> Value {
    let media: Vec<Value> = intent
        .media
        .iter()
        .map(|item| json!({"path": item.path, "content_hash": item.content_hash}))
        .collect();
    json!({
        "platform": intent.platform,
        "content_type": intent.content_type.as_str(),
        "mode": intent.mode.map(|mode| mode.as_str()),
        "caption": intent.caption,
        "media": media,
    })
}

fn outcome_to_json(outcome: &DestinationOutcome) -> Value {
    match outcome {
        DestinationOutcome::Published(published) => json!({
            "kind": "published",
            "post_id": published.post_id,
            "url": published.url,
            "evidence": published.evidence.as_str(),
        }),
        DestinationOutcome::SkippedDuplicate(duplicate) => json!({
            "kind": "skipped_duplicate",
            "existing_post_id": duplicate.existing_post_id,
        }),
        DestinationOutcome::Deferred(deferred) => json!({
            "kind": "deferred",
            "resume_after_ms": deferred.resume_after_ms,
            "source": deferred.source.as_str(),
            "detail": deferred.detail,
        }),
        DestinationOutcome::Refused(refused) => json!({
            "kind": "refused",
            "code": refused.code,
            "detail": refused.detail,
            "source": refused.source.as_str(),
            "retry_after_ms": refused.retry_after_ms,
        }),
        DestinationOutcome::Failed(failed) => json!({
            "kind": "failed",
            "class": failed.class.as_str(),
            "code": failed.code,
            "detail": failed.detail,
            "attempts": failed.attempts,
        }),
        DestinationOutcome::Unconfirmed(unconfirmed) => json!({
            "kind": "unconfirmed",
            "code": unconfirmed.code,
            "detail": unconfirmed.detail,
            "attempt": unconfirmed.attempt,
        }),
    }
}

fn destination_from_json(
    value: &Value,
    content_hash: &str,
) -> Result<DestinationRecord, JournalError> {
    let object = value.as_object().ok_or_else(|| JournalError::Corrupt {
        message: format!("a destination record is not an object: {value}"),
    })?;
    let platform = text_field(object, "platform")?;
    let attempts = object
        .get("attempts")
        .and_then(Value::as_u64)
        .map_or(0, |value| u32::try_from(value).unwrap_or(u32::MAX));
    let lease = match object.get("lease") {
        None | Some(Value::Null) => None,
        Some(lease) => {
            let lease = lease.as_object().ok_or_else(|| JournalError::Field {
                field: format!("{platform}.lease"),
                detail: "not an object".to_owned(),
            })?;
            Some(Lease {
                owner: text_field(lease, "owner")?,
                attempt_no: lease
                    .get("attempt_no")
                    .and_then(Value::as_u64)
                    .map_or(0, |value| u32::try_from(value).unwrap_or(u32::MAX)),
                expires_at_ms: lease
                    .get("expires_at_ms")
                    .and_then(Value::as_i64)
                    .unwrap_or(i64::MAX),
            })
        }
    };
    let recorded_outcome = match object.get("outcome") {
        None | Some(Value::Null) => None,
        Some(outcome) => Some(outcome_from_json(outcome)?),
    };
    // A stored lease means an attempt was claimed. If no outcome was ever
    // recorded for it, the attempt may have landed: the process that claimed it
    // died mid-call. Saying "pending" there would let the next run publish the
    // destination again, so the record is read as `Unconfirmed` and the next
    // owner reconciles before it retries.
    let outcome = match (&lease, recorded_outcome) {
        (Some(lease), None) => Some(DestinationOutcome::Unconfirmed(UnconfirmedOutcome {
            code: LEASE_OUTCOME_UNRECORDED_CODE.to_owned(),
            detail: format!(
                "{} claimed attempt {} and never recorded an outcome; that attempt may have \
                 published, so read it back before retrying",
                lease.owner, lease.attempt_no
            ),
            attempt: lease.attempt_no,
        })),
        (_, recorded) => recorded,
    };
    let intent = match object.get("intent") {
        None | Some(Value::Null) => None,
        Some(intent) => Some(intent_from_json(intent, &platform, content_hash)?),
    };
    Ok(DestinationRecord::from_parts(
        platform, intent, outcome, attempts, lease,
    ))
}

fn intent_from_json(
    value: &Value,
    platform: &str,
    content_hash: &str,
) -> Result<DestinationIntent, JournalError> {
    let object = value.as_object().ok_or_else(|| JournalError::Field {
        field: format!("{platform}.intent"),
        detail: "not an object".to_owned(),
    })?;
    let raw_type = text_field(object, "content_type")?;
    let content_type = coerce_content_type(&raw_type).map_err(|error| JournalError::Field {
        field: format!("{platform}.intent.content_type"),
        detail: error.to_string(),
    })?;
    let mode = match object.get("mode").and_then(Value::as_str) {
        None => None,
        Some(raw) => Some(coerce_video_mode(raw).map_err(|error| JournalError::Field {
            field: format!("{platform}.intent.mode"),
            detail: error.to_string(),
        })?),
    };
    let media = object
        .get("media")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|item| MediaRef {
                    path: item
                        .get("path")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                    content_hash: item
                        .get("content_hash")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                })
                .collect()
        })
        .unwrap_or_default();
    Ok(DestinationIntent {
        platform: platform.to_owned(),
        content_type,
        mode,
        caption: text_field(object, "caption")?,
        media,
        // Derived, never stored: one source of truth.
        key: DeliveryKey::new(platform, content_hash),
    })
}

fn outcome_from_json(value: &Value) -> Result<DestinationOutcome, JournalError> {
    let object = value.as_object().ok_or_else(|| JournalError::Corrupt {
        message: format!("an outcome is not an object: {value}"),
    })?;
    let kind = text_field(object, "kind")?;
    let text = |field: &str| -> Result<String, JournalError> {
        Ok(object
            .get(field)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned())
    };
    match kind.as_str() {
        "published" => Ok(DestinationOutcome::Published(PublishedOutcome {
            post_id: text("post_id")?,
            url: object.get("url").and_then(Value::as_str).map(str::to_owned),
            evidence: parse_evidence(&text("evidence")?)?,
        })),
        "skipped_duplicate" => Ok(DestinationOutcome::SkippedDuplicate(DuplicateOutcome {
            existing_post_id: object
                .get("existing_post_id")
                .and_then(Value::as_str)
                .map(str::to_owned),
        })),
        "deferred" => Ok(DestinationOutcome::Deferred(DeferredOutcome {
            resume_after_ms: object
                .get("resume_after_ms")
                .and_then(Value::as_i64)
                .unwrap_or(0),
            source: parse_defer_source(&text("source")?)?,
            detail: text("detail")?,
        })),
        "refused" => Ok(DestinationOutcome::Refused(RefusedOutcome {
            code: text("code")?,
            detail: text("detail")?,
            source: parse_refusal_source(&text("source")?)?,
            retry_after_ms: object.get("retry_after_ms").and_then(Value::as_i64),
        })),
        "failed" => Ok(DestinationOutcome::Failed(FailedOutcome {
            class: parse_failure_class(&text("class")?)?,
            code: text("code")?,
            detail: text("detail")?,
            attempts: object
                .get("attempts")
                .and_then(Value::as_u64)
                .map_or(0, |value| u32::try_from(value).unwrap_or(u32::MAX)),
        })),
        "unconfirmed" => Ok(DestinationOutcome::Unconfirmed(UnconfirmedOutcome {
            code: text("code")?,
            detail: text("detail")?,
            attempt: object
                .get("attempt")
                .and_then(Value::as_u64)
                .map_or(0, |value| u32::try_from(value).unwrap_or(u32::MAX)),
        })),
        other => Err(JournalError::Corrupt {
            message: format!("unknown outcome kind {other:?}"),
        }),
    }
}

fn parse_evidence(raw: &str) -> Result<PublishEvidence, JournalError> {
    match raw {
        "provider" => Ok(PublishEvidence::Provider),
        "readback" => Ok(PublishEvidence::Readback),
        other => Err(JournalError::Corrupt {
            message: format!("unknown publish evidence {other:?}"),
        }),
    }
}

fn parse_defer_source(raw: &str) -> Result<DeferSource, JournalError> {
    match raw {
        "pacing" => Ok(DeferSource::Pacing),
        "provider" => Ok(DeferSource::Provider),
        "rate_limit" => Ok(DeferSource::RateLimit),
        other => Err(JournalError::Corrupt {
            message: format!("unknown defer source {other:?}"),
        }),
    }
}

fn parse_refusal_source(raw: &str) -> Result<RefusalSource, JournalError> {
    match raw {
        "plan" => Ok(RefusalSource::Plan),
        "limits" => Ok(RefusalSource::Limits),
        "provider" => Ok(RefusalSource::Provider),
        other => Err(JournalError::Corrupt {
            message: format!("unknown refusal source {other:?}"),
        }),
    }
}

fn parse_failure_class(raw: &str) -> Result<FailureClass, JournalError> {
    match raw {
        "retryable" => Ok(FailureClass::Retryable),
        "permanent" => Ok(FailureClass::Permanent),
        other => Err(JournalError::Corrupt {
            message: format!("unknown failure class {other:?}"),
        }),
    }
}

fn text_field(object: &Map<String, Value>, field: &str) -> Result<String, JournalError> {
    object
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| JournalError::Field {
            field: field.to_owned(),
            detail: "missing or not a string".to_owned(),
        })
}

/// A plan id becomes a file name, so it is validated rather than trusted.
fn validate_plan_id(plan_id: &str) -> Result<(), JournalError> {
    let usable = !plan_id.is_empty()
        && plan_id.len() <= 96
        && plan_id
            .chars()
            .all(|item| item.is_ascii_alphanumeric() || matches!(item, '-' | '_'))
        && plan_id != "."
        && plan_id != "..";
    if usable {
        Ok(())
    } else {
        Err(JournalError::InvalidPlanId {
            value: plan_id.to_owned(),
        })
    }
}
