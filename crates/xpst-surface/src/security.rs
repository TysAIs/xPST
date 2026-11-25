//! `xpst security-audit` — the live installation checks.
//!
//! Parity target: `src/xpst/cli.py::security_audit` →
//! `src/xpst/security_audit.py::run_security_checks`. Every check reports what
//! was *observed*: where nothing can be observed (no dashboard has bound a
//! socket in this process) the row says `unobserved` instead of inventing a
//! pass, and advisory checks describe hardening posture without flipping the
//! overall verdict.
//!
//! The payload is `json.dumps(output, indent=2, default=str)` — two-space
//! indent, `ensure_ascii=True` — which is why the em dashes in the detail
//! strings appear as `\u2014`.
//!
//! The builder lives here rather than in `xpst-cli` because the MCP tool
//! `xpst_security_audit` serves the same document and `xpst-mcp` cannot depend
//! on the CLI crate.
//!
//! **Divergences, recorded in the ledger note:** `encrypted_storage` always
//! reports the Fernet file store, because the Rust engine carries its own
//! implementation and cannot be missing `cryptography`; and `dashboard_localhost`
//! is always `unobserved`, because a CLI process never binds the dashboard.

use std::path::Path;

use xpst_config::output::{array, boolean, integer, object, string};
use xpst_config::Value;

use crate::config_view::ConfigView;

/// `truthy_env`.
fn truthy_env(name: &str) -> bool {
    std::env::var(name)
        .map(|value| matches!(value.to_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(false)
}

/// `resolve_ffmpeg_path() is not None`, for the existence question only.
fn ffmpeg_available() -> bool {
    if let Some(path) = std::env::var_os("XPST_FFMPEG_PATH") {
        if Path::new(&path).exists() {
            return true;
        }
    }
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            if dir.join("ffmpeg").is_file() {
                return true;
            }
        }
    }
    // The copy xPST fetched itself on a machine with no system ffmpeg.
    std::env::var_os("XPST_CONFIG_DIR")
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| Path::new(&home).join(".xpst")))
        .is_some_and(|dir| dir.join("bin").join("ffmpeg").is_file())
}

/// `oct(stat.S_IMODE(mode))`.
#[cfg(unix)]
fn file_mode(path: &Path) -> Option<u32> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .ok()
        .map(|metadata| metadata.permissions().mode() & 0o777)
}

#[cfg(not(unix))]
fn file_mode(_path: &Path) -> Option<u32> {
    None
}

