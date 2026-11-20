//! A credential value that cannot be printed, compared into output, or
//! serialised by accident.
//!
//! The smoke tool's hard rule is that no secret ever reaches stdout. Making
//! that a property of the type is stronger than remembering not to `{:?}` the
//! wrong field: [`Secret`] has no `Serialize` impl at all, and both `Debug`
//! and `Display` render a fixed redaction, so a future edit that formats a
//! request cannot leak the token it carries.

use std::fmt;

/// An opaque credential string.
///
/// Deliberately **not** `Serialize`: report types cannot embed a secret even if
/// someone writes `serde` derives on them later.
#[derive(Clone, PartialEq, Eq, Default)]
pub struct Secret(String);

impl Secret {
    /// Wrap a credential.
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// The credential itself. Every call site is a place that puts it on the
    /// wire, never in the output.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }

    /// Whether this holds nothing at all.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.trim().is_empty()
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(***)")
    }
}

impl fmt::Display for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("***")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formatting_a_secret_never_reveals_it() {
        let secret = Secret::new("token-value-that-must-not-print");
        assert_eq!(format!("{secret}"), "***");
        assert_eq!(format!("{secret:?}"), "Secret(***)");
        assert!(!format!("{secret:?}").contains("must-not-print"));
    }

    #[test]
    fn expose_returns_the_wire_value() {
        assert_eq!(Secret::new("abc").expose(), "abc");
        assert!(Secret::new("   ").is_empty());
        assert!(!Secret::new(" x ").is_empty());
    }
}
