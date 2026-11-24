//! `xpst devices` and `xpst pair` — per-device credentials for a phone.
//!
//! Parity targets: `src/xpst/cli.py::devices` / `pair` and
//! `src/xpst/dashboard/devices.py`. Pairing state lives in
//! `<config dir>/devices.json` (plain JSON, `0600`, sorted keys, indent 2) —
//! deliberately not the Fernet store, because the CLI writes a code in one
//! process and the engine redeems it in another, and a hashed token needs no
//! encryption.
//!
//! Two behaviours are load-bearing:
//!
//! * **A corrupt file means "nothing paired yet".** Pairing state is a
//!   convenience; an unreadable file must not lock the operator out.
//! * **The token hash never leaves the file.** `_public_device` drops
//!   `token_sha256`, and that is the only shape either command reports.
//!
//! **Divergence, recorded in the ledger note:** text mode renders plain lines
//! instead of Rich's markup (`--json` is the parity surface).

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use serde_json::{json, Map, Value};

use crate::commands::bootstrap;
use crate::output::{
    array, boolean, integer, number, object, string, write_python_json, write_text,
};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `DEVICES_FILENAME`.
const DEVICES_FILENAME: &str = "devices.json";
/// `DEFAULT_PAIR_TTL_S`.
const DEFAULT_PAIR_TTL_S: i64 = 600;
/// `CODE_ALPHABET` — no 0/O, 1/I/L: codes are read off a terminal screen.
const CODE_ALPHABET: &[u8] = b"ABCDEFGHJKMNPQRSTUVWXYZ23456789";
/// `CODE_LENGTH`.
const CODE_LENGTH: usize = 8;

fn devices_path(dir: &Path) -> PathBuf {
    dir.join(DEVICES_FILENAME)
}

/// `_load`: a missing, unreadable or non-object file reads as "nothing paired".
fn load(dir: &Path) -> Map<String, Value> {
    let mut data = match std::fs::read_to_string(devices_path(dir))
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
    {
        Some(Value::Object(map)) => map,
        _ => Map::new(),
    };
    data.entry("version".to_string()).or_insert(json!(1));
    for key in ["pairing_codes", "devices", "failed_attempts"] {
        if !data.get(key).is_some_and(Value::is_array) {
            data.insert(key.to_string(), json!([]));
        }
    }
    data
}

/// `_save`: `json.dumps(data, indent=2, sort_keys=True) + "\n"`, `0600`.
fn save(dir: &Path, data: &Map<String, Value>) -> Result<(), String> {
    // Python writes `json.dumps(data, indent=2, sort_keys=True)`, so the key
    // order is alphabetical at every level — not the insertion order the
    // `preserve_order` map keeps.
    let mut document = Value::Object(data.clone());
    xpst_core::json::sort_keys_recursive(&mut document);
    let text = crate::output::python_pretty_json(&document);
    let path = devices_path(dir);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    crate::fsutil::write_private(&path, format!("{text}\n").as_bytes())
}

