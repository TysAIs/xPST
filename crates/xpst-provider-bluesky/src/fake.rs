//! An in-memory [`BlueskyTransport`] for tests.
//!
//! It records every call it was asked to make and can be told to fail any single
//! operation with a specific [`TransportError`]. Posts it "writes" are stored, so
//! a test can publish, read back the same URI and then delete it and watch the
//! readback flip to `NotFound` — the whole lifecycle, no sockets.
//!
//! It ships in the crate rather than behind `#[cfg(test)]` because it is also
//! how a caller exercises its own integration against this adapter without
//! pointing anything at a live PDS.

use std::collections::BTreeMap;
use std::sync::Mutex;

use async_trait::async_trait;

use crate::transport::{
    AtUri, BlobRef, BlueskyTransport, CreatedRecord, PostDraft, PostRecordView, Session,
    TransportError,
};

/// The DID the fake logs in as.
pub const FAKE_DID: &str = "did:plc:xpstfakeaccount";
/// The handle the fake logs in as.
pub const FAKE_HANDLE: &str = "xpst-fake.example";

/// A real CIDv1 used for every blob the fake "uploads".
///
/// It has to be a genuine CID: the record builder in [`crate::atrium`] re-encodes
/// the reference the PDS returned, and a made-up string would fail that path
/// instead of exercising it.
pub const FAKE_BLOB_CID: &str = "bafkreibme22gw2h7y2h7tg2fhqotaqjucnbc24deqo72b6mkl2egezxhvy";

/// One recorded call, in the order it happened.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Call {
    /// `com.atproto.server.createSession`.
    CreateSession {
        /// The identifier that was sent.
        identifier: String,
    },
    /// `com.atproto.repo.uploadBlob`.
    UploadBlob {
        /// The MIME type that was sent.
        mime: String,
        /// How many bytes were sent.
        bytes: usize,
    },
    /// `com.atproto.repo.createRecord`.
    CreatePost {
        /// How many images the draft carried.
        images: usize,
        /// The record key the request named.
        rkey: String,
    },
    /// `com.atproto.repo.getRecord`.
    GetPost {
        /// The URI that was asked about.
        uri: String,
    },
    /// `com.atproto.repo.deleteRecord`.
    DeletePost {
        /// The URI that was asked about.
        uri: String,
    },
}

impl Call {
    /// The NSID this call stands for, so a test can talk about protocol calls
    /// rather than about enum variants.
    pub fn nsid(&self) -> &'static str {
        match self {
            Self::CreateSession { .. } => "com.atproto.server.createSession",
            Self::UploadBlob { .. } => "com.atproto.repo.uploadBlob",
            Self::CreatePost { .. } => "com.atproto.repo.createRecord",
            Self::GetPost { .. } => "com.atproto.repo.getRecord",
            Self::DeletePost { .. } => "com.atproto.repo.deleteRecord",
        }
    }
}

struct FakeState {
    fail_session: Option<TransportError>,
    fail_upload: Option<TransportError>,
    fail_create: Option<TransportError>,
    lose_create_response: Option<TransportError>,
    fail_get: Option<TransportError>,
    fail_delete: Option<TransportError>,
    calls: Vec<Call>,
    uploads: Vec<(String, usize)>,
    drafts: Vec<PostDraft>,
    posts: BTreeMap<String, PostRecordView>,
    duplicate_rkey_writes: usize,
}

impl FakeState {
    fn reset_errors(&mut self) {
        self.fail_session = None;
        self.fail_upload = None;
        self.fail_create = None;
        self.lose_create_response = None;
        self.fail_get = None;
        self.fail_delete = None;
    }
}

