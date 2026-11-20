//! The real transport: XRPC over HTTPS, using the approved AT Protocol crate.
//!
//! `atrium-api` (MIT, MSRV 1.85 — below this workspace's floor) generates
//! the `com.atproto.*` request/response types and their error enums from the
//! published lexicons. This module supplies the only thing it does not: an
//! [`atrium_api::xrpc::XrpcClient`] over `reqwest` with rustls, so there is no
//! OpenSSL anywhere in the graph.
//!
//! What it adds on top of the generated types:
//!
//! * the `$type` discriminator on the post record (a record without it is not a
//!   post as far as the PDS is concerned);
//! * the real MIME type on `uploadBlob`, which the generated helper sends as
//!   `*/*`;
//! * status- and error-name-based classification, so `RecordNotFound` becomes
//!   [`TransportError::NotFound`] instead of a generic HTTP failure and a
//!   rejected password becomes `needs_reauth` instead of a retry loop.
//!
//! Nothing here is exercised by this crate's test suite — the crate's contract is
//! tested against the fake. This path is deliberately kept thin so that the code
//! a live smoke test would exercise is the code you can read in one screen.

use std::fmt;
use std::str::FromStr;

use async_trait::async_trait;
use atrium_api::app::bsky::embed::images;
use atrium_api::app::bsky::feed::post::{RecordData, RecordEmbedRefs};
use atrium_api::app::bsky::richtext::facet::{ByteSliceData, LinkData, MainFeaturesItem};
use atrium_api::client::AtpServiceClient;
use atrium_api::com::atproto::repo::{create_record, delete_record, get_record, upload_blob};
use atrium_api::com::atproto::server::create_session;
use atrium_api::types::string::{AtIdentifier, Did, Handle, Nsid, RecordKey};
use atrium_api::types::{BlobRef as AtBlobRef, Object, TypedBlobRef, Union, Unknown};
use atrium_api::xrpc::error::{Error as XrpcErrorType, XrpcError, XrpcErrorKind};
use atrium_api::xrpc::types::AuthorizationToken;
use atrium_api::xrpc::{
    http, HttpClient, InputDataOrBytes, OutputDataOrBytes, XrpcClient, XrpcRequest,
};
use xpst_provider::http::{
    redact_text, ClientConfig, HttpAdapter, HttpMethod, PreparedRequest, ReqwestAdapter,
};

use crate::capabilities::POST_COLLECTION;
use crate::transport::{
    AtUri, BlobRef, BlueskyTransport, CreatedRecord, PostDraft, PostRecordView, Session,
    TransportError,
};

/// The default service entryway.
pub const DEFAULT_SERVICE: &str = "https://bsky.social";

/// Normalise a configured service into `scheme://host` with no trailing slash.
///
/// A self-hosted PDS or any other entryway is expressed by configuring a
/// different host, not by a different code path.
pub fn normalize_service(service: &str) -> String {
    let trimmed = service.trim();
    if trimmed.is_empty() {
        return DEFAULT_SERVICE.to_owned();
    }
    let with_scheme = if trimmed.contains("://") {
        trimmed.to_owned()
    } else {
        format!("https://{trimmed}")
    };
    with_scheme.trim_end_matches('/').to_owned()
}

/// A real AT Protocol transport pointed at one service.
///
/// Generic over the adapter that performs the exchange. Production uses
/// [`ReqwestAdapter`]; the tests use a recording adapter, which is how the XRPC
/// request mapping (method, URL, `Authorization`, body) is proven without a
/// socket.
pub struct AtriumTransport<A: HttpAdapter = ReqwestAdapter> {
    service: String,
    adapter: A,
}

impl AtriumTransport<ReqwestAdapter> {
    /// A transport for `service` (e.g. `https://bsky.social` or `bsky.social`).
    ///
    /// Fails only when the TLS/HTTP stack cannot be initialised, which is a
    /// process-level problem rather than a request-level one.
    pub fn new(service: &str) -> Result<Self, TransportError> {
        Self::with_config(service, &ClientConfig::for_destination())
    }

    /// A transport for `service` with an adjusted client policy.
    pub fn with_config(service: &str, config: &ClientConfig) -> Result<Self, TransportError> {
        let adapter = ReqwestAdapter::new(config).map_err(|failure| TransportError::Network {
            detail: failure.to_string(),
        })?;
        Ok(Self::with_adapter(service, adapter))
    }
}