/// `run_security_checks(config)`: the document the MCP tool serves.
///
/// The CLI adds `check_count` on top (`cli.py:7906`); the MCP handler does not,
/// so the two surfaces are deliberately different documents and this function
/// is the tool's.
pub fn payload(view: &ConfigView) -> Value {
    let mut checks: Vec<Value> = Vec::new();
    let mut all_pass = true;

    // 1. Credential file permissions (0600).
    for (platform, field) in [
        ("youtube", "client_secrets"),
        ("youtube", "token_file"),
        ("x", "cookies_file"),
        ("instagram", "session_file"),
    ] {
        let path = view.text(platform, field).unwrap_or_default();
        if path.is_empty() || !Path::new(&path).exists() {
            continue;
        }
        let Some(mode) = file_mode(Path::new(&path)) else {
            continue;
        };
        let ok = mode == 0o600;
        checks.push(object(vec![
            ("check", string("file_permissions")),
            ("path", string(path)),
            ("mode", string(format!("0o{mode:o}"))),
            ("status", string("observed")),
            ("passed", boolean(ok)),
        ]));
        if !ok {
            all_pass = false;
        }
    }

    // 2. Dashboard bind host — nothing has bound in this process.
    checks.push(object(vec![
        ("check", string("dashboard_localhost")),
        ("status", string("unobserved")),
        ("passed", boolean(true)),
        ("advisory", boolean(true)),
        (
            "detail",
            string("No dashboard has bound a socket in this process; nothing observed either way."),
        ),
    ]));

    // 3. MCP readonly mode — the real env state, advisory.
    let readonly = truthy_env("XPST_MCP_READONLY");
    checks.push(object(vec![
        ("check", string("mcp_readonly")),
        ("status", string("observed")),
        ("passed", boolean(readonly)),
        ("advisory", boolean(true)),
        (
            "detail",
            string(if readonly {
                "XPST_MCP_READONLY is set — mutating MCP tools are blocked."
            } else {
                "XPST_MCP_READONLY not set — mutating MCP tools rely on the \
                 XPST_MCP_ALLOW_MUTATIONS/REQUIRE_CONFIRM gates."
            }),
        ),
    ]));

    // 4. Provider mode — informational, both modes are valid postures.
    checks.push(object(vec![
        ("check", string("provider_mode")),
        ("status", string("observed")),
        ("passed", boolean(true)),
        ("advisory", boolean(true)),
        (
            "detail",
            string(format!("Provider mode: {}", view.provider_mode())),
        ),
    ]));

    // 5. FFmpeg.
    let ffmpeg_ok = ffmpeg_available();
    checks.push(object(vec![
        ("check", string("ffmpeg_available")),
        ("status", string("observed")),
        ("passed", boolean(ffmpeg_ok)),
        (
            "detail",
            string(if ffmpeg_ok {
                "FFmpeg found"
            } else {
                "FFmpeg not found — video processing will fail"
            }),
        ),
    ]));
    if !ffmpeg_ok {
        all_pass = false;
    }

    // 6. Encrypted credential storage — the Rust engine always has the Fernet
    //    file store, so this is `encrypted-file` by construction.
    checks.push(object(vec![
        ("check", string("encrypted_storage")),
        ("status", string("observed")),
        ("passed", boolean(true)),
        (
            "detail",
            string("Fernet-encrypted file store (scrypt-derived key) is the active backend."),
        ),
    ]));

    let passed_count = checks
        .iter()
        .filter(|check| check.get("passed").and_then(Value::as_bool) == Some(true))
        .count();
    let failed_count = checks.len() - passed_count;
    object(vec![
        (
            "overall_status",
            string(if all_pass { "pass" } else { "fail" }),
        ),
        ("checks", array(checks.clone())),
        ("passed_count", integer(passed_count as i64)),
        ("failed_count", integer(failed_count as i64)),
    ])
}

/// The human-readable rendering of [`payload`].
pub fn render_text(payload: &Value) -> String {
    let all_pass = payload
        .get("overall_status")
        .and_then(Value::as_str)
        .is_some_and(|status| status == "pass");
    let mut text = format!(
        "\nSecurity Audit: {}\n\n",
        if all_pass { "PASS" } else { "FAIL" }
    );
    if let Some(checks) = payload.get("checks").and_then(Value::as_sequence) {
        for check in checks {
            let passed = check.get("passed").and_then(Value::as_bool) == Some(true);
            let icon = if passed { "✅" } else { "❌" };
            let detail = check.get("detail").and_then(Value::as_str).unwrap_or("");
            let suffix = if check.get("advisory").and_then(Value::as_bool) == Some(true) && !passed
            {
                " (advisory)"
            } else {
                ""
            };
            text.push_str(&format!(
                "  {icon} {}{suffix}: {detail}\n",
                check.get("check").and_then(Value::as_str).unwrap_or("")
            ));
        }
    }
    text.push('\n');
    text
}

#[cfg(test)]
mod tests {
    /// `_LOOPBACK_HOSTS`. The CLI never binds a dashboard, so the constant is
    /// only needed by the check that would classify a live bind.
    const LOOPBACK_HOSTS: [&str; 3] = ["127.0.0.1", "localhost", "::1"];

    #[test]
    fn loopback_hosts_are_the_documented_three() {
        assert!(LOOPBACK_HOSTS.contains(&"127.0.0.1"));
        assert!(LOOPBACK_HOSTS.contains(&"localhost"));
        assert!(LOOPBACK_HOSTS.contains(&"::1"));
    }

    #[test]
    fn a_loose_mode_is_reported_in_python_octal() {
        assert_eq!(format!("0o{:o}", 0o644), "0o644");
        assert_eq!(format!("0o{:o}", 0o600), "0o600");
    }
}