/// A CID-shaped string derived from the record's bytes.
///
/// Real record CIDs are content-addressed: `prepareCreate` computes
/// `cidForCbor(encode(record))`. The fake does the same thing with a cheap fold
/// over the record's fields, so a retry that changes `createdAt` gets a different
/// CID for the same key — which is what the PDS would hand back — while staying
/// byte-stable for identical input.
fn content_cid(draft: &PostDraft) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for part in std::iter::once(draft.text.as_str())
        .chain(std::iter::once(draft.created_at.as_str()))
        .chain(std::iter::once(draft.rkey.as_str()))
    {
        for byte in part.as_bytes() {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    format!("bafyfake{hash:016x}")
}

/// A transport that answers from memory.
pub struct FakeTransport {
    state: Mutex<FakeState>,
}

impl Default for FakeTransport {
    fn default() -> Self {
        Self::new()
    }
}

impl FakeTransport {
    fn state(&self) -> std::sync::MutexGuard<'_, FakeState> {
        self.state.lock().expect("fake state lock poisoned")
    }

    /// A fake whose every operation succeeds and which starts with no records.
    pub fn new() -> Self {
        Self {
            state: Mutex::new(FakeState {
                fail_session: None,
                fail_upload: None,
                fail_create: None,
                lose_create_response: None,
                fail_get: None,
                fail_delete: None,
                calls: Vec::new(),
                uploads: Vec::new(),
                drafts: Vec::new(),
                posts: BTreeMap::new(),
                duplicate_rkey_writes: 0,
            }),
        }
    }

    /// Make `createSession` fail with `error`.
    pub fn fail_session(&self, error: TransportError) -> &Self {
        self.state().fail_session = Some(error);
        self
    }

    /// Make `uploadBlob` fail with `error`.
    pub fn fail_upload(&self, error: TransportError) -> &Self {
        self.state().fail_upload = Some(error);
        self
    }

    /// Make `createRecord` fail with `error`, without writing anything.
    pub fn fail_create(&self, error: TransportError) -> &Self {
        self.state().fail_create = Some(error);
        self
    }

    /// Write the record, then fail with `error` — a request that landed and whose
    /// answer was lost.
    ///
    /// This is the case that cannot be told apart from [`Self::fail_create`] by a
    /// client, and the one that turns a retry into a second post unless the
    /// record key is deterministic.
    pub fn lose_create_response(&self, error: TransportError) -> &Self {
        self.state().lose_create_response = Some(error);
        self
    }

    /// Make `getRecord` fail with `error` for every URI.
    pub fn fail_get(&self, error: TransportError) -> &Self {
        self.state().fail_get = Some(error);
        self
    }

    /// Make `deleteRecord` fail with `error`.
    pub fn fail_delete(&self, error: TransportError) -> &Self {
        self.state().fail_delete = Some(error);
        self
    }

    /// Clear every canned failure.
    pub fn clear_failures(&self) -> &Self {
        self.state().reset_errors();
        self
    }

    /// Pretend the account already holds this record.
    pub fn seed_post(&self, uri: &str) -> &Self {
        let parsed = AtUri::parse(uri).expect("seeded URI must be an AT-URI");
        self.state().posts.insert(
            parsed.to_uri(),
            PostRecordView {
                uri: parsed.to_uri(),
                cid: Some("bafyreiseeded".to_owned()),
                record_type: Some(crate::capabilities::POST_COLLECTION.to_owned()),
            },
        );
        self
    }

    /// Every call made so far, oldest first.
    pub fn calls(&self) -> Vec<Call> {
        self.state().calls.clone()
    }

    /// The number of calls made so far.
    pub fn call_count(&self) -> usize {
        self.state().calls.len()
    }

    /// Every blob upload, as `(mime, byte length)`.
    pub fn uploads(&self) -> Vec<(String, usize)> {
        self.state().uploads.clone()
    }

    /// Every record the fake is asked to write.
    pub fn drafts(&self) -> Vec<PostDraft> {
        self.state().drafts.clone()
    }

    /// How many records the fake is holding.
    ///
    /// The number that matters for a duplicate-post check: two publishes of the
    /// same content must leave this at one.
    pub fn record_count(&self) -> usize {
        self.state().posts.len()
    }

    /// How many `createRecord` calls named a record key that already existed.
    ///
    /// Non-zero means the retry really did re-send, so [`Self::record_count`]
    /// staying at one is evidence that the write is idempotent rather than
    /// evidence that the retry never happened.
    pub fn duplicate_rkey_writes(&self) -> usize {
        self.state().duplicate_rkey_writes
    }

    /// Whether the fake is currently holding this record.
    pub fn has_post(&self, uri: &str) -> bool {
        match AtUri::parse(uri) {
            Some(parsed) => self
                .state
                .lock()
                .expect("fake state lock poisoned")
                .posts
                .contains_key(&parsed.to_uri()),
            None => false,
        }
    }
}