impl<A: HttpAdapter + Sync> AtriumTransport<A> {
    /// A transport for `service` over `adapter`.
    pub fn with_adapter(service: &str, adapter: A) -> Self {
        Self {
            service: normalize_service(service),
            adapter,
        }
    }

    /// The normalised service this transport talks to.
    pub fn service(&self) -> &str {
        &self.service
    }

    /// Borrow the adapter, so a test can read what was sent.
    pub fn adapter(&self) -> &A {
        &self.adapter
    }

    fn xrpc(&self, session: Option<&Session>) -> HttpXrpc<&A> {
        HttpXrpc {
            adapter: &self.adapter,
            base: self.service.clone(),
            token: session.map(|session| session.access_jwt.clone()),
        }
    }

    fn client(&self, session: Option<&Session>) -> AtpServiceClient<HttpXrpc<&A>> {
        AtpServiceClient::new(self.xrpc(session))
    }
}

/// One XRPC exchange, performed by an [`HttpAdapter`].
struct HttpXrpc<A> {
    adapter: A,
    base: String,
    /// The bearer token for this exchange, when the call needs authentication.
    ///
    /// Never rendered: this struct has no `Debug`, and the token is used only to
    /// build the request header.
    token: Option<String>,
}

impl<A: HttpAdapter + Send + Sync> HttpClient for HttpXrpc<A> {
    async fn send_http(
        &self,
        request: http::Request<Vec<u8>>,
    ) -> Result<http::Response<Vec<u8>>, Box<dyn std::error::Error + Send + Sync + 'static>> {
        let (parts, body) = request.into_parts();
        let method = match parts.method.as_str() {
            "GET" => HttpMethod::Get,
            "POST" => HttpMethod::Post,
            "PUT" => HttpMethod::Put,
            "DELETE" => HttpMethod::Delete,
            other => {
                return Err(Box::new(TransportError::Protocol {
                    detail: format!("XRPC sent an unsupported method {other}"),
                }))
            }
        };
        let mut prepared = PreparedRequest {
            method,
            url: parts.uri.to_string(),
            headers: Vec::new(),
            body,
        };
        for (name, value) in &parts.headers {
            prepared
                .headers
                .push((name.as_str().to_owned(), value_to_string(value)));
        }
        let answer = self.adapter.execute(prepared).await?;
        let mut out =
            http::Response::builder().status(http::StatusCode::from_u16(answer.status).map_err(
                |error| TransportError::Protocol {
                    detail: format!("the adapter returned an invalid status: {error}"),
                },
            )?);
        for (name, value) in &answer.headers {
            out = out.header(name.as_str(), value.as_str());
        }
        Ok(out.body(answer.body)?)
    }
}

/// Render a header value without assuming it is UTF-8.
fn value_to_string(value: &http::HeaderValue) -> String {
    String::from_utf8_lossy(value.as_bytes()).into_owned()
}

impl<A: HttpAdapter + Send + Sync> XrpcClient for HttpXrpc<A> {
    fn base_uri(&self) -> String {
        self.base.clone()
    }

    async fn authorization_token(&self, _is_refresh: bool) -> Option<AuthorizationToken> {
        self.token
            .as_ref()
            .map(|token| AuthorizationToken::Bearer(token.clone()))
    }
}

#[async_trait]
impl<A: HttpAdapter + Send + Sync> BlueskyTransport for AtriumTransport<A> {
    async fn create_session(
        &self,
        identifier: &str,
        app_password: &str,
    ) -> Result<Session, TransportError> {
        let input = create_session::InputData {
            identifier: identifier.to_owned(),
            password: app_password.to_owned(),
            allow_takendown: None,
            auth_factor_token: None,
        };
        let output = self
            .client(None)
            .service
            .com
            .atproto
            .server
            .create_session(input.into())
            .await
            .map_err(|error| classify(error, "createSession"))?;
        Ok(Session {
            did: output.did.as_str().to_owned(),
            handle: output.handle.as_str().to_owned(),
            access_jwt: output.access_jwt.clone(),
        })
    }

    async fn upload_blob(
        &self,
        session: &Session,
        mime: &str,
        bytes: &[u8],
    ) -> Result<BlobRef, TransportError> {
        // The generated helper hard-codes `Content-Type: */*`; the PDS gets the
        // real MIME type from here instead.
        let response = self
            .xrpc(Some(session))
            .send_xrpc::<(), Vec<u8>, upload_blob::Output, upload_blob::Error>(&XrpcRequest {
                method: http::Method::POST,
                nsid: upload_blob::NSID.into(),
                parameters: None,
                input: Some(InputDataOrBytes::Bytes(bytes.to_vec())),
                encoding: Some(mime.to_owned()),
            })
            .await
            .map_err(|error| classify(error, "uploadBlob"))?;
        match response {
            OutputDataOrBytes::Data(output) => blob_ref(output.blob.clone()),
            OutputDataOrBytes::Bytes(_) => Err(TransportError::Protocol {
                detail: "uploadBlob answered with a body that is not JSON".to_owned(),
            }),
        }
    }

