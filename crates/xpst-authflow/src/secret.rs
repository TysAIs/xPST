//! A string that refuses to print itself.
//!
//! Tokens, authorization codes, PKCE verifiers and app passwords all travel
//! through this type. [`std::fmt::Debug`] prints only a length, so a stray
//! `{:?}` in a log line cannot leak a credential. Serialization is explicit
//! and documented: [`SecretString`] serialises as its value, because durable
//! storage legitimately needs the bytes. The type therefore protects logs and
//! panics, not the on-disk format — that is the storage layer's job.

use std::fmt;

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use subtle::ConstantTimeEq;
use zeroize::Zeroize;

/// An owned secret string: redacted in `Debug`, zeroized on drop.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct SecretString(String);

impl SecretString {
    /// Wrap a value.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// The only way to read the secret. Named so call sites are auditable.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }

    /// True for the empty secret.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Debug for SecretString {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Length is not sensitive and is genuinely useful when debugging a
        // malformed credential. The bytes never appear.
        write!(
            formatter,
            "SecretString(<redacted, {} bytes>)",
            self.0.len()
        )
    }
}

impl Drop for SecretString {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

impl From<&str> for SecretString {
    fn from(value: &str) -> Self {
        Self::new(value)
    }
}

impl From<String> for SecretString {
    fn from(value: String) -> Self {
        Self::new(value)
    }
}

impl Serialize for SecretString {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for SecretString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer).map(Self)
    }
}

/// Compare two secrets without an early exit on the first differing byte.
///
/// Used for the CSRF `state` check. Length differences are still observable,
/// which is fine: both lengths are fixed by this crate's generator.
#[must_use]
pub fn constant_time_eq(left: &str, right: &str) -> bool {
    left.as_bytes().ct_eq(right.as_bytes()).into()
}

#[cfg(test)]
mod tests {
    use super::SecretString;

    #[test]
    fn debug_never_prints_the_value() {
        let secret = SecretString::new("fic-super-secret-token");
        let rendered = format!("{secret:?}");
        assert!(!rendered.contains("fic-super-secret-token"));
        assert!(rendered.contains("redacted"));
    }
}
