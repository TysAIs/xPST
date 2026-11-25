//! `XPSTConfig.save()` — the whole-file config writer.
//!
//! `messenger_set_rules` and `xpst_disconnect` both persist through
//! `XPSTConfig.save()`, and that method does not edit the file it read: it
//! rebuilds the ENTIRE document from the dataclass model and dumps it with
//! `yaml.safe_dump(default_flow_style=False, sort_keys=False)`. Two
//! consequences the port has to keep:
//!
//! * the written key order is `save()`'s declaration order, not the file's, so
//!   the base document is captured from the shipped engine
//!   ([`xpst_config::SAVE_DEFAULTS_YAML`]) rather than merged into the file;
//! * a field the model has and `save()` does not serialize is DROPPED from the
//!   file. `accounts.messenger.comment_reply_enabled`/`comment_platforms`,
//!   `accounts.rumble`, `accounts.x.api_key` and the app secrets are all in that
//!   set — see [`DROPPED_BY_SAVE`]. That is a defect in the shipped engine, and
//!   it is reproduced rather than fixed: the file the Python engine writes is
//!   the contract, and a Rust engine that quietly kept those keys would make the
//!   two engines disagree about the same profile.
//!
//! The order of operations mirrors `XPSTConfig.load()` followed by `save()`:
//!
//! 1. the captured defaults (the pristine save document);
//! 2. `_merge_config` — the file's values, per field, with the nested dataclasses
//!    rebuilt from their OWN defaults (`EncodingConfig(**block)`), not from the
//!    shipped preset;
//! 3. `_fix_legacy_paths` — `.crosspstr` → `.xpst`;
//! 4. `_apply_env_vars` — the `XPST_*` overrides;
//! 5. `_rebase_credential_paths` — a credential path still under `~/.xpst` moves
//!    onto an explicit config directory;
//! 6. `_expand_paths` — `~` and `$VARS`;
//! 7. `_validate` — the range checks `load()` raises on;
//! 8. the `_SECRET_CONFIG_FIELDS` migration — `accounts.tiktok.client_secret`
//!    moves into the encrypted store and the YAML slot is blanked.
//!
//! Everything here is held to `crates/xpst-surface/tests/fixtures/config_save.json`,
//! which `xtask/scripts/capture_config_save.py` produces by running the real
//! Python over a grid of profiles.

use std::path::{Path, PathBuf};

use xpst_config::yaml::Mapping;
use xpst_config::Value;

use crate::config_view::{expand_path, python_str, truthy};

/// Fields the dataclass model carries and `save()` never writes.
///
/// Recorded so the divergence is a documented fact rather than a surprise: a
/// `config.yaml` that sets one of these loses it on the next save, and the
/// `XPST_*` override for it is silently discarded. `messenger_set_rules` is the
/// tool that makes this reachable — it saves the whole file to persist two
/// fields, so a profile with comment auto-reply configured loses that setting.
pub const DROPPED_BY_SAVE: &[&str] = &[
    "accounts.messenger.comment_reply_enabled",
    "accounts.messenger.comment_platforms",
    "accounts.rumble",
    "accounts.x.api_key",
    "accounts.x.api_secret",
    "accounts.x.access_token",
    "accounts.x.access_token_secret",
    "accounts.x.bearer_token",
    "accounts.x.password",
    "accounts.x.max_video_duration_seconds",
    "accounts.instagram.app_secret",
    "accounts.instagram.password",
    "accounts.threads.app_secret",
    "accounts.bluesky.app_password",
    "accounts.youtube.video_mode",
    "video.encoding.youtube.two_pass",
    "video.encoding.youtube.hw_encoder",
];

/// What the writer needs from the process.
pub struct SaveRequest<'a> {
    /// The profile directory (`XPST_CONFIG_DIR`, else `~/.xpst`).
    pub config_dir: &'a Path,
    /// The user's home, which `~` expands against — NOT the config directory.
    pub home: &'a Path,
    /// `os.environ`-shaped lookup, injected so a test can drive it.
    pub env: &'a dyn Fn(&str) -> Option<String>,
    /// `_store_config_secret(config_dir, key, value) -> bool`: true when the
    /// value reached the encrypted store, in which case the YAML slot is
    /// blanked. False leaves the value in the file, which is what the shipped
    /// engine does when the store is unavailable.
    pub store_secret: &'a dyn Fn(&str, &str, &str) -> bool,
}

/// Build the document `save()` would write for `file`.
///
/// `file` is the parsed `config.yaml` (or `None` for a profile that has none).
pub fn save_document(file: Option<&Value>, request: &SaveRequest<'_>) -> Result<Value, String> {
    let config_dir = request.config_dir.display().to_string();
    let home = request.home.display().to_string();
    let mut document = xpst_config::save_defaults(&config_dir, &home)?;

    merge_file(&mut document, file);
    fix_legacy_paths(&mut document);
    apply_env(&mut document, request.env)?;
    rebase_credential_paths(&mut document, request.config_dir, request.home);
    expand_paths(&mut document, request.env);
    validate(&document)?;
    migrate_secret(&mut document, request);

    Ok(document)
}

