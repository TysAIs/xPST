//! Per-device credentials for a phone reaching the engine off-loopback.
//!
//! Port of `src/xpst/dashboard/devices.py`. Storage is
//! `<config_dir>/devices.json`, plain JSON with only hashes in it: the code the
//! CLI prints is single-use and short-lived, the token a phone receives is
//! shown once, and the file cannot be replayed as a credential.
//!
//! The refusal codes and messages are the Python ones, pinned by
//! `src/parity/pairing_refusals.json` (captured by
//! `xtask/scripts/capture_python_routes.py`) so a client that branches on
//! `PAIR_CODE_EXPIRED` behaves the same on both surfaces.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::auth::sha256_hex;

/// File holding pairing codes and paired devices, relative to the config dir.
pub const DEVICES_FILENAME: &str = "devices.json";

/// Pairing code lifetime: long enough to walk to the phone, short enough that
/// a leaked code is dead before the next coffee.
pub const DEFAULT_PAIR_TTL_S: u64 = 600;

/// Wrong presentations tolerated inside one pairing window.
pub const MAX_CODE_ATTEMPTS: usize = 5;

/// Unambiguous alphabet: no 0/O, 1/I/L. Codes are read off a screen.
pub const CODE_ALPHABET: &[u8] = b"ABCDEFGHJKMNPQRSTUVWXYZ23456789";

/// Length of a pairing code, in alphabet symbols.
pub const CODE_LENGTH: usize = 8;

/// Bytes of entropy in a device token (~43 URL-safe characters).
pub const TOKEN_BYTES: usize = 32;

/// Do not touch disk on every authenticated request just to move `last_seen`.
pub const LAST_SEEN_MIN_INTERVAL_S: f64 = 60.0;

/// A typed pairing refusal: unknown/expired/spent code, or too many guesses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PairingError {
    pub code: &'static str,
    pub message: String,
    pub status_code: u16,
}

impl PairingError {
    fn new(code: &'static str, message: impl Into<String>, status_code: u16) -> Self {
        Self {
            code,
            message: message.into(),
            status_code,
        }
    }

    /// The refusal as the API returns it: `{ok: false, code, error, message}`.
    #[must_use]
    pub fn to_payload(&self) -> Value {
        json!({
            "ok": false,
            "code": self.code,
            "error": { "code": self.code, "message": self.message },
            "message": self.message,
        })
    }
}

/// What a successful redemption hands the phone. The token is shown once.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PairingGrant {
    pub device: Value,
    pub token: String,
}

/// The devices file for a config dir (not created).
#[must_use]
pub fn devices_path(config_dir: &Path) -> PathBuf {
    config_dir.join(DEVICES_FILENAME)
}

fn load(config_dir: &Path) -> Map<String, Value> {
    let mut data = match std::fs::read_to_string(devices_path(config_dir)) {
        Ok(text) => match serde_json::from_str::<Value>(&text) {
            Ok(Value::Object(map)) => map,
            // A corrupt file means "nothing paired yet"; pairing state is a
            // convenience and must not lock the operator out permanently.
            _ => Map::new(),
        },
        Err(_) => Map::new(),
    };
    data.entry("version".to_string()).or_insert(json!(1));
    for key in ["pairing_codes", "devices", "failed_attempts"] {
        if !data.get(key).is_some_and(Value::is_array) {
            data.insert(key.to_string(), json!([]));
        }
    }
    data
}

fn save(config_dir: &Path, data: &Map<String, Value>) -> std::io::Result<()> {
    std::fs::create_dir_all(config_dir)?;
    let path = devices_path(config_dir);
    let temporary = path.with_extension("json.tmp");
    // `dashboard/devices.py` writes `json.dumps(data, indent=2, sort_keys=True)`.
    let mut document = Value::Object(data.clone());
    xpst_core::json::sort_keys_recursive(&mut document);
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(&document)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?
    );
    std::fs::write(&temporary, text)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(&temporary, &path)?;
    Ok(())
}

/// Strip separators and case from a code a human typed off a screen.
#[must_use]
pub fn normalize_code(code: &str) -> String {
    code.chars()
        .filter(|ch| ch.is_alphanumeric())
        .map(|ch| ch.to_ascii_uppercase())
        .collect()
}

fn format_code(raw: &str) -> String {
    if raw.len() == CODE_LENGTH {
        format!("{}-{}", &raw[..4], &raw[4..])
    } else {
        raw.to_string()
    }
}

/// Current time, in the same seconds-since-epoch units Python stores.
#[must_use]
pub fn now_seconds() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

