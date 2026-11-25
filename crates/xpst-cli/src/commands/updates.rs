//! `check_update_components(include_network=False)` — the local component
//! inventory, shared by `xpst update --components` and `xpst diagnostics`.
//!
//! Parity target: `src/xpst/updater.py::check_update_components` plus
//! `_annotate_component`, `check_helper_tools` and `check_provider_metadata`.
//!
//! Two consumers, one builder, because the card that commissioned this slice
//! found the `updates` section of `diagnostics` already porting the same rows:
//!
//! * `xpst update --components` (no `--check`) passes `include_network=False`,
//!   so it is the offline document — the app row declares `update_mode: "pip"`
//!   exactly as the shipped engine does;
//! * `xpst diagnostics` reports the same rows for a compiled engine, where the
//!   app row's mode is `binary` (there is no pip install to update).
//!
//! **Divergences, recorded in the ledger note:**
//!
//! * `packages` is empty. `TRACKED_PACKAGES` are Python distributions that a
//!   compiled engine does not ship; Python reports their installed versions
//!   from `importlib.metadata`, which is a property of the interpreter rather
//!   than of the engine. The fixture masks the section and compares the key.
//! * `current_version` for the app and provider-metadata rows is the Rust
//!   engine's own version, where Python reports its package version. The
//!   fixture masks the engine's version string by value.
//! * `latest_version` is always `null` (no network), so `updatable` is always
//!   false and the `update_available` branch of `_annotate_component` is
//!   unreachable here — the same is true of the shipped offline run.

use serde_json::{json, Value};

/// `_annotate_component()`'s ladder, for the offline (`latest_version: None`)
/// case only.
///
/// The `update_available` arm is unreachable without a network check, which is
/// why it is not modelled: `include_network=False` makes `latest_version` null
/// for every row.
fn annotate(
    name: &str,
    component_type: &str,
    current_version: Option<String>,
    installed: bool,
    update_mode: &str,
    required: bool,
) -> Value {
    let (status, action, update_command) = if !installed {
        let (action, command) = match update_mode {
            "external" => (
                format!("Install {name} with your operating system package manager."),
                Value::Null,
            ),
            "pip" => (
                format!("Install {name} with xPST's updater."),
                json!("xpst update"),
            ),
            _ => (format!("Install or configure {name}."), Value::Null),
        };
        ("missing", action, command)
    } else if update_mode == "external" {
        (
            "manual",
            format!(
                "{name} is installed; update it with your operating system package manager \
                 when needed."
            ),
            Value::Null,
        )
    } else if update_mode == "bundled" {
        (
            "current",
            "Provider metadata is bundled; update xPST to refresh it.".to_string(),
            json!("xpst update --check"),
        )
    } else if update_mode == "pip" {
        (
            "unknown",
            "Run xpst update --components --check to check latest versions.".to_string(),
            json!("xpst update --components --check"),
        )
    } else {
        ("current", "No action needed.".to_string(), Value::Null)
    };

    json!({
        "name": name,
        "component_type": component_type,
        "current_version": current_version,
        "latest_version": Value::Null,
        "installed": installed,
        "update_mode": update_mode,
        "required": required,
        "updatable": false,
        "status": status,
        "action": action,
        "update_command": update_command,
        "error": Value::Null,
    })
}

/// The engine's own version, as the app and provider-metadata rows report it.
pub fn engine_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// `check_update_components(include_network=False)`.
///
/// `app_mode` is `pip` for `xpst update --components` (the shipped document's
/// value) and `binary` for `xpst diagnostics` (a compiled engine has no pip
/// install to update).
pub fn components(app_mode: &str) -> Value {
    let app = annotate(
        "xpst",
        "app",
        Some(engine_version().to_string()),
        true,
        app_mode,
        true,
    );

    // `check_helper_tools()`: yt-dlp is a pip component whose version comes
    // from the binary xPST would actually invoke; FFmpeg is an external one.
    let ytdlp_version = xpst_media::yt_dlp_version();
    let ytdlp = annotate(
        "yt-dlp",
        "helper",
        ytdlp_version.clone(),
        ytdlp_version.is_some(),
        "pip",
        true,
    );

    let ffmpeg_path = xpst_media::resolve_ffmpeg();
    let (ffmpeg_version, ffmpeg_error) = match ffmpeg_path.as_deref() {
        Some(path) => xpst_media::ffmpeg_version(path),
        None => (None, None),
    };
    let mut ffmpeg = annotate(
        "FFmpeg",
        "helper",
        ffmpeg_version,
        ffmpeg_path.is_some(),
        "external",
        true,
    );
    if let Some(error) = ffmpeg_error {
        ffmpeg["error"] = json!(error);
    }

    let provider_metadata = annotate(
        "provider-manifests",
        "provider_metadata",
        Some(engine_version().to_string()),
        true,
        "bundled",
        true,
    );

    json!({
        "app": [app],
        "packages": Vec::<Value>::new(),
        "helpers": [ytdlp, ffmpeg],
        "provider_metadata": [provider_metadata],
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_offline_ladder_matches_the_shipped_one() {
        // `_annotate_component` with `latest_version: None`.
        let missing = annotate("yt-dlp", "helper", None, false, "pip", true);
        assert_eq!(missing["status"], json!("missing"));
        assert_eq!(
            missing["action"],
            json!("Install yt-dlp with xPST's updater.")
        );
        assert_eq!(missing["update_command"], json!("xpst update"));

        let unknown = annotate("yt-dlp", "helper", Some("1".into()), true, "pip", true);
        assert_eq!(unknown["status"], json!("unknown"));
        assert_eq!(
            unknown["update_command"],
            json!("xpst update --components --check")
        );

        let manual = annotate("FFmpeg", "helper", Some("1".into()), true, "external", true);
        assert_eq!(manual["status"], json!("manual"));
        assert_eq!(manual["update_command"], Value::Null);

        let bundled = annotate(
            "provider-manifests",
            "provider_metadata",
            None,
            true,
            "bundled",
            true,
        );
        assert_eq!(bundled["status"], json!("current"));
        assert_eq!(bundled["update_command"], json!("xpst update --check"));

        let binary = annotate("xpst", "app", Some("1".into()), true, "binary", true);
        assert_eq!(binary["status"], json!("current"));
        assert_eq!(binary["action"], json!("No action needed."));
    }

    #[test]
    fn the_document_has_the_four_sections_in_order() {
        let document = components("pip");
        let keys: Vec<&String> = document.as_object().unwrap().keys().collect();
        assert_eq!(
            keys,
            vec!["app", "packages", "helpers", "provider_metadata"],
            "section order drifted from `check_update_components`"
        );
        assert_eq!(document["app"][0]["update_mode"], json!("pip"));
        assert_eq!(document["packages"], json!([]));
        assert_eq!(document["helpers"][0]["name"], json!("yt-dlp"));
        assert_eq!(document["helpers"][1]["name"], json!("FFmpeg"));
        assert_eq!(
            document["provider_metadata"][0]["name"],
            json!("provider-manifests")
        );
    }
}
