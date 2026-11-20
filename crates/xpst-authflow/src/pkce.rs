//! PKCE (RFC 7636) and the OAuth `state` parameter.
//!
//! The S256 transform is `BASE64URL(SHA256(ASCII(code_verifier)))`. Both the
//! verifier and the state are generated from the operating system's randomness
//! and never from a clock or a counter. `Pkce::from_verifier` accepts a
//! caller-supplied verifier only after RFC 7636 validation, which is what makes
//! the RFC's test vector expressible as a test.

use sha2::{Digest, Sha256};

use crate::encoding::b64url_encode;
use crate::error::AuthFlowError;
use crate::secret::{constant_time_eq, SecretString};

/// Minimum verifier length, RFC 7636 §4.1.
pub const MIN_VERIFIER_LEN: usize = 43;
/// Maximum verifier length, RFC 7636 §4.1.
pub const MAX_VERIFIER_LEN: usize = 128;

/// Bytes of OS randomness behind a generated verifier/state.
const ENTROPY_BYTES: usize = 64;
/// State tokens need less material; 32 bytes is 256 bits.
const STATE_BYTES: usize = 32;

/// A PKCE verifier plus its S256 challenge.
pub struct Pkce {
    verifier: SecretString,
    challenge: String,
}

impl Pkce {
    /// Generate a fresh verifier and challenge.
    ///
    /// 64 random bytes encode to 86 base64url characters — inside RFC 7636's
    /// window and with 512 bits of entropy.
    pub fn generate() -> Result<Self, AuthFlowError> {
        let mut bytes = [0_u8; ENTROPY_BYTES];
        getrandom::getrandom(&mut bytes)
            .map_err(|error| AuthFlowError::Random(error.to_string()))?;
        Self::from_verifier(b64url_encode(&bytes))
    }

    /// Validate a supplied verifier and compute its S256 challenge.
    ///
    /// This is the constructor the RFC 7636 test vector uses.
    pub fn from_verifier(verifier: impl Into<String>) -> Result<Self, AuthFlowError> {
        let verifier = verifier.into();
        validate_verifier(&verifier)?;
        let challenge = s256_challenge(&verifier);
        Ok(Self {
            verifier: SecretString::new(verifier),
            challenge,
        })
    }

    /// The verifier. Sent only to the token endpoint.
    #[must_use]
    pub fn verifier(&self) -> &SecretString {
        &self.verifier
    }

    /// The S256 challenge. Sent on the authorize URL.
    #[must_use]
    pub fn challenge(&self) -> &str {
        &self.challenge
    }

    /// The only challenge method this crate emits.
    #[must_use]
    pub fn method(&self) -> &'static str {
        "S256"
    }
}

/// Compute `BASE64URL(SHA256(ASCII(verifier)))` per RFC 7636 §4.2.
#[must_use]
pub fn s256_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    b64url_encode(&digest)
}

/// Check RFC 7636 §4.1: length 43..=128 and only unreserved characters
/// (`ALPHA / DIGIT / "-" / "." / "_" / "~"`).
fn validate_verifier(verifier: &str) -> Result<(), AuthFlowError> {
    if !(MIN_VERIFIER_LEN..=MAX_VERIFIER_LEN).contains(&verifier.len()) {
        return Err(AuthFlowError::PkceVerifierLength {
            actual: verifier.len(),
        });
    }
    if let Some(bad) = verifier
        .chars()
        .find(|character| !is_unreserved(*character))
    {
        return Err(AuthFlowError::PkceVerifierCharset(bad));
    }
    Ok(())
}

fn is_unreserved(character: char) -> bool {
    character.is_ascii_alphanumeric() || matches!(character, '-' | '.' | '_' | '~')
}

/// The CSRF `state` value for one authorization attempt.
pub struct StateToken {
    value: SecretString,
}

impl StateToken {
    /// Generate 256 bits of OS randomness as a base64url string.
    pub fn generate() -> Result<Self, AuthFlowError> {
        let mut bytes = [0_u8; STATE_BYTES];
        getrandom::getrandom(&mut bytes)
            .map_err(|error| AuthFlowError::Random(error.to_string()))?;
        Ok(Self {
            value: SecretString::new(b64url_encode(&bytes)),
        })
    }

    /// The value to place on the authorize URL.
    #[must_use]
    pub fn expose(&self) -> &str {
        self.value.expose()
    }

    /// Compare the redirect's `state` against this flow's value.
    ///
    /// Constant-time on equal-length inputs. A missing value must be rejected
    /// by the caller before reaching here; an empty string simply mismatches.
    pub fn verify(&self, returned: &str) -> Result<(), AuthFlowError> {
        if constant_time_eq(self.value.expose(), returned) {
            Ok(())
        } else {
            Err(AuthFlowError::StateMismatch)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Pkce, StateToken};

    #[test]
    fn rfc7636_appendix_b_vector() {
        // RFC 7636 Appendix B, verbatim.
        let pkce = Pkce::from_verifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
            .expect("the RFC verifier is valid");
        assert_eq!(
            pkce.challenge(),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
        assert_eq!(pkce.method(), "S256");
    }

    #[test]
    fn state_mismatch_is_rejected() {
        let state = StateToken::generate().expect("os randomness");
        assert!(state.verify(state.expose()).is_ok());
        assert!(state.verify("not-the-state").is_err());
    }
}