/// Render a save document exactly as `yaml.safe_dump(..., sort_keys=False)` does.
pub fn render(document: &Value) -> String {
    xpst_config::pyyaml::dump(document)
}

/// Write the document the way `_atomic_write_text` does: owner-only, and a
/// crash mid-write leaves the previous file intact.
pub fn write(path: &Path, document: &Value) -> Result<(), String> {
    let text = render(document);
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    std::fs::create_dir_all(&parent)
        .map_err(|err| format!("cannot create {}: {err}", parent.display()))?;
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "config.yaml".to_string());
    let temporary = parent.join(format!(".{name}.tmp-{}", std::process::id()));
    std::fs::write(&temporary, text.as_bytes())
        .map_err(|err| format!("cannot write {}: {err}", temporary.display()))?;
    restrict_to_owner(&temporary);
    std::fs::rename(&temporary, path).map_err(|err| {
        let _ = std::fs::remove_file(&temporary);
        format!("cannot replace {}: {err}", path.display())
    })?;
    restrict_to_owner(path);
    Ok(())
}

#[cfg(unix)]
fn restrict_to_owner(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn restrict_to_owner(_path: &Path) {}

// ── document navigation ─────────────────────────────────────────────────────

fn get_path<'a>(document: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut current = document;
    for segment in path {
        current = current.get(segment)?;
    }
    Some(current)
}

/// Replace the value at an existing path. Returns false when the path is not in
/// the document — which is how a field `save()` does not serialize stays absent
/// instead of being invented.
fn set_path(document: &mut Value, path: &[&str], value: Value) -> bool {
    let Some((last, parents)) = path.split_last() else {
        return false;
    };
    let mut current = document;
    for segment in parents {
        let Some(map) = current.as_mapping_mut() else {
            return false;
        };
        let Some(next) = map.get_mut(segment) else {
            return false;
        };
        current = next;
    }
    let Some(map) = current.as_mapping_mut() else {
        return false;
    };
    let Some(slot) = map.get_mut(last) else {
        return false;
    };
    *slot = value;
    true
}

fn set_string(document: &mut Value, path: &[&str], text: String) {
    set_path(document, path, Value::String(text));
}

// ── 2. `_merge_config` ──────────────────────────────────────────────────────

/// `accounts.<platform>` fields `_merge_config` copies straight across.
///
/// A field the dataclass has but `save()` does not write is deliberately absent
/// here: copying it would have no effect on the document anyway, and listing it
/// would suggest it survives the save.
const ACCOUNT_FIELDS: &[(&str, &[&str])] = &[
    (
        "tiktok",
        &[
            "username",
            "cookies_from_browser",
            "cookies_file",
            "proxy",
            "enabled",
            "client_key",
            "client_secret",
            "access_token",
            "refresh_token",
            "sandbox",
            "draft_mode",
            "publish_mode",
            "browser_headless",
            "provider",
        ],
    ),
    (
        "youtube",
        &[
            "enabled",
            "client_secrets",
            "token_file",
            "channel_id",
            "username",
            "proxy",
        ],
    ),
    (
        "x",
        &["enabled", "cookies_file", "username", "proxy", "auth_mode"],
    ),
    (
        "instagram",
        &[
            "enabled",
            "session_file",
            "username",
            "proxy",
            "auth_mode",
            "device_id",
            "graph_access_token",
            "graph_ig_user_id",
            "app_id",
            "account_type",
            "account_type_source",
        ],
    ),
    (
        "threads",
        &[
            "enabled",
            "graph_access_token",
            "threads_user_id",
            "proxy",
            "app_id",
        ],
    ),
    ("bluesky", &["enabled", "identifier", "service", "proxy"]),
    (
        "messenger",
        &[
            "enabled",
            "page_id",
            "page_access_token",
            "app_id",
            "app_secret",
            "verify_token",
            "webhook_path",
            "auto_reply",
            "proxy",
        ],
    ),
    (
        "facebook",
        &[
            "enabled",
            "page_id",
            "page_name",
            "page_access_token",
            "app_id",
            "app_secret",
            "graph_user_token",
            "login_config_id",
            "proxy",
        ],
    ),
    ("local", &["path"]),
];

