//! The credential this crate obtains.
//!
//! Shape is deliberately flat and serialisable: it is what the storage layer
//! writes. For redirect flows the token is obtained by exchanging an
//! [`crate::AuthorizationCode`]; for static-secret flows the pasted secret IS
//! the credential (a Bluesky app password / Mastodon personal access token).

use serde::{Deserialize, Serialize};

use crate::platform::{AuthMode, AuthPlatform};
use crate::secret::SecretString;

/// One acquired credential.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Credential {
    /// Which platform it belongs to.
    pub platform: AuthPlatform,
    /// Which auth mode produced it. Carried so a stored credential cannot be
    /// mistaken for a ban-safe one after the fact.
    pub auth_mode: AuthMode,
    /// Bearer token, app password, or personal access token.
    pub access_token: SecretString,
    /// Refresh token, when the provider issued one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refresh_token: Option<SecretString>,
    /// Non-secret account identity (handle, instance URL, user id).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub identifier: Option<String>,
    /// Scopes the provider granted.
    #[serde(default)]
    pub scopes: Vec<String>,
    /// Unix seconds when obtained, if the caller knows.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub obtained_at_unix: Option<u64>,
    /// Unix seconds when the access token expires, if known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at_unix: Option<u64>,
}

impl Credential {
    /// A credential from a pasted static secret.
    #[must_use]
    pub fn from_static_secret(
        platform: AuthPlatform,
        auth_mode: AuthMode,
        identifier: Option<String>,
        secret: SecretString,
    ) -> Self {
        Self {
            platform,
            auth_mode,
            access_token: secret,
            refresh_token: None,
            identifier,
            scopes: Vec::new(),
            obtained_at_unix: None,
            expires_at_unix: None,
        }
    }
}
