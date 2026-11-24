//! Behaviour of the two local config writers, driven from a fixture profile.
//!
//! The wire bodies are pinned by `crates/xpst-cli/tests/mcp_stdio.rs` against a
//! capture from the shipped Python server; these tests pin the FILESYSTEM
//! effects, which a payload cannot prove: which credential files a disconnect
//! actually removed, which it left alone, and that `messenger_set_rules` wrote
//! the rules it reported.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use xpst_mcp::registry::registry;
use xpst_mcp::{StorePaths, ToolContext};

fn profile(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create fixture profile");
    dir
}

fn ctx(dir: &Path) -> ToolContext {
    ToolContext::new(StorePaths::at(dir))
}

fn call(ctx: &ToolContext, tool: &str, args: Value) -> Value {
    registry()
        .dispatch(ctx, tool, &args)
        .unwrap_or_else(|err| panic!("{tool} failed: {err}"))
}

const CONNECTED_YOUTUBE: &str = "version: 4\n\
accounts:\n  youtube:\n    enabled: true\n    channel_id: chan-parity\n\
  messenger:\n    enabled: true\n    auto_reply: false\n    comment_reply_enabled: true\n";

/// Seed the credential store and the plaintext files a connected YouTube
/// profile holds, plus one artifact that is NOT YouTube's.
fn seed_connected(dir: &Path) {
    std::fs::write(dir.join("config.yaml"), CONNECTED_YOUTUBE).expect("seed config");
    let store = xpst_auth::credentials::CredentialStore::new(dir);
    store
        .store("youtube_token", "{\"token\": \"parity\"}")
        .expect("store youtube token");
    store
        .store("messenger_page_token", "parity-page-token")
        .expect("store messenger token");
    for name in [
        "credentials/youtube_token.json",
        "sessions/youtube-parity.json",
        "sessions/x-parity.json",
    ] {
        let path = dir.join(name);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("seed dir");
        std::fs::write(&path, "parity").expect("seed file");
    }
}

#[test]
fn disconnect_removes_only_the_platforms_own_artifacts() {
    let dir = profile("disconnect_youtube");
    seed_connected(&dir);

    let payload = call(
        &ctx(&dir),
        "xpst_disconnect",
        json!({"platform": "youtube"}),
    );

    assert_eq!(payload["success"], json!(true));
    assert_eq!(payload["disabled"], json!(true));
    assert_eq!(
        payload["removed"],
        json!([
            "youtube_token",
            "youtube_token.json",
            "sessions/youtube-parity.json"
        ])
    );
    // The store key, the plaintext token and the session artifact are gone...
    assert!(!dir.join("credentials/youtube_token.enc").exists());
    assert!(!dir.join("credentials/youtube_token.json").exists());
    assert!(!dir.join("sessions/youtube-parity.json").exists());
    // ...and everything that is not YouTube's survives.
    assert!(dir.join("sessions/x-parity.json").exists());
    assert!(dir.join("credentials/messenger_page_token.enc").exists());
    // The config says disabled, and the whole file was rewritten from the model.
    let config = std::fs::read_to_string(dir.join("config.yaml")).expect("config");
    assert!(
        config.contains("  youtube:\n    enabled: false\n"),
        "{config}"
    );
    assert!(config.contains("channel_id: chan-parity"), "{config}");
    // `save()` does not serialize `comment_reply_enabled`, so the write drops it.
    assert!(
        !config.contains("comment_reply_enabled"),
        "the shipped save() drops this field: {config}"
    );
}

#[test]
fn a_second_disconnect_finds_nothing_and_reports_not_disabled() {
    let dir = profile("disconnect_twice");
    seed_connected(&dir);
    call(
        &ctx(&dir),
        "xpst_disconnect",
        json!({"platform": "youtube"}),
    );

    let payload = call(
        &ctx(&dir),
        "xpst_disconnect",
        json!({"platform": "youtube"}),
    );
    assert_eq!(payload["success"], json!(true));
    assert_eq!(payload["removed"], json!([]));
    assert_eq!(payload["disabled"], json!(false));
}

#[test]
fn an_unknown_platform_touches_nothing() {
    let dir = profile("disconnect_unknown");
    seed_connected(&dir);
    let before = std::fs::read_to_string(dir.join("config.yaml")).expect("config");

    let payload = call(&ctx(&dir), "xpst_disconnect", json!({"platform": "nope"}));

    assert_eq!(payload["success"], json!(false));
    assert_eq!(payload["error"], json!("Unknown platform: nope"));
    assert_eq!(payload["removed"], json!([]));
    // The early return happens BEFORE the credential sweep and the config write.
    assert!(dir.join("credentials/youtube_token.enc").exists());
    assert!(dir.join("sessions/youtube-parity.json").exists());
    assert_eq!(
        std::fs::read_to_string(dir.join("config.yaml")).expect("config"),
        before
    );
}

#[test]
fn a_non_string_platform_is_echoed_raw_and_named_with_str() {
    let dir = profile("disconnect_number");
    seed_connected(&dir);

    let payload = call(&ctx(&dir), "xpst_disconnect", json!({"platform": 5}));

    // Python echoes the argument it was given, not a coerced string.
    assert_eq!(payload["platform"], json!(5));
    assert_eq!(payload["error"], json!("Unknown platform: 5"));
}

#[test]
fn messenger_set_rules_writes_the_coerced_map() {
    let dir = profile("messenger_rules");
    std::fs::write(dir.join("config.yaml"), CONNECTED_YOUTUBE).expect("seed config");

    let payload = call(
        &ctx(&dir),
        "messenger_set_rules",
        json!({"rules": {"price": 5, "*": "Thanks"}, "auto_reply": true}),
    );

    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["auto_reply"], json!(true));
    // `str()` on both sides of the map.
    assert_eq!(payload["reply_rules"], json!({"price": "5", "*": "Thanks"}));

    let config = std::fs::read_to_string(dir.join("config.yaml")).expect("config");
    assert!(config.contains("    auto_reply: true\n"), "{config}");
    assert!(
        config.contains("    reply_rules:\n      price: '5'\n      '*': Thanks\n"),
        "{config}"
    );
    // The whole-file rewrite drops the field `save()` never serializes.
    assert!(!config.contains("comment_reply_enabled"), "{config}");
}

#[test]
fn messenger_set_rules_defaults_auto_reply_to_true_and_keeps_an_empty_map() {
    let dir = profile("messenger_rules_default");
    std::fs::write(dir.join("config.yaml"), CONNECTED_YOUTUBE).expect("seed config");

    let payload = call(&ctx(&dir), "messenger_set_rules", json!({}));
    assert_eq!(payload["auto_reply"], json!(true));
    assert_eq!(payload["reply_rules"], json!({}));
}

#[test]
fn messenger_set_rules_refuses_a_truthy_non_object_with_pythons_sentence() {
    let dir = profile("messenger_rules_refused");
    std::fs::write(dir.join("config.yaml"), CONNECTED_YOUTUBE).expect("seed config");

    let error = registry()
        .dispatch(&ctx(&dir), "messenger_set_rules", &json!({"rules": "abc"}))
        .expect_err("must refuse");
    // `ToolError::Handler` renders Python's `Error: <sentence>` body.
    assert_eq!(
        error.to_string(),
        "Error: 'str' object has no attribute 'items'"
    );
    // Nothing was written.
    assert_eq!(
        std::fs::read_to_string(dir.join("config.yaml")).expect("config"),
        CONNECTED_YOUTUBE
    );
}
