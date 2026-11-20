//! The OS keychain backend, behind a trait so the unavailable path is testable.
//!
//! `keyring` decides the real backend per target: Secret Service over D-Bus on
//! Linux, Keychain on macOS, Credential Locker on Windows. On Linux the Secret
//! Service is a *session* service; with no D-Bus session (SSH, cron, a slim
//! container) every call fails. That failure is returned as a string so
//! [`crate::storage::CredentialStore`] can decide between the encrypted file
//! fallback and an actionable refusal — it is never a panic.

/// Minimal keychain surface. Tests implement this to simulate a headless Linux
/// box without touching the real keychain (or prompting for an unlock).
pub trait KeychainBackend: Send + Sync {
    /// Cheap availability probe. `Ok(())` means the backend can serve reads and
    /// writes; `Err(detail)` is the platform's own complaint.
    fn probe(&self) -> Result<(), String>;
    /// Read one value, `None` when the entry does not exist.
    fn get(&self, key: &str) -> Result<Option<String>, String>;
    /// Create or replace one value.
    fn set(&self, key: &str, value: &str) -> Result<(), String>;
    /// Remove one value; absent is success.
    fn delete(&self, key: &str) -> Result<(), String>;
}

/// Account name used only to answer "is a Secret Service there?".
const PROBE_KEY: &str = "__xpst_keychain_probe__";

/// The real platform keychain.
#[derive(Debug, Clone)]
pub struct OsKeychain {
    service: String,
}

impl Default for OsKeychain {
    fn default() -> Self {
        Self {
            service: "xpst".to_string(),
        }
    }
}

impl OsKeychain {
    /// Use a different keychain service name (tests, embedders).
    #[must_use]
    pub fn new(service: impl Into<String>) -> Self {
        Self {
            service: service.into(),
        }
    }

    fn entry(&self, key: &str) -> Result<keyring::Entry, String> {
        keyring::Entry::new(&self.service, key).map_err(|error| error.to_string())
    }
}

impl KeychainBackend for OsKeychain {
    fn probe(&self) -> Result<(), String> {
        match self.entry(PROBE_KEY)?.get_password() {
            // A readable value or "no such entry" both prove the service is up.
            Ok(_) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(error.to_string()),
        }
    }

    fn get(&self, key: &str) -> Result<Option<String>, String> {
        match self.entry(key)?.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => Err(error.to_string()),
        }
    }

    fn set(&self, key: &str, value: &str) -> Result<(), String> {
        self.entry(key)?
            .set_password(value)
            .map_err(|error| error.to_string())
    }

    fn delete(&self, key: &str) -> Result<(), String> {
        match self.entry(key)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(error.to_string()),
        }
    }
}