    async fn create_post(
        &self,
        session: &Session,
        draft: &PostDraft,
    ) -> Result<CreatedRecord, TransportError> {
        // The key is supplied, never generated. A generated key hides which
        // record a lost response would have written, which is the difference
        // between a reconciliable outcome and a duplicate post.
        let rkey =
            RecordKey::new(draft.rkey.clone()).map_err(|error| TransportError::Protocol {
                detail: format!("invalid record key {:?}: {error}", draft.rkey),
            })?;
        let input = create_record::InputData {
            collection: nsid(POST_COLLECTION)?,
            record: record_unknown(draft)?,
            repo: identifier(&session.did)?,
            rkey: Some(rkey),
            swap_commit: None,
            validate: None,
        };
        let output = self
            .client(Some(session))
            .service
            .com
            .atproto
            .repo
            .create_record(input.into())
            .await
            .map_err(|error| classify(error, "createRecord"))?;
        Ok(CreatedRecord {
            uri: output.uri.clone(),
            cid: output.cid.as_ref().to_string(),
            rkey: output.uri.rsplit('/').next().unwrap_or_default().to_owned(),
        })
    }

    async fn get_post(
        &self,
        session: &Session,
        uri: &AtUri,
    ) -> Result<PostRecordView, TransportError> {
        let parameters = get_record::ParametersData {
            repo: identifier(&uri.repo)?,
            collection: nsid(&uri.collection)?,
            rkey: RecordKey::new(uri.rkey.clone()).map_err(|error| TransportError::Protocol {
                detail: format!("invalid record key {:?}: {error}", uri.rkey),
            })?,
            cid: None,
        };
        let output = self
            .client(Some(session))
            .service
            .com
            .atproto
            .repo
            .get_record(parameters.into())
            .await
            .map_err(|error| classify(error, "getRecord"))?;
        let record_type = serde_json::to_value(&output.value).ok().and_then(|value| {
            value
                .get("$type")
                .and_then(|value| value.as_str())
                .map(str::to_owned)
        });
        Ok(PostRecordView {
            uri: output.uri.clone(),
            cid: output.cid.as_ref().map(|cid| cid.as_ref().to_string()),
            record_type,
        })
    }

    async fn delete_post(&self, session: &Session, uri: &AtUri) -> Result<(), TransportError> {
        let input = delete_record::InputData {
            repo: identifier(&uri.repo)?,
            collection: nsid(&uri.collection)?,
            rkey: RecordKey::new(uri.rkey.clone()).map_err(|error| TransportError::Protocol {
                detail: format!("invalid record key {:?}: {error}", uri.rkey),
            })?,
            swap_commit: None,
            swap_record: None,
        };
        self.client(Some(session))
            .service
            .com
            .atproto
            .repo
            .delete_record(input.into())
            .await
            .map_err(|error| classify(error, "deleteRecord"))?;
        Ok(())
    }
}