/// `_sha256`.
fn sha256_hex(text: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// `_normalize_code`: uppercase, alphanumerics only.
fn normalize_code(code: &str) -> String {
    code.chars()
        .filter(|character| character.is_alphanumeric())
        .flat_map(char::to_uppercase)
        .collect()
}

/// `_format_code`.
fn format_code(raw: &str) -> String {
    if raw.chars().count() == CODE_LENGTH {
        let chars: Vec<char> = raw.chars().collect();
        format!(
            "{}-{}",
            chars[..4].iter().collect::<String>(),
            chars[4..].iter().collect::<String>()
        )
    } else {
        raw.to_string()
    }
}

/// `_public_device`: the record with the token hash removed, in Python's key
/// order (`device_id`, `label`, `created_at`, `last_seen_at`, `client`,
/// `revoked`).
fn public_device(device: &Value) -> xpst_config::Value {
    let field = |name: &str| {
        device
            .get(name)
            .map_or_else(crate::output::null, crate::output::from_json)
    };
    let client = match device.get("client") {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        _ => String::new(),
    };
    object(vec![
        ("device_id", field("device_id")),
        ("label", field("label")),
        ("created_at", field("created_at")),
        ("last_seen_at", field("last_seen_at")),
        ("client", string(client)),
        (
            "revoked",
            boolean(
                device
                    .get("revoked")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            ),
        ),
    ])
}

/// `list_devices`: newest first, never the token.
fn list_devices(dir: &Path) -> Vec<xpst_config::Value> {
    let data = load(dir);
    let mut devices: Vec<xpst_config::Value> = data
        .get("devices")
        .and_then(Value::as_array)
        .map(|items| items.iter().map(public_device).collect())
        .unwrap_or_default();
    devices.sort_by(|left, right| {
        let stamp = |value: &xpst_config::Value| {
            value
                .get("created_at")
                .and_then(xpst_config::Value::as_f64)
                .unwrap_or_default()
        };
        stamp(right)
            .partial_cmp(&stamp(left))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    devices
}

/// `revoke_device`: true when a live device was revoked.
fn revoke_device(dir: &Path, device_id: &str) -> Result<bool, String> {
    let mut data = load(dir);
    let Some(devices) = data.get_mut("devices").and_then(Value::as_array_mut) else {
        return Ok(false);
    };
    for device in devices.iter_mut() {
        if device.get("device_id").and_then(Value::as_str) != Some(device_id) {
            continue;
        }
        if device
            .get("revoked")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            return Ok(false);
        }
        if let Some(record) = device.as_object_mut() {
            record.insert("revoked".to_string(), Value::Bool(true));
            record.insert("revoked_at".to_string(), json!(now_seconds()));
        }
        save(dir, &data)?;
        return Ok(true);
    }
    Ok(false)
}

/// `time.time()`.
fn now_seconds() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or_default()
}

/// `create_pairing_code`, returning the payload `pair` prints.
fn create_pairing_code(
    dir: &Path,
    label: &str,
    ttl_s: i64,
    code: Option<&str>,
) -> Result<Vec<(&'static str, xpst_config::Value)>, (String, String)> {
    // Python: `max(30, int(ttl_s or DEFAULT_PAIR_TTL_S))` — `0` is falsy, so
    // `--ttl 0` means the 600 s default, not the 30 s floor.
    let ttl = if ttl_s == 0 {
        DEFAULT_PAIR_TTL_S
    } else {
        ttl_s
    }
    .max(30);
    let issued = now_seconds();
    let raw = match code {
        Some(code) => normalize_code(code),
        None => random_code(),
    };
    if raw.chars().count() != CODE_LENGTH {
        return Err((
            "PAIR_CODE_SHAPE".to_string(),
            format!("A pairing code must be {CODE_LENGTH} characters."),
        ));
    }

    let mut data = load(dir);
    // Drop expired and spent codes; leaving them forever makes "no active code"
    // ambiguous.
    if let Some(codes) = data.get_mut("pairing_codes").and_then(Value::as_array_mut) {
        codes.retain(|item| {
            let redeemed = item
                .get("redeemed_at")
                .is_some_and(|value| !value.is_null());
            let expires = item
                .get("expires_at")
                .and_then(Value::as_f64)
                .unwrap_or_default();
            !redeemed && expires > issued
        });
    }
    let label: String = label.chars().take(80).collect();
    let entry = json!({
        "code_sha256": sha256_hex(&raw),
        "label": label,
        "created_at": issued,
        "expires_at": issued + ttl as f64,
        "attempts": 0,
        "redeemed_at": Value::Null,
        "device_id": Value::Null,
    });
    if let Some(codes) = data.get_mut("pairing_codes").and_then(Value::as_array_mut) {
        codes.push(entry);
    }
    // Issuing a code proves a human is at the engine machine: a fresh code
    // starts a fresh guess budget.
    data.insert("failed_attempts".to_string(), json!([]));
    save(dir, &data).map_err(|error| ("PAIR_STORE_ERROR".to_string(), error))?;

    Ok(vec![
        ("code", string(format_code(&raw))),
        ("label", string(label)),
        ("created_at", number(issued)),
        ("expires_at", number(issued + ttl as f64)),
        ("expires_in_s", integer(ttl)),
    ])
}

/// Eight characters from `CODE_ALPHABET`, drawn from OS entropy.
fn random_code() -> String {
    let mut bytes = [0u8; CODE_LENGTH];
    if getrandom::fill(&mut bytes).is_err() {
        // A code that cannot be random must not be predictable: fall back to a
        // time-derived seed rather than a constant.
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or_default();
        for (index, byte) in bytes.iter_mut().enumerate() {
            *byte = ((nanos >> (index * 8)) & 0xff) as u8;
        }
    }
    bytes
        .iter()
        .map(|byte| CODE_ALPHABET[*byte as usize % CODE_ALPHABET.len()] as char)
        .collect()
}

fn config_dir(global: &GlobalOptions) -> Result<PathBuf, bootstrap::BootstrapError> {
    let path = bootstrap::ensure_config(global)?;
    Ok(path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from(".")))
}