#[async_trait]
impl BlueskyTransport for FakeTransport {
    async fn create_session(
        &self,
        identifier: &str,
        _app_password: &str,
    ) -> Result<Session, TransportError> {
        let mut state = self.state();
        state.calls.push(Call::CreateSession {
            identifier: identifier.to_owned(),
        });
        match state.fail_session.clone() {
            Some(error) => Err(error),
            None => Ok(Session {
                did: FAKE_DID.to_owned(),
                handle: FAKE_HANDLE.to_owned(),
                access_jwt: "fake-access-jwt".to_owned(),
            }),
        }
    }

    async fn upload_blob(
        &self,
        _session: &Session,
        mime: &str,
        bytes: &[u8],
    ) -> Result<BlobRef, TransportError> {
        let mut state = self.state();
        state.calls.push(Call::UploadBlob {
            mime: mime.to_owned(),
            bytes: bytes.len(),
        });
        if let Some(error) = state.fail_upload.clone() {
            return Err(error);
        }
        state.uploads.push((mime.to_owned(), bytes.len()));
        Ok(BlobRef {
            cid: FAKE_BLOB_CID.to_owned(),
            mime: mime.to_owned(),
            size: bytes.len() as u64,
        })
    }

    async fn create_post(
        &self,
        session: &Session,
        draft: &PostDraft,
    ) -> Result<CreatedRecord, TransportError> {
        let mut state = self.state();
        state.calls.push(Call::CreatePost {
            images: draft.images.len(),
            rkey: draft.rkey.clone(),
        });
        if let Some(error) = state.fail_create.clone() {
            return Err(error);
        }

        // `com.atproto.repo.createRecord` writes at the key it is given. When the
        // key already exists the PDS does not append a second record and does not
        // reject the call: `prepareCreate` builds a Create write and
        // `processWrites` stores it at that MST path, replacing the previous
        // value. Because a record's CID is content-addressed, re-writing the same
        // content yields the same CID, so the retry is a no-op rewrite — one
        // record, not two. The fake reproduces that, and counts the rewrites so a
        // test can tell "the retry happened" from "the retry appended".
        let rkey = draft.rkey.clone();
        let uri = format!(
            "at://{}/{}/{rkey}",
            session.did,
            crate::capabilities::POST_COLLECTION
        );
        let cid = content_cid(draft);
        if state.posts.contains_key(&uri) {
            state.duplicate_rkey_writes += 1;
        }
        state.drafts.push(draft.clone());
        state.posts.insert(
            uri.clone(),
            PostRecordView {
                uri: uri.clone(),
                cid: Some(cid.clone()),
                record_type: Some(crate::capabilities::POST_COLLECTION.to_owned()),
            },
        );

        if let Some(error) = state.lose_create_response.clone() {
            // The record is committed; the client never hears about it.
            return Err(error);
        }

        Ok(CreatedRecord { uri, cid, rkey })
    }

    async fn get_post(
        &self,
        _session: &Session,
        uri: &AtUri,
    ) -> Result<PostRecordView, TransportError> {
        let mut state = self.state();
        state.calls.push(Call::GetPost { uri: uri.to_uri() });
        if let Some(error) = state.fail_get.clone() {
            return Err(error);
        }
        match state.posts.get(&uri.to_uri()) {
            Some(view) => Ok(view.clone()),
            None => Err(TransportError::NotFound {
                detail: format!("RecordNotFound: no record at {}", uri.to_uri()),
            }),
        }
    }

    async fn delete_post(&self, _session: &Session, uri: &AtUri) -> Result<(), TransportError> {
        let mut state = self.state();
        state.calls.push(Call::DeletePost { uri: uri.to_uri() });
        if let Some(error) = state.fail_delete.clone() {
            return Err(error);
        }
        match state.posts.remove(&uri.to_uri()) {
            Some(_) => Ok(()),
            None => Err(TransportError::NotFound {
                detail: format!("RecordNotFound: no record at {}", uri.to_uri()),
            }),
        }
    }
}