/// The `app.bsky.feed.post` record, with its `$type`, as the value
/// `createRecord` needs.
fn record_unknown(draft: &PostDraft) -> Result<Unknown, TransportError> {
    let mut record = RecordData {
        created_at: atrium_api::types::string::Datetime::from_str(&draft.created_at).map_err(
            |error| TransportError::Protocol {
                detail: format!("invalid createdAt {:?}: {error}", draft.created_at),
            },
        )?,
        embed: None,
        entities: None,
        facets: None,
        labels: None,
        langs: None,
        reply: None,
        tags: None,
        text: draft.text.clone(),
    };
    if !draft.facets.is_empty() {
        record.facets = Some(
            draft
                .facets
                .iter()
                .map(|facet| {
                    Object::from(atrium_api::app::bsky::richtext::facet::MainData {
                        features: vec![Union::Refs(MainFeaturesItem::Link(Box::new(
                            Object::from(LinkData {
                                uri: facet.uri.clone(),
                            }),
                        )))],
                        index: Object::from(ByteSliceData {
                            byte_start: facet.byte_start,
                            byte_end: facet.byte_end,
                        }),
                    })
                })
                .collect(),
        );
    }
    if !draft.images.is_empty() {
        let mut items = Vec::with_capacity(draft.images.len());
        for attachment in &draft.images {
            let cid = atrium_api::types::string::Cid::from_str(&attachment.blob.cid).map_err(
                |error| TransportError::Protocol {
                    detail: format!(
                        "the upload returned a CID this client cannot re-use ({}): {error}",
                        attachment.blob.cid
                    ),
                },
            )?;
            items.push(Object::from(images::ImageData {
                alt: attachment.alt.clone(),
                aspect_ratio: None,
                image: AtBlobRef::Typed(TypedBlobRef::Blob(atrium_api::types::Blob {
                    r#ref: atrium_api::types::CidLink(*cid.as_ref()),
                    mime_type: attachment.blob.mime.clone(),
                    size: attachment.blob.size as usize,
                })),
            }));
        }
        record.embed = Some(Union::Refs(RecordEmbedRefs::AppBskyEmbedImagesMain(
            Box::new(Object::from(images::MainData { images: items })),
        )));
    }

    let mut value = serde_json::to_value(&record).map_err(|error| TransportError::Protocol {
        detail: format!("could not encode the post record: {error}"),
    })?;
    if let Some(map) = value.as_object_mut() {
        map.insert(
            "$type".to_owned(),
            serde_json::Value::String(POST_COLLECTION.to_owned()),
        );
    }
    serde_json::from_value(value).map_err(|error| TransportError::Protocol {
        detail: format!("could not build an AT Protocol record: {error}"),
    })
}

fn blob_ref(blob: AtBlobRef) -> Result<BlobRef, TransportError> {
    match blob {
        AtBlobRef::Typed(TypedBlobRef::Blob(blob)) => Ok(BlobRef {
            cid: blob.r#ref.0.to_string(),
            mime: blob.mime_type.clone(),
            size: blob.size as u64,
        }),
        AtBlobRef::Untyped(_) => Err(TransportError::Protocol {
            detail: "uploadBlob returned a legacy untyped blob reference".to_owned(),
        }),
    }
}

fn nsid(value: &str) -> Result<Nsid, TransportError> {
    Nsid::new(value.to_owned()).map_err(|error| TransportError::Protocol {
        detail: format!("invalid NSID {value:?}: {error}"),
    })
}

fn identifier(value: &str) -> Result<AtIdentifier, TransportError> {
    if value.starts_with("did:") {
        return Did::new(value.to_owned())
            .map(AtIdentifier::Did)
            .map_err(|error| TransportError::Protocol {
                detail: format!("invalid DID {value:?}: {error}"),
            });
    }
    Handle::new(value.to_owned())
        .map(AtIdentifier::Handle)
        .map_err(|error| TransportError::Protocol {
            detail: format!("invalid handle {value:?}: {error}"),
        })
}

/// Names the PDS uses when a credential, rather than a request, is the problem.
const AUTH_ERROR_NAMES: [&str; 5] = [
    "AuthenticationRequired",
    "InvalidIdentifier",
    "InvalidPassword",
    "InvalidToken",
    "ExpiredToken",
];

/// Turn an XRPC failure into the class a caller has to act on.
fn classify<E>(error: XrpcErrorType<E>, action: &str) -> TransportError
where
    E: fmt::Debug + fmt::Display,
{
    match error {
        XrpcErrorType::XrpcResponse(XrpcError { status, error }) => {
            let (name, message) = error_name(error);
            let detail = if name.is_empty() {
                format!("{action} answered HTTP {}", status.as_u16())
            } else {
                format!(
                    "{action} answered HTTP {} ({name}: {message})",
                    status.as_u16()
                )
            };
            // The PDS's own message is echoed into our report, so a URL it
            // happens to contain goes through the same redaction as ours.
            let detail = redact_text(&detail);
            let code = status.as_u16();
            if code == 401 || AUTH_ERROR_NAMES.contains(&name.as_str()) {
                return TransportError::Unauthorized { detail };
            }
            if code == 429 {
                return TransportError::RateLimited {
                    retry_after_s: None,
                    detail,
                };
            }
            if code == 400 && (name == "RecordNotFound" || name == "CouldNotFindRecord") {
                return TransportError::NotFound { detail };
            }
            if status.is_server_error() {
                return TransportError::Upstream {
                    status: code,
                    detail,
                };
            }
            TransportError::Rejected {
                status: code,
                detail,
            }
        }
        XrpcErrorType::Authentication(header) => TransportError::Unauthorized {
            detail: redact_text(&format!("{action} was not authorised: {header:?}")),
        },
        XrpcErrorType::HttpClient(source) => TransportError::Network {
            detail: redact_text(&format!("{action} could not be sent: {source}")),
        },
        other => TransportError::Network {
            detail: redact_text(&format!("{action} did not complete: {other}")),
        },
    }
}