fn merge_file(document: &mut Value, file: Option<&Value>) {
    let Some(file) = file else {
        return;
    };

    if let Some(accounts) = file.get("accounts").and_then(Value::as_mapping) {
        for (platform, fields) in ACCOUNT_FIELDS {
            // `if tk and isinstance(tk, dict)`: an empty or null section leaves
            // every default in place.
            let Some(section) = accounts.get(platform).and_then(Value::as_mapping) else {
                continue;
            };
            if section.is_empty() {
                continue;
            }
            for field in *fields {
                if let Some(value) = section.get(field) {
                    set_path(document, &["accounts", platform, field], value.clone());
                }
            }
            if *platform == "tiktok" {
                merge_aggregator(document, section);
            }
            if *platform == "messenger" {
                // `if isinstance(ms.get("reply_rules"), dict)` — a scalar or a
                // list leaves the current map alone.
                if let Some(rules) = section.get("reply_rules").and_then(Value::as_mapping) {
                    set_path(
                        document,
                        &["accounts", "messenger", "reply_rules"],
                        Value::Mapping(rules.clone()),
                    );
                }
            }
        }
    }

    if let Some(video) = file.get("video").and_then(Value::as_mapping) {
        if !video.is_empty() {
            for field in ["download_dir", "cleanup_after_post"] {
                if let Some(value) = video.get(field) {
                    set_path(document, &["video", field], value.clone());
                }
            }
            if let Some(encoding) = video.get("encoding").and_then(Value::as_mapping) {
                if !encoding.is_empty() {
                    for platform in ["youtube", "instagram", "x", "tiktok"] {
                        let Some(block) = encoding.get(platform).and_then(Value::as_mapping) else {
                            continue;
                        };
                        if block.is_empty() {
                            continue;
                        }
                        replace_from_class(
                            document,
                            &["video", "encoding", platform],
                            &format!("video.encoding_{platform}"),
                            block,
                        );
                    }
                }
            }
        }
    }

    // `ReliabilityConfig(**block)`, `MonitoringConfig(**block)` and
    // `ScheduleConfig(**block)`: the whole section is rebuilt from the class
    // defaults, so a partial block resets every field it omits.
    for section in ["reliability", "monitoring", "schedule"] {
        if let Some(block) = file.get(section).and_then(Value::as_mapping) {
            if !block.is_empty() {
                replace_from_class(document, &[section], section, block);
            }
        }
    }

    if let Some(notifications) = file.get("notifications").and_then(Value::as_mapping) {
        for field in ["enabled", "on_success", "on_failure"] {
            if let Some(value) = notifications.get(field) {
                set_path(document, &["notifications", field], value.clone());
            }
        }
        if let Some(discord) = notifications.get("discord").and_then(Value::as_mapping) {
            if let Some(value) = discord.get("webhook_url") {
                set_path(
                    document,
                    &["notifications", "discord", "webhook_url"],
                    value.clone(),
                );
            }
        }
        if let Some(telegram) = notifications.get("telegram").and_then(Value::as_mapping) {
            for field in ["bot_token", "chat_id"] {
                if let Some(value) = telegram.get(field) {
                    set_path(
                        document,
                        &["notifications", "telegram", field],
                        value.clone(),
                    );
                }
            }
        }
    }

    if let Some(limits) = file.get("rate_limits").and_then(Value::as_mapping) {
        if !limits.is_empty() {
            for platform in ["youtube", "instagram", "x", "tiktok", "threads", "facebook"] {
                if let Some(value) = limits.get(platform) {
                    set_path(document, &["rate_limits", platform], value.clone());
                }
            }
        }
    }

    merge_bio(document, file);
    merge_chat(document, file);

    if let Some(anti_bot) = file.get("anti_bot").and_then(Value::as_mapping) {
        if let Some(value) = anti_bot.get("caption_variation") {
            // `bool(...)`, so the STRING "false" is true.
            set_path(
                document,
                &["anti_bot", "caption_variation"],
                Value::Bool(truthy(value)),
            );
        }
    }

    // `config._shortcuts = file_config["shortcuts"]` — replaced wholesale, even
    // with an empty map.
    if let Some(shortcuts) = file.get("shortcuts").and_then(Value::as_mapping) {
        set_path(document, &["shortcuts"], Value::Mapping(shortcuts.clone()));
    }

    if let Some(value) = file.get("first_run_complete") {
        set_path(
            document,
            &["first_run_complete"],
            Value::Bool(truthy(value)),
        );
    }

    if let Some(value) = file.get("provider_mode") {
        // `str(...)`: `None` becomes `"None"`, `true` becomes `"True"`.
        set_string(document, &["provider_mode"], python_str(value));
    }
}

fn merge_aggregator(document: &mut Value, section: &Mapping) {
    let Some(aggregator) = section.get("aggregator").and_then(Value::as_mapping) else {
        return;
    };
    for field in ["api_key_ref", "account_id", "profile_id"] {
        if let Some(value) = aggregator.get(field) {
            set_path(
                document,
                &["accounts", "tiktok", "aggregator", field],
                value.clone(),
            );
        }
    }
    // `bool(agg.get(...))` on these two.
    for field in ["vendor_schedule", "scoped_key"] {
        if let Some(value) = aggregator.get(field) {
            set_path(
                document,
                &["accounts", "tiktok", "aggregator", field],
                Value::Bool(truthy(value)),
            );
        }
    }
}

