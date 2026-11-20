//! The five platforms this smoke covers, the request each one's identity
//! endpoint needs, and how to read an account label out of its reply.
//!
//! Every endpoint here is a **read**: it reports who the credential belongs to
//! and changes nothing on the account.
//!
//! | platform | request | cost |
//! |---|---|---|
//! | Bluesky | `GET /xrpc/com.atproto.server.getSession` with a cached session, or `POST /xrpc/com.atproto.server.createSession` with an app password | free |
//! | Mastodon | `GET /api/v1/accounts/verify_credentials` | free |
//! | X | `GET /2/users/me` | free |
//! | YouTube | `GET /youtube/v3/channels?part=snippet&mine=true` | 1 quota unit |
//! | Instagram | `GET /{ig-id}?fields=id,username` | free |
//!
//! The Bluesky `createSession` call is the one POST: an app password is not a
//! bearer token, so it cannot be presented in a header. The call creates a
//! session and no post, record or account setting changes.
//!
//! Labels are the account handle or channel title — the thing a human needs in
//! order to recognise which account answered. Never an account id, never a
//! token.

use serde_json::Value;

use crate::secret::Secret;

/// A platform the smoke can probe.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Platform {
    /// AT Protocol (Bluesky).
    Bluesky,
    /// Mastodon.
    Mastodon,
    /// X (Twitter).
    X,
    /// YouTube Data API v3.
    YouTube,
    /// Instagram Graph API.
    Instagram,
}

impl Platform {
    /// Every platform the report covers, in output order.
    pub const ALL: [Self; 5] = [
        Self::Bluesky,
        Self::Mastodon,
        Self::X,
        Self::YouTube,
        Self::Instagram,
    ];

    /// The stable machine name.
    #[must_use]
    pub fn slug(self) -> &'static str {
        match self {
            Self::Bluesky => "bluesky",
            Self::Mastodon => "mastodon",
            Self::X => "x",
            Self::YouTube => "youtube",
            Self::Instagram => "instagram",
        }
    }

    /// Parse a user-supplied platform name (`--platform`).
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        let key = name.trim().to_ascii_lowercase();
        Self::ALL.into_iter().find(|p| p.slug() == key)
    }

    /// Read the account label out of a successful identity reply.
    ///
    /// Returns `None` when the payload does not carry one — a 2xx with no
    /// label is still an authenticated credential, it just cannot be named.
    #[must_use]
    pub fn label_from_body(self, body: &str) -> Option<String> {
        let json: Value = serde_json::from_str(body).ok()?;
        let label = match self {
            // {"handle": "name.bsky.social", "did": "did:plc:..."}
            Self::Bluesky => json.get("handle").and_then(Value::as_str),
            // {"acct": "name", "username": "name", ...}
            Self::Mastodon => json.get("acct").and_then(Value::as_str),
            // {"data": {"username": "name", ...}}
            Self::X => json
                .get("data")
                .and_then(|d| d.get("username"))
                .and_then(Value::as_str),
            // {"items": [{"id": "...", "snippet": {"title": "..."}}]}
            Self::YouTube => json
                .get("items")
                .and_then(|i| i.get(0))
                .and_then(|i| i.get("snippet"))
                .and_then(|s| s.get("title"))
                .and_then(Value::as_str),
            // {"id": "...", "username": "name"}
            Self::Instagram => json.get("username").and_then(Value::as_str),
        }?;
        let trimmed = label.trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed.to_owned())
        }
    }
}

/// The HTTP method a request uses.
///
/// `Get` is the identity call for every provider that takes a bearer token.
/// `Post` exists for exactly one shape: an AT Protocol app password is not a
/// bearer token, so the only way to ask Bluesky "does this credential work" is
/// the documented `createSession` call, which takes the handle and password in
/// a JSON body. It creates a session and publishes nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    /// A read with the credential in an `Authorization: Bearer` header.
    Get,
    /// A JSON POST; the body is a [`Secret`] because it carries the credential.
    Post,
}

/// One identity request.
pub struct Request {
    /// Which platform this request is for.
    pub platform: Platform,
    /// How it is sent.
    pub method: Method,
    /// The absolute endpoint URL. Never printed: a Graph URL can carry an
    /// account id in its path.
    pub url: String,
    /// The credential, sent as `Authorization: Bearer <token>`.
    pub bearer: Option<Secret>,
    /// The JSON body, when the endpoint takes one. Holds a credential for
    /// `createSession`, so it is a [`Secret`] and never printed.
    pub body: Option<Secret>,
}

impl std::fmt::Debug for Request {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Request")
            .field("platform", &self.platform)
            .field("method", &self.method)
            .field("url", &self.url)
            .field("bearer", &self.bearer)
            .field("body", &self.body)
            .finish()
    }
}

impl Request {
    /// A GET with a bearer credential.
    #[must_use]
    pub fn bearer(platform: Platform, url: impl Into<String>, token: Secret) -> Self {
        Self {
            platform,
            method: Method::Get,
            url: url.into(),
            bearer: Some(token),
            body: None,
        }
    }

    /// A POST with a JSON body that carries the credential.
    #[must_use]
    pub fn json_post(platform: Platform, url: impl Into<String>, body: Secret) -> Self {
        Self {
            platform,
            method: Method::Post,
            url: url.into(),
            bearer: None,
            body: Some(body),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs_round_trip() {
        for platform in Platform::ALL {
            assert_eq!(Platform::parse(platform.slug()), Some(platform));
        }
        assert_eq!(Platform::parse("Twitter"), None);
        assert_eq!(Platform::parse(" YouTube "), Some(Platform::YouTube));
    }

    #[test]
    fn labels_come_from_the_documented_fields() {
        assert_eq!(
            Platform::Bluesky.label_from_body(r#"{"handle":"n.bsky.social","did":"did:plc:x"}"#),
            Some("n.bsky.social".to_owned())
        );
        assert_eq!(
            Platform::Mastodon.label_from_body(r#"{"acct":"n","username":"n"}"#),
            Some("n".to_owned())
        );
        assert_eq!(
            Platform::X.label_from_body(r#"{"data":{"id":"1","username":"n"}}"#),
            Some("n".to_owned())
        );
        assert_eq!(
            Platform::YouTube
                .label_from_body(r#"{"items":[{"id":"UC1","snippet":{"title":"Channel"}}]}"#),
            Some("Channel".to_owned())
        );
        assert_eq!(
            Platform::Instagram.label_from_body(r#"{"id":"1","username":"n"}"#),
            Some("n".to_owned())
        );
    }

    #[test]
    fn a_reply_without_a_label_yields_none_not_a_guess() {
        // An empty channel list is a real YouTube answer: the credential works,
        // the account has no channel. That is not a label.
        assert_eq!(Platform::YouTube.label_from_body(r#"{"items":[]}"#), None);
        assert_eq!(Platform::Bluesky.label_from_body("{}"), None);
        assert_eq!(Platform::Instagram.label_from_body("not json"), None);
        assert_eq!(Platform::X.label_from_body(r#"{"data":{}}"#), None);
    }

    #[test]
    fn request_debug_redacts_the_credential() {
        let request = Request::bearer(
            Platform::X,
            "https://api.x.com/2/users/me",
            Secret::new("super-secret-token"),
        );
        assert!(!format!("{request:?}").contains("super-secret-token"));
    }
}