fn random_bytes(count: usize) -> Vec<u8> {
    let mut buffer = vec![0u8; count];
    // A failure here is a broken platform RNG; refuse rather than mint a
    // predictable token.
    getrandom::getrandom(&mut buffer).expect("operating-system randomness is available");
    buffer
}

fn random_symbols(count: usize) -> String {
    random_bytes(count)
        .into_iter()
        .map(|byte| CODE_ALPHABET[(byte as usize) % CODE_ALPHABET.len()] as char)
        .collect()
}

/// A URL-safe token: 256 bits, no padding.
#[must_use]
pub fn generate_token() -> String {
    crate::auth::base64_encode(&random_bytes(TOKEN_BYTES))
        .replace('+', "-")
        .replace('/', "_")
        .trim_end_matches('=')
        .to_string()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Create (and persist) a one-time pairing code.
///
/// Returns the code in cleartext **once** — the file stores only its hash — so
/// the caller is the only place it is ever displayed.
pub fn create_pairing_code(
    config_dir: &Path,
    label: &str,
    ttl_s: Option<u64>,
    code: Option<&str>,
    now: Option<f64>,
) -> Result<Value, PairingError> {
    let ttl = ttl_s.unwrap_or(DEFAULT_PAIR_TTL_S).max(30);
    let issued = now.unwrap_or_else(now_seconds);
    let raw = match code {
        Some(supplied) => normalize_code(supplied),
        None => random_symbols(CODE_LENGTH),
    };
    if raw.chars().count() != CODE_LENGTH {
        return Err(PairingError::new(
            "PAIR_CODE_SHAPE",
            format!("A pairing code must be {CODE_LENGTH} characters."),
            400,
        ));
    }
    let mut data = load(config_dir);
    // Drop expired and spent codes: leaving them forever makes "no active code"
    // ambiguous.
    if let Some(Value::Array(codes)) = data.get_mut("pairing_codes") {
        codes.retain(|item| {
            item.get("redeemed_at").is_none_or(Value::is_null)
                && item
                    .get("expires_at")
                    .and_then(Value::as_f64)
                    .is_some_and(|expires| expires > issued)
        });
    }
    let entry = json!({
        "code_sha256": sha256_hex(&raw),
        "label": label.chars().take(80).collect::<String>(),
        "created_at": issued,
        "expires_at": issued + ttl as f64,
        "attempts": 0,
        "redeemed_at": null,
        "device_id": null,
    });
    if let Some(Value::Array(codes)) = data.get_mut("pairing_codes") {
        codes.push(entry);
    }
    // Issuing a code proves a human is at the engine machine, which is what the
    // guess budget protects: a fresh code starts a fresh budget.
    data.insert("failed_attempts".to_string(), json!([]));
    save(config_dir, &data).map_err(io_error)?;
    Ok(json!({
        "code": format_code(&raw),
        "label": label,
        "created_at": issued,
        "expires_at": issued + ttl as f64,
        "expires_in_s": ttl,
    }))
}

fn io_error(error: std::io::Error) -> PairingError {
    PairingError::new(
        "PAIR_STORE_UNWRITABLE",
        format!("The device store could not be written: {error}"),
        500,
    )
}

/// Exchange a pairing code for a device token.
pub fn redeem_pairing_code(
    config_dir: &Path,
    code: &str,
    device_label: &str,
    client: &str,
    now: Option<f64>,
) -> Result<PairingGrant, PairingError> {
    let presented = normalize_code(code);
    let stamp = now.unwrap_or_else(now_seconds);
    let mut data = load(config_dir);

    // Failed presentations are counted over the code lifetime, so a client that
    // guesses cannot spin forever.
    let window_start = stamp - DEFAULT_PAIR_TTL_S as f64;
    let mut attempts: Vec<f64> = data
        .get("failed_attempts")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_f64).collect())
        .unwrap_or_default();
    attempts.retain(|item| *item >= window_start);
    if attempts.len() >= MAX_CODE_ATTEMPTS {
        data.insert("failed_attempts".to_string(), json!(attempts));
        save(config_dir, &data).map_err(io_error)?;
        return Err(PairingError::new(
            "PAIR_TOO_MANY_ATTEMPTS",
            "Too many wrong pairing codes. Run `xpst pair` on the engine machine for a fresh one.",
            429,
        ));
    }
    let digest = sha256_hex(&presented);
    let entry_index = data
        .get("pairing_codes")
        .and_then(Value::as_array)
        .and_then(|codes| {
            codes.iter().position(|candidate| {
                crate::auth::constant_time_eq(
                    candidate
                        .get("code_sha256")
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                    &digest,
                )
            })
        });
    let Some(index) = entry_index else {
        attempts.push(stamp);
        data.insert("failed_attempts".to_string(), json!(attempts));
        save(config_dir, &data).map_err(io_error)?;
        return Err(PairingError::new(
            "PAIR_CODE_UNKNOWN",
            "That pairing code is not valid. Run `xpst pair` on the engine machine for a fresh one.",
            401,
        ));
    };

    let entry = data["pairing_codes"][index].clone();
    let redeemed = entry
        .get("redeemed_at")
        .is_some_and(|value| !value.is_null());
    if redeemed {
        return Err(PairingError::new(
            "PAIR_CODE_SPENT",
            "That pairing code was already used. Run `xpst pair` again for a new one.",
            401,
        ));
    }
    let expires_at = entry
        .get("expires_at")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    if expires_at <= stamp {
        return Err(PairingError::new(
            "PAIR_CODE_EXPIRED",
            "That pairing code expired. Run `xpst pair` again for a fresh one.",
            401,
        ));
    }

    let token = generate_token();
    let device_id = format!("dev_{}", hex(&random_bytes(6)));
    let label = if device_label.is_empty() {
        entry
            .get("label")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or("phone")
            .to_string()
    } else {
        device_label.to_string()
    };
    let device = json!({
        "device_id": device_id,
        "label": label.chars().take(80).collect::<String>(),
        "token_sha256": sha256_hex(&token),
        "created_at": stamp,
        "last_seen_at": stamp,
        "client": client.chars().take(120).collect::<String>(),
    });
    if let Some(Value::Array(devices)) = data.get_mut("devices") {
        devices.push(device.clone());
    }
    if let Some(Value::Array(codes)) = data.get_mut("pairing_codes") {
        let entry = &mut codes[index];
        entry["redeemed_at"] = json!(stamp);
        entry["device_id"] = json!(device_id);
        let attempts_so_far = entry.get("attempts").and_then(Value::as_u64).unwrap_or(0);
        entry["attempts"] = json!(attempts_so_far + 1);
    }
    data.insert("failed_attempts".to_string(), json!([]));
    save(config_dir, &data).map_err(io_error)?;

    Ok(PairingGrant {
        device: public_device(&device),
        token,
    })
}