fn merge_bio(document: &mut Value, file: &Value) {
    let Some(bio) = file.get("bio").and_then(Value::as_mapping) else {
        return;
    };
    if bio.is_empty() {
        return;
    }
    // `str(bio.get("handle", config.bio.handle) or "")`
    let current = get_path(document, &["bio", "handle"]).cloned();
    let raw = bio.get("handle").cloned().or(current);
    let handle = match raw {
        Some(value) if truthy(&value) => python_str(&value),
        _ => String::new(),
    };
    set_string(document, &["bio", "handle"], handle);

    // `if isinstance(bio.get("links"), list)`: an entry that is not a mapping is
    // skipped, and an entry missing a label or a url is dropped.
    if let Some(Value::Sequence(items)) = bio.get("links") {
        let mut links = Vec::new();
        for item in items {
            let Some(entry) = item.as_mapping() else {
                continue;
            };
            let label = entry
                .get("label")
                .map(python_str)
                .unwrap_or_default()
                .trim()
                .to_string();
            let url = entry
                .get("url")
                .map(python_str)
                .unwrap_or_default()
                .trim()
                .to_string();
            if label.is_empty() || url.is_empty() {
                continue;
            }
            let mut link = Mapping::new();
            link.insert(Value::from("label"), Value::String(label));
            link.insert(Value::from("url"), Value::String(url));
            links.push(Value::Mapping(link));
        }
        set_path(document, &["bio", "links"], Value::Sequence(links));
    }
}

fn merge_chat(document: &mut Value, file: &Value) {
    let Some(chat) = file.get("chat").and_then(Value::as_mapping) else {
        return;
    };
    for field in ["twitch_channels", "kick_channels"] {
        if let Some(Value::Sequence(items)) = chat.get(field) {
            let channels: Vec<Value> = items
                .iter()
                .map(|item| python_str(item).trim().to_string())
                .filter(|name| !name.is_empty())
                .map(Value::String)
                .collect();
            set_path(document, &["chat", field], Value::Sequence(channels));
        }
    }
    if let Some(value) = chat.get("youtube_video_id") {
        let text = if truthy(value) {
            python_str(value)
        } else {
            String::new()
        };
        set_string(document, &["chat", "youtube_video_id"], text);
    }
    // `int(...)`/`float(...)` inside a `try`: a value that does not convert
    // leaves the current one alone.
    if let Some(value) = chat.get("max_messages") {
        if let Some(number) = python_int(value) {
            set_path(
                document,
                &["chat", "max_messages"],
                Value::Number(number.into()),
            );
        }
    }
    if let Some(value) = chat.get("youtube_poll_interval_s") {
        if let Some(number) = python_float(value) {
            if let Some(number) = serde_json::Number::from_f64(number) {
                set_path(
                    document,
                    &["chat", "youtube_poll_interval_s"],
                    Value::Number(number),
                );
            }
        }
    }
}

/// Rebuild one nested section from its dataclass defaults plus the file's block.
///
/// The save document's own key order is the shape; `class_defaults` supplies the
/// value for a field the block omits. That is `ClassName(**block)` exactly: a
/// partial `video.encoding.youtube` block saves every other encoding field as
/// `null`, not as the shipped preset.
fn replace_from_class(document: &mut Value, path: &[&str], class_path: &str, block: &Mapping) {
    let base = xpst_config::class_defaults(class_path);
    let Some(shape) = get_path(document, path).and_then(Value::as_mapping) else {
        return;
    };
    let mut rebuilt = Mapping::new();
    for (key, _) in shape.iter() {
        let Some(key) = key.as_str() else {
            continue;
        };
        let value = block
            .get(key)
            .or_else(|| base.as_ref().and_then(|base| base.get(key)))
            .cloned()
            .unwrap_or(Value::Null);
        rebuilt.insert(Value::from(key), value);
    }
    set_path(document, path, Value::Mapping(rebuilt));
}

// ── 3. `_fix_legacy_paths` ──────────────────────────────────────────────────

const LEGACY_PATH_FIELDS: &[(&str, &str)] = &[
    ("youtube", "client_secrets"),
    ("youtube", "token_file"),
    ("x", "cookies_file"),
    ("instagram", "session_file"),
    ("tiktok", "cookies_file"),
];

fn fix_legacy_paths(document: &mut Value) {
    let mut fields: Vec<(&str, &str)> = LEGACY_PATH_FIELDS.to_vec();
    fields.push(("video", "download_dir"));
    fields.push(("monitoring", "log_file"));
    for (section, field) in fields {
        let path: &[&str] = if section == "video" || section == "monitoring" {
            &[section, field]
        } else {
            &["accounts", section, field]
        };
        let Some(Value::String(raw)) = get_path(document, path) else {
            continue;
        };
        if raw.is_empty() {
            continue;
        }
        // `path.replace(".crosspstr", ".xpst").replace("crosspstr", "xpst")`
        let fixed = raw
            .replace(".crosspstr", ".xpst")
            .replace("crosspstr", "xpst");
        if fixed != *raw {
            set_string(document, path, fixed);
        }
    }
}

// ── 4. `_apply_env_vars` ────────────────────────────────────────────────────

/// How an override's text becomes a value.
#[derive(Clone, Copy)]
enum Kind {
    /// Assigned verbatim.
    Text,
    /// `v.lower() in ("true", "1", "yes")`.
    Bool,
    /// `v.lower()` — no strip, so `"  Community  "` keeps its spaces.
    Lower,
    /// `v.strip().lower()`.
    LowerStripped,
    /// `v.strip().lower()`, but only for a known publish route.
    PublishMode,
    /// `json.loads(v)` into a mapping; invalid JSON empties it, a non-object
    /// leaves the current value alone.
    JsonMapping,
    /// `int(v)` — raises, which fails the whole load.
    Int,
}