/// `xpst devices [--revoke ID]`.
pub fn list(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let dir = match config_dir(global) {
        Ok(dir) => dir,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };

    if let Some(revoke_id) = leaf_string(leaf, "opt:revoke").filter(|value| !value.is_empty()) {
        let revoked = match revoke_device(&dir, &revoke_id) {
            Ok(revoked) => revoked,
            Err(error) => {
                let _ = writeln!(err, "xpst devices: cannot write devices.json: {error}");
                return exit::GENERAL;
            }
        };
        if json_mode {
            let payload = object(vec![
                ("ok", boolean(revoked)),
                ("device_id", string(revoke_id.clone())),
                ("revoked", boolean(revoked)),
            ]);
            let _ = write_python_json(out, &payload);
        } else if revoked {
            let _ = write_text(out, &format!("Revoked {revoke_id}."));
        } else {
            let _ = write_text(
                out,
                &format!("{revoke_id} was already revoked (or is unknown)."),
            );
        }
        return if revoked {
            exit::SUCCESS
        } else {
            exit::CONFIG_ERROR
        };
    }

    let rows = list_devices(&dir);
    if json_mode {
        let payload = object(vec![
            ("ok", boolean(true)),
            ("devices", array(rows.clone())),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }
    if rows.is_empty() {
        let _ = write_text(out, "No paired devices. Run `xpst pair` to add one.");
        return exit::SUCCESS;
    }
    let mut text = String::new();
    for row in &rows {
        let state = if row.get("revoked").and_then(xpst_config::Value::as_bool) == Some(true) {
            "revoked"
        } else {
            "active"
        };
        text.push_str(&format!(
            "{}  {}  ({state})\n",
            row.get("device_id")
                .and_then(xpst_config::Value::as_str)
                .unwrap_or(""),
            row.get("label")
                .and_then(xpst_config::Value::as_str)
                .unwrap_or(""),
        ));
    }
    let _ = write_text(out, &text);
    exit::SUCCESS
}

/// `xpst pair`.
pub fn pair(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let dir = match config_dir(global) {
        Ok(dir) => dir,
        Err(error) => return bootstrap::report(err, json_mode, out, &error),
    };
    let label = leaf_string(leaf, "opt:label").unwrap_or_else(|| "phone".to_string());
    let ttl = crate::clickargs::leaf_integer(leaf, "opt:ttl").unwrap_or(DEFAULT_PAIR_TTL_S);
    let code = leaf_string(leaf, "opt:code");
    let host = leaf_string(leaf, "opt:host").unwrap_or_else(|| "127.0.0.1".to_string());
    let port =
        crate::clickargs::leaf_integer_text(leaf, "opt:port").unwrap_or_else(|| "8080".to_string());

    let issued = match create_pairing_code(&dir, &label, ttl, code.as_deref()) {
        Ok(issued) => issued,
        Err((code, message)) => {
            if json_mode {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    (
                        "error",
                        object(vec![("code", string(code)), ("message", string(message))]),
                    ),
                ]);
                let _ = write_python_json(out, &payload);
            } else {
                let _ = write_text(out, &message);
            }
            return exit::CONFIG_ERROR;
        }
    };

    let url = format!("http://{host}:{port}");
    if json_mode {
        let mut entries = vec![("ok", boolean(true)), ("url", string(url.clone()))];
        entries.extend(issued.iter().cloned());
        let _ = write_python_json(out, &object(entries));
        return exit::SUCCESS;
    }

    let code = issued
        .iter()
        .find(|(key, _)| *key == "code")
        .and_then(|(_, value)| value.as_str())
        .unwrap_or_default()
        .to_string();
    let label = issued
        .iter()
        .find(|(key, _)| *key == "label")
        .and_then(|(_, value)| value.as_str())
        .unwrap_or_default()
        .to_string();
    let expires_in = issued
        .iter()
        .find(|(key, _)| *key == "expires_in_s")
        .and_then(|(_, value)| value.as_i64())
        .unwrap_or_default();
    let mut text = format!("Pairing code: {code}\n");
    text.push_str(&format!(
        "Valid for {} min, single use, label '{label}'.\n",
        expires_in / 60
    ));
    text.push_str(&format!(
        "On the phone, open {url} and enter the code (or POST /api/pair {{\"code\": \"{code}\"}}).\n"
    ));
    let _ = write_text(out, &text);
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_are_normalized_and_formatted() {
        assert_eq!(normalize_code("abcd-2345"), "ABCD2345");
        assert_eq!(format_code("ABCD2345"), "ABCD-2345");
        assert_eq!(format_code("abc"), "abc");
    }

    #[test]
    fn a_corrupt_file_reads_as_nothing_paired() {
        let dir = std::env::temp_dir().join(format!("xpst-devices-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(devices_path(&dir), "{not json").unwrap();
        let data = load(&dir);
        assert_eq!(data["version"], json!(1));
        assert_eq!(data["devices"], json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_fixed_code_is_hashed_and_never_stored_in_clear() {
        let dir = std::env::temp_dir().join(format!("xpst-pair-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let issued = create_pairing_code(&dir, "phone", 600, Some("ABCD2345")).expect("issued");
        assert_eq!(issued[0].1, crate::output::string("ABCD-2345"));
        let text = std::fs::read_to_string(devices_path(&dir)).unwrap();
        assert!(
            !text.contains("ABCD2345"),
            "the clear code must not be stored"
        );
        assert!(text.contains(&sha256_hex("ABCD2345")));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_short_code_is_refused() {
        let dir = std::env::temp_dir().join(format!("xpst-pair-short-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let error = create_pairing_code(&dir, "phone", 600, Some("abc")).unwrap_err();
        assert_eq!(error.0, "PAIR_CODE_SHAPE");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