/// A device record with the token hash removed — the only safe shape.
fn public_device(device: &Value) -> Value {
    json!({
        "device_id": device.get("device_id").cloned().unwrap_or(Value::Null),
        "label": device.get("label").cloned().unwrap_or(Value::Null),
        "created_at": device.get("created_at").cloned().unwrap_or(Value::Null),
        "last_seen_at": device.get("last_seen_at").cloned().unwrap_or(Value::Null),
        "client": device.get("client").cloned().unwrap_or_else(|| json!("")),
        "revoked": device.get("revoked").and_then(Value::as_bool).unwrap_or(false),
    })
}

/// Paired devices, newest first, never their tokens.
#[must_use]
pub fn list_devices(config_dir: &Path) -> Vec<Value> {
    let data = load(config_dir);
    let mut devices: Vec<Value> = data
        .get("devices")
        .and_then(Value::as_array)
        .map(|items| items.iter().map(public_device).collect())
        .unwrap_or_default();
    devices.sort_by(|left, right| {
        let key = |value: &Value| {
            value
                .get("created_at")
                .and_then(Value::as_f64)
                .unwrap_or(0.0)
        };
        key(right)
            .partial_cmp(&key(left))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    devices
}

/// Revoke one device. `true` only when a live device was revoked.
pub fn revoke_device(
    config_dir: &Path,
    device_id: &str,
    now: Option<f64>,
) -> Result<bool, PairingError> {
    let stamp = now.unwrap_or_else(now_seconds);
    let mut data = load(config_dir);
    let Some(Value::Array(devices)) = data.get_mut("devices") else {
        return Ok(false);
    };
    for device in devices.iter_mut() {
        if device.get("device_id").and_then(Value::as_str) == Some(device_id) {
            if device.get("revoked").and_then(Value::as_bool) == Some(true) {
                return Ok(false);
            }
            device["revoked"] = json!(true);
            device["revoked_at"] = json!(stamp);
            save(config_dir, &data).map_err(io_error)?;
            return Ok(true);
        }
    }
    Ok(false)
}

/// The live device for a presented token, or `None`.
///
/// Comparison is constant-time over the stored hash. `last_seen_at` moves at
/// most once per [`LAST_SEEN_MIN_INTERVAL_S`], so authenticating does not
/// rewrite the file on every request.
#[must_use]
pub fn verify_device_token(config_dir: &Path, token: &str) -> Option<Value> {
    let presented = token.trim();
    if presented.is_empty() {
        return None;
    }
    let digest = sha256_hex(presented);
    let stamp = now_seconds();
    let mut data = load(config_dir);
    let mut matched: Option<usize> = None;
    if let Some(Value::Array(devices)) = data.get("devices") {
        for (index, device) in devices.iter().enumerate() {
            if device.get("revoked").and_then(Value::as_bool) == Some(true) {
                continue;
            }
            if crate::auth::constant_time_eq(
                device
                    .get("token_sha256")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
                &digest,
            ) {
                matched = Some(index);
                break;
            }
        }
    }
    let index = matched?;
    let last_seen = data["devices"][index]
        .get("last_seen_at")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    if stamp - last_seen >= LAST_SEEN_MIN_INTERVAL_S {
        let device = &mut data["devices"][index];
        device["last_seen_at"] = json!(stamp);
        // A last-seen write must never fail authentication.
        let _ = save(config_dir, &data);
    }
    Some(public_device(&data["devices"][index]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sandbox(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("xpst-api-devices-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_code_can_be_redeemed_once_and_only_once() {
        let dir = sandbox("redeem");
        let issued =
            create_pairing_code(&dir, "phone", Some(600), Some("ABCD2345"), Some(1_000.0)).unwrap();
        assert_eq!(issued["code"], "ABCD-2345");

        let grant = redeem_pairing_code(&dir, "abcd-2345", "", "test", Some(1_010.0)).unwrap();
        assert!(!grant.token.is_empty());
        assert_eq!(grant.device["label"], "phone");
        assert!(grant.device.get("token_sha256").is_none());

        let again = redeem_pairing_code(&dir, "ABCD2345", "", "test", Some(1_011.0));
        assert_eq!(again.unwrap_err().code, "PAIR_CODE_SPENT");
    }

    #[test]
    fn an_expired_code_is_expired_and_a_wrong_one_is_unknown() {
        let dir = sandbox("expiry");
        create_pairing_code(&dir, "", Some(600), Some("EXPIRE22"), Some(1_000.0)).unwrap();
        let expired = redeem_pairing_code(&dir, "EXPIRE22", "", "", Some(2_000.0)).unwrap_err();
        assert_eq!(expired.code, "PAIR_CODE_EXPIRED");
        assert_eq!(expired.status_code, 401);

        let unknown = redeem_pairing_code(&dir, "ZZZZZZZZ", "", "", Some(2_000.0)).unwrap_err();
        assert_eq!(unknown.code, "PAIR_CODE_UNKNOWN");
    }

    #[test]
    fn five_wrong_codes_spend_the_budget() {
        let dir = sandbox("budget");
        create_pairing_code(&dir, "", Some(600), Some("BUDGET22"), Some(1_000.0)).unwrap();
        for index in 0..MAX_CODE_ATTEMPTS {
            let error =
                redeem_pairing_code(&dir, &format!("WRONG{index:03}"), "", "", Some(1_001.0))
                    .unwrap_err();
            assert_eq!(error.code, "PAIR_CODE_UNKNOWN");
        }
        let blocked = redeem_pairing_code(&dir, "BUDGET22", "", "", Some(1_002.0)).unwrap_err();
        assert_eq!(blocked.code, "PAIR_TOO_MANY_ATTEMPTS");
        assert_eq!(blocked.status_code, 429);
    }

    #[test]
    fn a_paired_token_authenticates_until_it_is_revoked() {
        let dir = sandbox("revoke");
        create_pairing_code(&dir, "", Some(600), Some("TOKEN234"), Some(1_000.0)).unwrap();
        let grant = redeem_pairing_code(&dir, "TOKEN234", "", "", Some(1_001.0)).unwrap();
        assert!(verify_device_token(&dir, &grant.token).is_some());

        let device_id = grant.device["device_id"].as_str().unwrap().to_string();
        assert!(revoke_device(&dir, &device_id, Some(1_002.0)).unwrap());
        assert!(verify_device_token(&dir, &grant.token).is_none());
        assert!(!revoke_device(&dir, &device_id, Some(1_003.0)).unwrap());
    }

    #[test]
    fn a_corrupt_file_reads_as_nothing_paired() {
        let dir = sandbox("corrupt");
        std::fs::write(devices_path(&dir), "{not json").unwrap();
        assert!(list_devices(&dir).is_empty());
        assert!(verify_device_token(&dir, "anything").is_none());
    }

    #[test]
    fn a_short_code_is_refused_by_shape() {
        let dir = sandbox("shape");
        let error =
            create_pairing_code(&dir, "", Some(600), Some("ABC"), Some(1_000.0)).unwrap_err();
        assert_eq!(error.code, "PAIR_CODE_SHAPE");
    }
}