fn error_name<E>(error: Option<XrpcErrorKind<E>>) -> (String, String)
where
    E: fmt::Display,
{
    match error {
        Some(XrpcErrorKind::Undefined(body)) => (
            body.error.clone().unwrap_or_default(),
            body.message.clone().unwrap_or_default(),
        ),
        Some(XrpcErrorKind::Custom(error)) => (error.to_string(), String::new()),
        None => (String::new(), String::new()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transport::{ImageAttachment, LinkFacet};

    fn draft() -> PostDraft {
        PostDraft {
            text: "hi https://example.com".to_owned(),
            created_at: "2026-11-17T12:00:00.000Z".to_owned(),
            images: vec![ImageAttachment {
                blob: BlobRef {
                    cid: crate::fake::FAKE_BLOB_CID.to_owned(),
                    mime: "image/png".to_owned(),
                    size: 42,
                },
                alt: "alt".to_owned(),
            }],
            facets: vec![LinkFacet {
                byte_start: 3,
                byte_end: 22,
                uri: "https://example.com".to_owned(),
            }],
            rkey: "3jzfcijpj2z2a".to_owned(),
        }
    }

    /// The post record is the one thing this adapter cannot get wrong: the PDS
    /// rejects a record that is not lexicon-shaped, and a record without
    /// `$type` is not a post at all. This asserts the encoded record, not a
    /// response.
    #[test]
    fn the_post_record_is_a_typed_bsky_post_with_facets_and_images() {
        let value = serde_json::to_value(record_unknown(&draft()).expect("record encodes"))
            .expect("record serialises");

        assert_eq!(value["$type"], serde_json::json!("app.bsky.feed.post"));
        assert_eq!(value["text"], serde_json::json!("hi https://example.com"));
        assert_eq!(
            value["createdAt"],
            serde_json::json!("2026-11-17T12:00:00.000Z")
        );
        assert_eq!(
            value["facets"][0]["features"][0]["$type"],
            serde_json::json!("app.bsky.richtext.facet#link")
        );
        assert_eq!(
            value["facets"][0]["features"][0]["uri"],
            serde_json::json!("https://example.com")
        );
        assert_eq!(
            value["facets"][0]["index"]["byteStart"],
            serde_json::json!(3)
        );
        assert_eq!(
            value["facets"][0]["index"]["byteEnd"],
            serde_json::json!(22)
        );
        assert_eq!(
            value["embed"]["$type"],
            serde_json::json!("app.bsky.embed.images")
        );
        assert_eq!(value["embed"]["images"][0]["alt"], serde_json::json!("alt"));
        assert_eq!(
            value["embed"]["images"][0]["image"]["$type"],
            serde_json::json!("blob")
        );
        assert_eq!(
            value["embed"]["images"][0]["image"]["mimeType"],
            serde_json::json!("image/png")
        );
        assert_eq!(
            value["embed"]["images"][0]["image"]["size"],
            serde_json::json!(42)
        );
    }

    #[test]
    fn a_text_only_record_has_no_embed() {
        let mut draft = draft();
        draft.images.clear();
        draft.facets.clear();

        let value = serde_json::to_value(record_unknown(&draft).expect("record encodes"))
            .expect("record serialises");

        assert!(value.get("embed").is_none());
        assert!(value.get("facets").is_none());
    }

    #[test]
    fn a_malformed_timestamp_is_a_protocol_error_not_a_missing_field() {
        let mut draft = draft();
        draft.created_at = "yesterday".to_owned();

        let error = record_unknown(&draft).expect_err("a bad createdAt must not be published");
        assert!(
            matches!(error, TransportError::Protocol { .. }),
            "{error:?}"
        );
    }

    #[test]
    fn dids_and_handles_are_told_apart() {
        assert!(matches!(
            identifier("did:plc:abc").expect("did"),
            AtIdentifier::Did(_)
        ));
        assert!(matches!(
            identifier("bot.example").expect("handle"),
            AtIdentifier::Handle(_)
        ));
        assert!(identifier("not a handle").is_err());
    }
}