struct EnvRule {
    var: &'static str,
    path: &'static [&'static str],
    kind: Kind,
}

/// One row per `XPST_*` override `_apply_env_vars` reads.
///
/// The rows whose path is not in the save document (`accounts.x.api_key`,
/// `accounts.messenger.comment_platforms`, …) are listed on purpose: the shipped
/// engine sets the dataclass field and `save()` then drops it, so the override
/// has no effect on the file. `set_path` returning false reproduces that, and
/// the capture's `changes: []` row pins it.
const ENV_RULES: &[EnvRule] = &[
    // TikTok
    EnvRule {
        var: "XPST_TIKTOK_USERNAME",
        path: &["accounts", "tiktok", "username"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_COOKIES_FROM_BROWSER",
        path: &["accounts", "tiktok", "cookies_from_browser"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_TIKTOK_PROXY",
        path: &["accounts", "tiktok", "proxy"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_PROVIDER",
        path: &["accounts", "tiktok", "provider"],
        kind: Kind::LowerStripped,
    },
    EnvRule {
        var: "XPST_TIKTOK_AGGREGATOR_ACCOUNT_ID",
        path: &["accounts", "tiktok", "aggregator", "account_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_AGGREGATOR_PROFILE_ID",
        path: &["accounts", "tiktok", "aggregator", "profile_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_ZERNIO_API_KEY_REF",
        path: &["accounts", "tiktok", "aggregator", "api_key_ref"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_CLIENT_KEY",
        path: &["accounts", "tiktok", "client_key"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_CLIENT_SECRET",
        path: &["accounts", "tiktok", "client_secret"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_ACCESS_TOKEN",
        path: &["accounts", "tiktok", "access_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_REFRESH_TOKEN",
        path: &["accounts", "tiktok", "refresh_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TIKTOK_SANDBOX",
        path: &["accounts", "tiktok", "sandbox"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_TIKTOK_PUBLISH_MODE",
        path: &["accounts", "tiktok", "publish_mode"],
        kind: Kind::PublishMode,
    },
    EnvRule {
        var: "XPST_TIKTOK_BROWSER_HEADLESS",
        path: &["accounts", "tiktok", "browser_headless"],
        kind: Kind::Bool,
    },
    // YouTube
    EnvRule {
        var: "XPST_YOUTUBE_ENABLED",
        path: &["accounts", "youtube", "enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_YOUTUBE_CLIENT_SECRETS",
        path: &["accounts", "youtube", "client_secrets"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_YOUTUBE_TOKEN_FILE",
        path: &["accounts", "youtube", "token_file"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_YOUTUBE_PROXY",
        path: &["accounts", "youtube", "proxy"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_YOUTUBE_CHANNEL_ID",
        path: &["accounts", "youtube", "channel_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_YOUTUBE_USERNAME",
        path: &["accounts", "youtube", "username"],
        kind: Kind::Text,
    },
    // X
    EnvRule {
        var: "XPST_X_ENABLED",
        path: &["accounts", "x", "enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_X_COOKIES_FILE",
        path: &["accounts", "x", "cookies_file"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_PROXY",
        path: &["accounts", "x", "proxy"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_AUTH_MODE",
        path: &["accounts", "x", "auth_mode"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_API_KEY",
        path: &["accounts", "x", "api_key"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_API_SECRET",
        path: &["accounts", "x", "api_secret"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_ACCESS_TOKEN",
        path: &["accounts", "x", "access_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_ACCESS_TOKEN_SECRET",
        path: &["accounts", "x", "access_token_secret"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_BEARER_TOKEN",
        path: &["accounts", "x", "bearer_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_X_USERNAME",
        path: &["accounts", "x", "username"],
        kind: Kind::Text,
    },
    // Instagram
    EnvRule {
        var: "XPST_INSTAGRAM_ENABLED",
        path: &["accounts", "instagram", "enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_SESSION_FILE",
        path: &["accounts", "instagram", "session_file"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_USERNAME",
        path: &["accounts", "instagram", "username"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_PROXY",
        path: &["accounts", "instagram", "proxy"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_AUTH_MODE",
        path: &["accounts", "instagram", "auth_mode"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_GRAPH_ACCESS_TOKEN",
        path: &["accounts", "instagram", "graph_access_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_GRAPH_IG_USER_ID",
        path: &["accounts", "instagram", "graph_ig_user_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_APP_ID",
        path: &["accounts", "instagram", "app_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_INSTAGRAM_APP_SECRET",
        path: &["accounts", "instagram", "app_secret"],
        kind: Kind::Text,
    },
    // Bluesky
    EnvRule {
        var: "XPST_BLUESKY_ENABLED",
        path: &["accounts", "bluesky", "enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_BLUESKY_IDENTIFIER",
        path: &["accounts", "bluesky", "identifier"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_BLUESKY_SERVICE",
        path: &["accounts", "bluesky", "service"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_BLUESKY_PROXY",
        path: &["accounts", "bluesky", "proxy"],
        kind: Kind::Text,
    },
    // Threads
    EnvRule {
        var: "XPST_THREADS_ENABLED",
        path: &["accounts", "threads", "enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_THREADS_GRAPH_ACCESS_TOKEN",
        path: &["accounts", "threads", "graph_access_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_THREADS_USER_ID",
        path: &["accounts", "threads", "threads_user_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_THREADS_APP_ID",
        path: &["accounts", "threads", "app_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_THREADS_APP_SECRET",
        path: &["accounts", "threads", "app_secret"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_THREADS_PROXY",
        path: &["accounts", "threads", "proxy"],
        kind: Kind::Text,
    },
    // Messenger
    EnvRule {
        var: "XPST_MESSENGER_ENABLED",
        path: &["accounts", "messenger", "enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_MESSENGER_PAGE_ACCESS_TOKEN",
        path: &["accounts", "messenger", "page_access_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_APP_ID",
        path: &["accounts", "messenger", "app_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_APP_SECRET",
        path: &["accounts", "messenger", "app_secret"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_VERIFY_TOKEN",
        path: &["accounts", "messenger", "verify_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_PAGE_ID",
        path: &["accounts", "messenger", "page_id"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_AUTO_REPLY",
        path: &["accounts", "messenger", "auto_reply"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_MESSENGER_REPLY_RULES",
        path: &["accounts", "messenger", "reply_rules"],
        kind: Kind::JsonMapping,
    },
    EnvRule {
        var: "XPST_MESSENGER_COMMENT_REPLY_ENABLED",
        path: &["accounts", "messenger", "comment_reply_enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_MESSENGER_COMMENT_PLATFORMS",
        path: &["accounts", "messenger", "comment_platforms"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_MESSENGER_PROXY",
        path: &["accounts", "messenger", "proxy"],
        kind: Kind::Text,
    },
    // Local
    EnvRule {
        var: "XPST_LOCAL_PATH",
        path: &["accounts", "local", "path"],
        kind: Kind::Text,
    },
    // Reliability / schedule / monitoring
    EnvRule {
        var: "XPST_MAX_RETRIES",
        path: &["reliability", "max_retries"],
        kind: Kind::Int,
    },
    EnvRule {
        var: "XPST_ANALYTICS_SNAPSHOT_ENABLED",
        path: &["schedule", "analytics_snapshot_enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_ANALYTICS_SNAPSHOT_INTERVAL",
        path: &["schedule", "analytics_snapshot_interval"],
        kind: Kind::Int,
    },
    EnvRule {
        var: "XPST_LOG_LEVEL",
        path: &["monitoring", "log_level"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_LOG_FILE",
        path: &["monitoring", "log_file"],
        kind: Kind::Text,
    },
    // Notifications
    EnvRule {
        var: "XPST_NOTIFICATIONS_ENABLED",
        path: &["notifications", "enabled"],
        kind: Kind::Bool,
    },
    EnvRule {
        var: "XPST_DISCORD_WEBHOOK_URL",
        path: &["notifications", "discord", "webhook_url"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TELEGRAM_BOT_TOKEN",
        path: &["notifications", "telegram", "bot_token"],
        kind: Kind::Text,
    },
    EnvRule {
        var: "XPST_TELEGRAM_CHAT_ID",
        path: &["notifications", "telegram", "chat_id"],
        kind: Kind::Text,
    },
    // Provider mode
    EnvRule {
        var: "XPST_PROVIDER_MODE",
        path: &["provider_mode"],
        kind: Kind::Lower,
    },
];

fn apply_env(document: &mut Value, env: &dyn Fn(&str) -> Option<String>) -> Result<(), String> {
    for rule in ENV_RULES {
        // `if v := os.getenv(...)`: an empty value is falsy and skipped.
        let Some(raw) = env(rule.var).filter(|value| !value.is_empty()) else {
            continue;
        };
        let value = match rule.kind {
            Kind::Text => Value::String(raw),
            Kind::Bool => Value::Bool(is_true(&raw)),
            Kind::Lower => Value::String(raw.to_lowercase()),
            Kind::LowerStripped => Value::String(raw.trim().to_lowercase()),
            Kind::PublishMode => {
                let mode = raw.trim().to_lowercase();
                if !matches!(mode.as_str(), "auto" | "browser" | "browser_only") {
                    continue;
                }
                Value::String(mode)
            }
            Kind::JsonMapping => {
                // `json.loads` failure empties the map; a non-object leaves the
                // current value alone.
                match serde_json::from_str::<serde_json::Value>(&raw) {
                    Ok(serde_json::Value::Object(map)) => {
                        Value::Mapping(json_object_to_mapping(&map))
                    }
                    Ok(_) => continue,
                    Err(_) => Value::Mapping(Mapping::new()),
                }
            }
            Kind::Int => {
                let Some(number) = python_int_text(&raw) else {
                    return Err(format!("invalid literal for int() with base 10: '{}'", raw));
                };
                Value::Number(number.into())
            }
        };
        set_path(document, rule.path, value);
    }
    Ok(())
}

fn is_true(value: &str) -> bool {
    matches!(value.to_lowercase().as_str(), "true" | "1" | "yes")
}

fn json_object_to_mapping(map: &serde_json::Map<String, serde_json::Value>) -> Mapping {
    let mut out = Mapping::new();
    for (key, value) in map {
        out.insert(
            Value::from(key.as_str()),
            xpst_config::output::from_json(value),
        );
    }
    out
}

// ── 5. `_rebase_credential_paths` ───────────────────────────────────────────

fn rebase_credential_paths(document: &mut Value, config_dir: &Path, home: &Path) {
    let default_root = home.join(".xpst");
    // `if not _config_dir_override(): return` plus `if active_root ==
    // default_root: return` — with no explicit profile the two roots are the
    // same directory, so the comparison covers both.
    if config_dir == default_root {
        return;
    }
    for (section, field) in LEGACY_PATH_FIELDS {
        let path: &[&str] = &["accounts", section, field];
        let Some(Value::String(raw)) = get_path(document, path) else {
            continue;
        };
        if raw.is_empty() {
            continue;
        }
        let candidate = expand_home(raw, &home.display().to_string());
        let Ok(relative) = Path::new(&candidate).strip_prefix(&default_root) else {
            continue;
        };
        let rebased = config_dir.join(relative).display().to_string();
        set_string(document, path, rebased);
    }
}

/// `os.path.expanduser` for the `~` and `~/...` forms.
fn expand_home(text: &str, home: &str) -> String {
    if text == "~" {
        return home.to_string();
    }
    if let Some(rest) = text.strip_prefix("~/") {
        if home.is_empty() {
            return text.to_string();
        }
        return PathBuf::from(home).join(rest).display().to_string();
    }
    text.to_string()
}

// ── 6. `_expand_paths` ──────────────────────────────────────────────────────

fn expand_paths(document: &mut Value, env: &dyn Fn(&str) -> Option<String>) {
    let mut fields: Vec<(&str, &str)> = LEGACY_PATH_FIELDS.to_vec();
    fields.push(("video", "download_dir"));
    fields.push(("monitoring", "log_file"));
    for (section, field) in fields {
        let path: &[&str] = if section == "video" || section == "monitoring" {
            &[section, field]
        } else {
            &["accounts", section, field]
        };
        let Some(Value::String(raw)) = get_path(document, path) else {
            continue;
        };
        if raw.is_empty() {
            continue;
        }
        let expanded = expand_path(raw, env);
        if expanded != *raw {
            set_string(document, path, expanded);
        }
    }
}

// ── 7. `_validate` ──────────────────────────────────────────────────────────

const MIN_ANALYTICS_SNAPSHOT_INTERVAL: i64 = 60;
const MAX_ANALYTICS_SNAPSHOT_INTERVAL: i64 = 86_400;

fn validate(document: &Value) -> Result<(), String> {
    let mut errors: Vec<String> = Vec::new();

    let check_interval = get_path(document, &["schedule", "check_interval"])
        .and_then(python_int)
        .unwrap_or_default();
    if check_interval < 60 {
        errors.push("Check interval must be at least 60 seconds".to_string());
    }
    let catchup = get_path(document, &["schedule", "catchup_window"])
        .and_then(python_int)
        .unwrap_or_default();
    if catchup < 3600 {
        errors.push("Catchup window must be at least 1 hour".to_string());
    }
    let snapshot = get_path(document, &["schedule", "analytics_snapshot_interval"])
        .and_then(python_int)
        .unwrap_or_default();
    if !(MIN_ANALYTICS_SNAPSHOT_INTERVAL..=MAX_ANALYTICS_SNAPSHOT_INTERVAL).contains(&snapshot) {
        errors.push(format!(
            "Analytics snapshot interval must be between {MIN_ANALYTICS_SNAPSHOT_INTERVAL} \
             and {MAX_ANALYTICS_SNAPSHOT_INTERVAL} seconds"
        ));
    }

    for platform in ["youtube", "instagram", "x", "tiktok"] {
        let block = &["video", "encoding", platform];
        if let Some(resolution) = get_path(document, block)
            .and_then(|value| value.get("resolution"))
            .and_then(python_int)
        {
            if !matches!(resolution, 360 | 480 | 720 | 1080 | 1440 | 1920 | 2160) {
                errors.push(format!("Invalid resolution for {platform}: {resolution}"));
            }
        }
        if let Some(crf) = get_path(document, block)
            .and_then(|value| value.get("crf"))
            .and_then(python_int)
        {
            if !(0..=51).contains(&crf) {
                errors.push(format!("Invalid CRF for {platform}: {crf}"));
            }
        }
        if let Some(fps) = get_path(document, block)
            .and_then(|value| value.get("fps"))
            .and_then(python_int)
        {
            if !matches!(fps, 24 | 25 | 30 | 60) {
                errors.push(format!("Invalid FPS for {platform}: {fps}"));
            }
        }
    }

    if errors.is_empty() {
        return Ok(());
    }
    let mut message = String::from("Configuration errors:");
    for error in errors {
        message.push_str("\n  - ");
        message.push_str(&error);
    }
    Err(message)
}

// ── 8. the `_SECRET_CONFIG_FIELDS` migration ────────────────────────────────

fn migrate_secret(document: &mut Value, request: &SaveRequest<'_>) {
    let path: &[&str] = &["accounts", "tiktok", "client_secret"];
    let Some(Value::String(secret)) = get_path(document, path) else {
        return;
    };
    if secret.is_empty() {
        return;
    }
    let secret = secret.clone();
    let config_dir = request.config_dir.display().to_string();
    if (request.store_secret)(&config_dir, "tiktok_client_secret", &secret) {
        set_string(document, path, String::new());
    }
}

// ── Python scalar coercions ─────────────────────────────────────────────────

/// Python's `int()` for a YAML value: `int(42.7)` truncates, `int("4")` parses,
/// `int("4.2")` and `int(None)` fail.
fn python_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(flag) => Some(i64::from(*flag)),
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|float| float.trunc() as i64)),
        Value::String(text) => python_int_text(text),
        _ => None,
    }
}

/// Python's `int(text)`: surrounding whitespace and `_` digit separators are
/// accepted, a sign is allowed, and anything else is a `ValueError`.
fn python_int_text(text: &str) -> Option<i64> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    let (sign, digits) = match trimmed.as_bytes()[0] {
        b'+' => (1, &trimmed[1..]),
        b'-' => (-1, &trimmed[1..]),
        _ => (1, trimmed),
    };
    if digits.is_empty() {
        return None;
    }
    let mut value: i64 = 0;
    for character in digits.chars() {
        if character == '_' {
            continue;
        }
        let digit = character.to_digit(10)?;
        value = value.checked_mul(10)?.checked_add(i64::from(digit))?;
    }
    Some(sign * value)
}

/// Python's `float()` for a YAML value.
fn python_float(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.trim().replace('_', "").parse::<f64>().ok(),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn document() -> Value {
        xpst_config::save_defaults("/profile", "/home").expect("defaults")
    }

    #[test]
    fn python_int_matches_the_shipped_coercions() {
        assert_eq!(python_int(&Value::String("42".into())), Some(42));
        assert_eq!(python_int(&Value::String(" 42 ".into())), Some(42));
        assert_eq!(python_int(&Value::String("1_000".into())), Some(1000));
        assert_eq!(python_int(&Value::String("-7".into())), Some(-7));
        assert_eq!(python_int(&Value::String("+7".into())), Some(7));
        assert_eq!(python_int(&Value::String("4.2".into())), None);
        assert_eq!(python_int(&Value::String("".into())), None);
        assert_eq!(python_int(&Value::Null), None);
        assert_eq!(python_int(&Value::Bool(true)), Some(1));
        assert_eq!(
            python_int(&Value::Number(serde_json::Number::from_f64(42.7).unwrap())),
            Some(42)
        );
    }

    #[test]
    fn set_path_refuses_a_key_the_document_does_not_have() {
        let mut doc = document();
        assert!(!set_path(
            &mut doc,
            &["accounts", "x", "api_key"],
            Value::Null
        ));
        assert!(set_path(
            &mut doc,
            &["accounts", "x", "enabled"],
            Value::Bool(false)
        ));
    }

    #[test]
    fn a_partial_encoding_block_resets_the_other_fields() {
        let file = xpst_config::yaml::from_str(
            "video:\n  encoding:\n    youtube:\n      resolution: 1080\n",
        )
        .expect("yaml");
        let mut doc = document();
        merge_file(&mut doc, Some(&file));
        let block = get_path(&doc, &["video", "encoding", "youtube"]).expect("block");
        assert_eq!(block.get("resolution").and_then(python_int), Some(1080));
        // The dataclass default, not the shipped preset.
        assert_eq!(block.get("bitrate"), Some(&Value::Null));
        assert_eq!(block.get("passthrough"), Some(&Value::Bool(false)));
    }

    #[test]
    fn an_empty_section_leaves_the_defaults_alone() {
        let file = xpst_config::yaml::from_str("accounts:\n  youtube: {}\n").expect("yaml");
        let mut doc = document();
        merge_file(&mut doc, Some(&file));
        assert_eq!(
            get_path(&doc, &["accounts", "youtube", "enabled"]),
            Some(&Value::Bool(true))
        );
    }

    #[test]
    fn a_string_false_is_true_for_the_boolean_fields() {
        let file = xpst_config::yaml::from_str(
            "first_run_complete: 'false'\nanti_bot:\n  caption_variation: 'false'\n",
        )
        .expect("yaml");
        let mut doc = document();
        merge_file(&mut doc, Some(&file));
        assert_eq!(
            get_path(&doc, &["first_run_complete"]),
            Some(&Value::Bool(true))
        );
        assert_eq!(
            get_path(&doc, &["anti_bot", "caption_variation"]),
            Some(&Value::Bool(true))
        );
    }
}
